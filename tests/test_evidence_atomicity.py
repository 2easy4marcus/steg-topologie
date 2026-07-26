import pytest

from app import db, evidence_pipeline


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


def test_only_completed_build_can_be_activated():
    db.create_model_build("building-1", "2026-07-26T10:00:00Z")

    with pytest.raises(ValueError, match="completed"):
        evidence_pipeline.activate_model_build("building-1")

    assert db.active_build_id() is None


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

