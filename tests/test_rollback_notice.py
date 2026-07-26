import pytest

from app import db, import_official, rollback_notice


def _two_parses():
    first = {
        "id": "notice-1", "title": "Notice",
        "url": "https://example.test/notice-1",
        "notice_date": "26/07/2026", "zones": ["A", "B"],
        "subregions": [], "raw_text": "A B", "raw_html": "<html>A B</html>",
    }
    second = {**first, "zones": ["A", "C"], "raw_html": "<html>A C</html>"}
    import_official.process_notice(first, "2026-07-26T10:00:00+00:00")
    old_parse = db.get_notice_state("notice-1")["active_parse_id"]
    import_official.process_notice(second, "2026-07-26T11:00:00+00:00")
    return old_parse, db.get_notice_state("notice-1")["active_parse_id"]


def test_rollback_defaults_to_dry_run():
    old_parse, current = _two_parses()

    assert rollback_notice.main(["notice-1", old_parse]) == 0

    assert db.get_notice_state("notice-1")["active_parse_id"] == current


def test_rollback_apply_records_reason_and_activates_owned_parse():
    old_parse, _ = _two_parses()

    assert rollback_notice.main(
        ["notice-1", old_parse, "--apply", "--reason", "parser regression"]
    ) == 0

    assert db.get_notice_state("notice-1")["active_parse_id"] == old_parse
    assert db.latest_notice_rollback("notice-1")["reason"] == "parser regression"


def test_rollback_rejects_parse_owned_by_another_notice():
    old_parse, _ = _two_parses()

    with pytest.raises(ValueError, match="belong"):
        rollback_notice.main(["notice-2", old_parse, "--apply"])

