import json

import pytest

from app import db, import_official, observability


NOW = "2026-07-26T12:00:00+00:00"


def _seed_job(job_id, started_at, status="completed"):
    db.start_ingestion_run(job_id, "scrape", started_at)
    db.finish_ingestion_run(job_id, status, started_at)


def test_cleanup_keeps_boundary_rows_and_removes_only_older_history():
    _seed_job("job-old", "2026-04-26T11:59:59+00:00")
    _seed_job("job-boundary", "2026-04-27T12:00:00+00:00")
    db.insert_job_event(
        "event-old", "job-boundary", "2026-06-26T11:59:59+00:00",
        "info", "job_started", "Job started.", None, None,
    )
    db.insert_job_event(
        "event-boundary", "job-boundary", "2026-06-26T12:00:00+00:00",
        "info", "job_started", "Job started.", None, None,
    )

    observability.cleanup_after_scheduled_job(succeeded=True, now=NOW)

    assert db.get_ingestion_run_public("job-old") is None
    assert db.get_ingestion_run_public("job-boundary") is not None
    assert [row["id"] for row in db.list_job_events("job-boundary")] == [
        "event-boundary"
    ]


def test_cleanup_does_not_run_after_failed_job(monkeypatch):
    calls = []
    monkeypatch.setattr(
        db, "delete_expired_operations_batch",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    observability.cleanup_after_scheduled_job(succeeded=False, now=NOW)

    assert calls == []


def test_cleanup_uses_batches_no_larger_than_500(monkeypatch):
    batches = iter([(500, 500), (1, 0), (0, 0)])
    limits = []

    def fake_cleanup(*, jobs_before, events_before, batch_size):
        limits.append(batch_size)
        return next(batches)

    monkeypatch.setattr(db, "delete_expired_operations_batch", fake_cleanup)

    observability.cleanup_after_scheduled_job(succeeded=True, now=NOW)

    assert limits
    assert max(limits) == 500
    assert len(limits) == 3


def test_cleanup_failure_is_a_structured_warning_and_does_not_raise(
    monkeypatch, capsys
):
    def fail_cleanup(**kwargs):
        raise RuntimeError("database detail must not be logged")

    monkeypatch.setattr(db, "delete_expired_operations_batch", fail_cleanup)

    observability.cleanup_after_scheduled_job(succeeded=True, now=NOW)

    record = json.loads(capsys.readouterr().out)
    assert record["level"] == "warning"
    assert record["event"] == "operations_retention_failed"
    assert "database detail" not in json.dumps(record)


def test_scheduled_scrape_triggers_cleanup_only_after_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        import_official.steg_scraper, "scrape_current_notices", lambda: []
    )
    monkeypatch.setattr(
        observability, "cleanup_after_scheduled_job",
        lambda **kwargs: calls.append(kwargs),
    )

    import_official.run(verbose=False)

    assert calls == [{"succeeded": True, "now": calls[0]["now"]}]


def test_failed_scheduled_scrape_does_not_trigger_cleanup(monkeypatch):
    calls = []

    def fail():
        raise RuntimeError("network failed")

    monkeypatch.setattr(
        import_official.steg_scraper, "scrape_current_notices", fail
    )
    monkeypatch.setattr(
        observability, "cleanup_after_scheduled_job",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="network failed"):
        import_official.run(verbose=False)

    assert calls == []
