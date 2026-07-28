import logging

import pytest

from app import db, evidence_pipeline
from app.evidence_models import ParsedLocality, ParsedNoticeEvidence, ParseStatus


def _seed_snapshot_and_parse(
    snapshot_id: str,
    parse_id: str,
    *,
    parse_status: str = "ok",
):
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notice_snapshots(
                snapshot_id, notice_id, source_url, content_hash,
                raw_html, first_fetched_at
            ) VALUES (?, 'notice-1', 'https://example.test', ?, '<html/>', ?)
            """,
            [snapshot_id, f"hash-{snapshot_id}", "2026-07-26T10:00:00Z"],
        )
        conn.execute(
            """
            INSERT INTO notice_parses(
                parse_id, snapshot_id, notice_id, title, parser_version,
                normalization_version, parse_status, parse_warnings, parsed_at
            ) VALUES (?, ?, 'notice-1', 'Notice', '2', '1', ?, '[]', ?)
            """,
            [parse_id, snapshot_id, parse_status, "2026-07-26T10:01:00Z"],
        )


def test_parse_activation_requires_latest_snapshot():
    _seed_snapshot_and_parse("snapshot-old", "parse-old")
    _seed_snapshot_and_parse("snapshot-new", "parse-new")
    db.select_latest_snapshot(
        "notice-1", "snapshot-new", "2026-07-26T10:02:00Z"
    )

    with pytest.raises(ValueError, match="latest snapshot"):
        evidence_pipeline.activate_parse(
            "notice-1", "parse-old", "2026-07-26T10:03:00Z"
        )

    assert db.get_notice_state("notice-1")["active_parse_id"] is None


def test_failed_parse_cannot_replace_last_known_valid_parse():
    _seed_snapshot_and_parse("snapshot-old", "parse-old")
    db.select_latest_snapshot(
        "notice-1", "snapshot-old", "2026-07-26T10:01:00Z"
    )
    evidence_pipeline.activate_parse(
        "notice-1", "parse-old", "2026-07-26T10:02:00Z"
    )
    _seed_snapshot_and_parse(
        "snapshot-new", "parse-failed", parse_status="failed"
    )
    db.select_latest_snapshot(
        "notice-1", "snapshot-new", "2026-07-26T10:03:00Z"
    )

    with pytest.raises(ValueError, match="eligible"):
        evidence_pipeline.activate_parse(
            "notice-1", "parse-failed", "2026-07-26T10:04:00Z"
        )

    assert db.get_notice_state("notice-1")["active_parse_id"] == "parse-old"


def test_refused_activation_is_logged_not_swallowed(caplog):
    """persist_notice must not crash on an ineligible parse -- but it must not
    hide it either. A bare `except ValueError: pass` here is what let a bug
    that refused EVERY activation run unnoticed in production for days."""
    notice = {
        "id": "notice-ineligible",
        "url": "https://example.test/notice-ineligible",
        # No outage-notice marker in the title -> parse_status 'warning', and
        # a single locality -> below the 2-locality bar warnings must clear.
        "title": "Unrelated recruitment announcement",
        "notice_date": "20/07/2026",
        "zones": ["Only One Zone"],
        "subregions": [],
        "raw_text": "raw",
    }

    with caplog.at_level(logging.WARNING, logger="app.evidence_pipeline"):
        changed = evidence_pipeline.persist_notice(
            notice, fetched_at="2026-07-26T10:00:00Z"
        )

    assert changed is False  # control flow unchanged: no crash, no activation
    assert db.get_notice_state("notice-ineligible")["active_parse_id"] is None
    records = [
        r for r in caplog.records
        if r.name == "app.evidence_pipeline" and r.levelno >= logging.WARNING
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "notice-ineligible" in message
    assert "not eligible for activation" in message


def test_only_completed_build_can_be_activated():
    db.create_model_build("building-1", "2026-07-26T10:00:00Z")

    with pytest.raises(ValueError, match="completed"):
        evidence_pipeline.activate_model_build("building-1")

    assert db.active_build_id() is None


def _evidence(snapshot_id: str, *, localities: int = 2) -> ParsedNoticeEvidence:
    return ParsedNoticeEvidence(
        notice_id="notice-1",
        snapshot_id=snapshot_id,
        source_url="https://example.test",
        title="Notice",
        parser_version="2",
        normalization_version="1",
        parse_status=ParseStatus.OK,
        localities=[
            ParsedLocality(
                raw_name=f"zone-{i}", canonical_name=f"zone-{i}", ordinal=i
            )
            for i in range(localities)
        ],
        warnings=[],
    )


# ---- guards below are enforced by single-statement SQL (no transactions);
# ---- these pin that folding the guard into the SQL did not weaken it.

def test_snapshot_must_belong_to_notice():
    """select_latest_snapshot's guard, now a WHERE EXISTS on the upsert."""
    _seed_snapshot_and_parse("snapshot-other", "parse-other")

    with pytest.raises(ValueError, match="snapshot does not belong to notice"):
        db.select_latest_snapshot(
            "notice-2", "snapshot-other", "2026-07-26T10:02:00Z"
        )

    assert db.get_notice_state("notice-2") is None


