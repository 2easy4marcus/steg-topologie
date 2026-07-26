import pytest

from app import db


def _insert_snapshot(snapshot_id="snapshot-1", content_hash="hash-1"):
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notice_snapshots(
                snapshot_id, notice_id, source_url, content_hash,
                raw_html, first_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_id,
                "notice-1",
                "https://example.test/notice-1",
                content_hash,
                "<html></html>",
                "2026-07-26T10:00:00Z",
            ],
        )


def test_evidence_schema_is_initialized():
    expected = {
        "notice_fetch_attempts",
        "notice_snapshots",
        "notice_parses",
        "notice_localities",
        "notice_state",
        "model_builds",
        "build_locality_counts",
        "build_cooccurrences",
        "model_state",
        "cluster_runs",
        "cluster_members",
        "cluster_state",
    }
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert expected <= {row["name"] for row in rows}


def test_snapshot_identity_is_unique_per_notice_and_content_hash():
    _insert_snapshot()

    with pytest.raises(Exception):
        _insert_snapshot(snapshot_id="snapshot-2", content_hash="hash-1")


def test_parse_identity_is_unique_per_snapshot_and_versions():
    _insert_snapshot()
    values = [
        "parse-1",
        "snapshot-1",
        "notice-1",
        "Notice",
        "26/07/2026",
        "2026-07-26",
        "2",
        "1",
        "ok",
        "[]",
        "2026-07-26T10:01:00Z",
    ]
    sql = """
        INSERT INTO notice_parses(
            parse_id, snapshot_id, notice_id, title, notice_date_raw,
            notice_date_iso, parser_version, normalization_version,
            parse_status, parse_warnings, parsed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with db.get_conn() as conn:
        conn.execute(sql, values)

    values[0] = "parse-2"
    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute(sql, values)


def test_public_build_query_omits_internal_error_detail():
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO model_builds(
                build_id, status, created_at, public_error_code,
                internal_error_detail
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                "build-1",
                "failed",
                "2026-07-26T10:00:00Z",
                "database_unavailable",
                "token=must-not-leak",
            ],
        )

    result = db.get_model_build_public("build-1")

    assert result["public_error_code"] == "database_unavailable"
    assert "internal_error_detail" not in result

