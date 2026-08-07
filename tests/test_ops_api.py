from fastapi.testclient import TestClient

from app import db, main, observability


def _seed_job(job_id="job-1"):
    db.start_ingestion_run(
        job_id, "scrape", "2026-07-26T10:00:00Z", request_id="req-1"
    )
    observability.record_job_event(
        job_id,
        "job_started",
        occurred_at="2026-07-26T10:00:00Z",
        request_id="req-1",
    )


def test_ops_api_is_unavailable_without_configured_secret(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", None)
    client = TestClient(main.app)

    response = client.get("/api/internal/ops/jobs")

    assert response.status_code == 503


def test_ops_api_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "ops-secret")
    client = TestClient(main.app)

    response = client.get(
        "/api/internal/ops/jobs", headers={"X-Ops-Secret": "wrong"}
    )

    assert response.status_code == 401


def test_ops_jobs_and_events_return_safe_paginated_data(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "ops-secret")
    _seed_job()
    client = TestClient(main.app)
    headers = {"X-Ops-Secret": "ops-secret"}

    jobs = client.get(
        "/api/internal/ops/jobs?limit=1", headers=headers
    )
    detail = client.get(
        "/api/internal/ops/jobs/job-1", headers=headers
    )
    events = client.get(
        "/api/internal/ops/jobs/job-1/events?limit=1", headers=headers
    )

    assert jobs.status_code == detail.status_code == events.status_code == 200
    assert jobs.json()["items"][0]["id"] == "job-1"
    assert detail.json()["id"] == "job-1"
    assert events.json()["items"][0]["event_type"] == "job_started"
    combined = f"{jobs.text}{detail.text}{events.text}"
    assert "internal_error_detail" not in combined
    assert "ops-secret" not in combined


def test_ops_summary_requires_secret(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "ops-secret")
    client = TestClient(main.app)

    assert client.get("/api/internal/ops/summary").status_code == 401
    assert client.get(
        "/api/internal/ops/summary", headers={"X-Ops-Secret": "wrong"}
    ).status_code == 401


def test_ops_summary_returns_metrics_without_secrets(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "ops-secret")
    client = TestClient(main.app)

    response = client.get(
        "/api/internal/ops/summary", headers={"X-Ops-Secret": "ops-secret"}
    )

    assert response.status_code == 200
    body = response.json()
    assert "sample_count" in body
    assert "status_counts" in body
    assert "headers" not in response.text.lower()
    assert "ops-secret" not in response.text


def test_ops_limits_are_bounded(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "ops-secret")
    client = TestClient(main.app)
    headers = {"X-Ops-Secret": "ops-secret"}

    assert client.get(
        "/api/internal/ops/jobs?limit=101", headers=headers
    ).status_code == 422
    assert client.get(
        "/api/internal/ops/jobs/job-1/events?limit=501", headers=headers
    ).status_code == 422

