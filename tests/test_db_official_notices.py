# tests/test_db_official_notices.py
from app import db


def test_official_notice_exists_false_for_unknown_id():
    assert db.official_notice_exists("does-not-exist") is False


def test_official_notice_exists_true_after_upsert():
    db.upsert_official_notice({
        "id": "n-exists-1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": [], "subregions": [],
        "raw_text": "raw", "scraped_at": "2026-07-23T18:00:00+00:00",
    })
    assert db.official_notice_exists("n-exists-1") is True
