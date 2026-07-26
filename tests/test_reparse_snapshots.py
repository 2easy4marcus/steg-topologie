from app import db, import_official, locality_dedup, reparse_snapshots


def test_reparse_creates_new_normalization_version_without_refetch(monkeypatch):
    notice = {
        "id": "notice-1",
        "title": "Notice",
        "url": "https://example.test/notice-1",
        "notice_date": "26/07/2026",
        "zones": ["A", "B"],
        "subregions": [],
        "raw_text": "A B",
        "raw_html": "<html>A B</html>",
    }
    import_official.process_notice(
        notice, "2026-07-26T10:00:00+00:00"
    )
    before = db.get_notice_state("notice-1")["active_parse_id"]
    monkeypatch.setattr(locality_dedup, "NORMALIZATION_VERSION", "2")

    assert reparse_snapshots.main(["--apply"]) == 0

    after = db.get_notice_state("notice-1")["active_parse_id"]
    assert after != before
    with db.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM notice_snapshots"
        ).fetchone()["c"]
    assert count == 1


def test_reparse_dry_run_does_not_change_active_parse(monkeypatch):
    notice = {
        "id": "notice-1", "title": "Notice",
        "url": "https://example.test/notice-1",
        "notice_date": "26/07/2026", "zones": ["A", "B"],
        "subregions": [], "raw_text": "A B", "raw_html": "<html>A B</html>",
    }
    import_official.process_notice(
        notice, "2026-07-26T10:00:00+00:00"
    )
    before = db.get_notice_state("notice-1")["active_parse_id"]
    monkeypatch.setattr(locality_dedup, "NORMALIZATION_VERSION", "2")

    assert reparse_snapshots.main([]) == 0

    assert db.get_notice_state("notice-1")["active_parse_id"] == before

