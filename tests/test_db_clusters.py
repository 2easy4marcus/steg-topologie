# tests/test_db_clusters.py
from app import db


def test_new_tables_exist_and_roundtrip():
    db.upsert_locality("Dekka", lat=None, lng=None, governorate=None)
    row = db.get_locality("Dekka")
    assert row["name"] == "Dekka"
    assert row["lat"] is None

    db.set_locality_coords("Dekka", 34.1, 9.2)
    row = db.get_locality("Dekka")
    assert row["lat"] == 34.1
    assert row["lng"] == 9.2

    db.record_alias("دقه", "Dekka")
    assert db.resolve_alias("دقه") == "Dekka"
    assert db.resolve_alias("unknown") is None

    db.increment_cooccurrence("Dekka", "Tozeur")
    db.increment_cooccurrence("Dekka", "Tozeur")
    rows = db.list_cooccurrences()
    assert rows == [{"locality_a": "Dekka", "locality_b": "Tozeur", "notice_count": 2, "last_seen": ""}]

    db.write_cluster_run("2026-07-24", {"Dekka": 0, "Tozeur": 0}, {"Dekka": 0.5, "Tozeur": 0.5})
    latest = db.latest_cluster_run()
    assert latest["run_date"] == "2026-07-24"
    assert {r["locality"] for r in latest["rows"]} == {"Dekka", "Tozeur"}
    dekka_row = next(r for r in latest["rows"] if r["locality"] == "Dekka")
    tozeur_row = next(r for r in latest["rows"] if r["locality"] == "Tozeur")
    assert dekka_row["lat"] == 34.1
    assert dekka_row["lng"] == 9.2
    assert tozeur_row["lat"] is None
    assert tozeur_row["lng"] is None


def test_notices_and_reports_still_work_after_migration():
    """Existing behavior (from before this feature) must not regress."""
    notice = {
        "id": "n1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "من 8 الى 12", "zones": ["Dekka", "Tozeur"],
        "subregions": [], "raw_text": "raw", "scraped_at": "2026-07-24T00:00:00Z",
    }
    db.upsert_official_notice(notice)
    assert db.count_official_notices() == 1
    fetched = db.list_official_notices()[0]
    assert fetched["zones"] == ["Dekka", "Tozeur"]

    report_id = db.create_user_report({
        "utility": "electricity", "status": "active", "governorate": "Tunis",
        "delegation": None, "zone_text": None, "comment": "",
        "started_at": None, "ended_at": None, "created_at": "2026-07-24T00:00:00Z",
    })
    assert report_id is not None
    assert db.list_user_reports()[0]["governorate"] == "Tunis"