def test_duplicate_parse_id_returns_false_and_writes_nothing():
    """save_parse_with_localities' guard, now a WHERE NOT EXISTS."""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notice_snapshots(
                snapshot_id, notice_id, source_url, content_hash,
                raw_html, first_fetched_at
            ) VALUES ('snapshot-1', 'notice-1', 'https://example.test',
                      'hash-1', '<html/>', '2026-07-26T10:00:00Z')
            """
        )
    evidence = _evidence("snapshot-1", localities=2)

    assert db.save_parse_with_localities(
        evidence, "2026-07-26T10:01:00Z"
    ) is True
    # Same snapshot + versions -> same derived parse_id -> duplicate.
    assert db.save_parse_with_localities(
        _evidence("snapshot-1", localities=5), "2026-07-26T10:02:00Z"
    ) is False

    parse_id = db.processing_parse_id("snapshot-1", "2", "1")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM notice_parses"
        ).fetchone()["n"] == 1
        # The rejected duplicate must not have touched the locality rows.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM notice_localities WHERE parse_id = ?",
            [parse_id],
        ).fetchone()["n"] == 2


def test_running_cluster_run_cannot_be_activated():
    db.create_model_build("build-1", "2026-07-26T10:00:00Z")
    db.complete_model_build("build-1", "2026-07-26T10:01:00Z", 0, 0, 0)
    evidence_pipeline.activate_model_build("build-1")
    db.create_cluster_run(
        "run-1", "build-1", "ppmi-louvain-v1", "2026-07-26T10:02:00Z"
    )

    with pytest.raises(
        ValueError, match="cluster run must be completed before activation"
    ):
        evidence_pipeline.activate_cluster_run("run-1")

    assert db.active_cluster_run_id() is None


def test_rollback_rejects_failed_parse_and_writes_no_audit_row():
    _seed_snapshot_and_parse("snapshot-old", "parse-old")
    db.select_latest_snapshot(
        "notice-1", "snapshot-old", "2026-07-26T10:01:00Z"
    )
    evidence_pipeline.activate_parse(
        "notice-1", "parse-old", "2026-07-26T10:02:00Z"
    )
    _seed_snapshot_and_parse(
        "snapshot-bad", "parse-failed", parse_status="failed"
    )

    with pytest.raises(ValueError, match="not eligible for rollback"):
        db.rollback_notice_parse(
            "notice-1",
            "parse-failed",
            "regression",
            "2026-07-26T10:03:00Z",
            "rollback-1",
        )

    assert db.get_notice_state("notice-1")["active_parse_id"] == "parse-old"
    assert db.latest_notice_rollback("notice-1") is None


def test_rollback_requires_existing_notice_state():
    _seed_snapshot_and_parse("snapshot-old", "parse-old")

    with pytest.raises(ValueError, match="notice state not found"):
        db.rollback_notice_parse(
            "notice-1",
            "parse-old",
            "regression",
            "2026-07-26T10:03:00Z",
            "rollback-1",
        )

    assert db.latest_notice_rollback("notice-1") is None


def test_cluster_activation_requires_current_build():
    db.create_model_build("build-1", "2026-07-26T10:00:00Z")
    db.complete_model_build("build-1", "2026-07-26T10:01:00Z", 0, 0, 0)
    evidence_pipeline.activate_model_build("build-1")
    db.create_model_build("build-2", "2026-07-26T10:02:00Z")
    db.complete_model_build("build-2", "2026-07-26T10:03:00Z", 0, 0, 0)
    db.create_cluster_run(
        "run-2", "build-2", "ppmi-louvain-v1", "2026-07-26T10:04:00Z"
    )
    db.complete_cluster_run("run-2", "2026-07-26T10:05:00Z", 0, 0)

    with pytest.raises(ValueError, match="active build"):
        evidence_pipeline.activate_cluster_run("run-2")

    assert db.active_cluster_run_id() is None

