# tests/test_import_official_cooccurrence.py
from app import db, import_official


def test_run_records_cooccurrences_for_each_notice(monkeypatch):
    fake_notices = [{
        "id": "n1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": ["Dekka", "Tozeur", "Kebili"],
        "subregions": [], "raw_text": "raw",
    }]
    monkeypatch.setattr(import_official.steg_scraper, "scrape_current_notices", lambda: fake_notices)

    import_official.run(verbose=False)

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    # 3 zones -> 3 pairs, each seen once in this one notice
    assert rows[("Dekka", "Kebili")] == 1
    assert rows[("Dekka", "Tozeur")] == 1
    assert rows[("Kebili", "Tozeur")] == 1
    assert db.get_locality("Dekka") is not None


def test_run_extracts_pairs_from_subregions_too(monkeypatch):
    fake_notices = [{
        "id": "n2", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": [],
        "subregions": [
            {"name": "جهة زغوان", "zones": ["Zaghouan Ville", "Bir Mcherga"]},
        ],
        "raw_text": "raw",
    }]
    monkeypatch.setattr(import_official.steg_scraper, "scrape_current_notices", lambda: fake_notices)

    import_official.run(verbose=False)

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    assert rows[("Bir Mcherga", "Zaghouan Ville")] == 1


def test_run_handles_notice_with_zero_or_one_locality_without_crashing(monkeypatch):
    fake_notices = [
        {
            "id": "n3", "title": "t", "url": "http://x", "region": "جهة الشمال",
            "notice_date": "23/07/2026", "notice_time": "18:00",
            "time_window_sentence": "s", "zones": [],
            "subregions": [], "raw_text": "raw",
        },
        {
            "id": "n4", "title": "t", "url": "http://x", "region": "جهة الشمال",
            "notice_date": "23/07/2026", "notice_time": "18:00",
            "time_window_sentence": "s", "zones": ["Solo Town"],
            "subregions": [], "raw_text": "raw",
        },
    ]
    monkeypatch.setattr(import_official.steg_scraper, "scrape_current_notices", lambda: fake_notices)

    # Must not raise -- 0 and 1 resolved localities both produce zero pairs.
    count = import_official.run(verbose=False)

    assert count == 2
    assert db.list_cooccurrences() == []
    assert db.get_locality("Solo Town") is not None


def test_run_collapses_duplicate_locality_in_same_notice(monkeypatch):
    fake_notices = [{
        "id": "n5", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": ["Dekka", "Dekka", "Tozeur"],
        "subregions": [], "raw_text": "raw",
    }]
    monkeypatch.setattr(import_official.steg_scraper, "scrape_current_notices", lambda: fake_notices)

    import_official.run(verbose=False)

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    # "Dekka" appearing twice must NOT produce a self-pair (Dekka, Dekka),
    # and must not double-count the Dekka-Tozeur pair.
    assert ("Dekka", "Dekka") not in rows
    assert rows[("Dekka", "Tozeur")] == 1
    assert len(rows) == 1
    assert db.get_locality_notice_counts()["Dekka"] == 1


def test_process_notice_upserts_and_records_cooccurrence():
    notice = {
        "id": "pn1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": ["Dekka", "Tozeur"],
        "subregions": [], "raw_text": "raw",
    }
    changed = import_official.process_notice(
        notice, "2026-07-23T18:00:00+00:00"
    )
    import_official.rebuild_if_changed(
        changed, "2026-07-23T18:00:00+00:00"
    )

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    assert rows[("Dekka", "Tozeur")] == 1
    assert db.count_official_notices() == 1
    assert notice["scraped_at"] == "2026-07-23T18:00:00+00:00"


def test_processing_identical_notice_twice_does_not_inflate_evidence():
    notice = {
        "id": "pn1", "title": "t", "url": "http://x",
        "region": "جهة الشمال", "notice_date": "23/07/2026",
        "notice_time": "18:00", "time_window_sentence": "s",
        "zones": ["Dekka", "Tozeur"], "subregions": [],
        "raw_text": "raw", "raw_html": "<html>Dekka Tozeur</html>",
    }

    changed_first = import_official.process_notice(
        notice, "2026-07-23T18:00:00+00:00"
    )
    changed_second = import_official.process_notice(
        notice, "2026-07-23T19:00:00+00:00"
    )
    if changed_first:
        import_official.rebuild_if_changed(True, "2026-07-23T18:00:00+00:00")
    if changed_second:
        import_official.rebuild_if_changed(True, "2026-07-23T19:00:00+00:00")

    rows = db.list_cooccurrences()
    assert changed_first is True
    assert changed_second is False
    assert len(rows) == 1
    assert rows[0]["notice_count"] == 1
    assert db.get_locality_notice_counts()["Dekka"] == 1


def test_changed_content_replaces_active_parse_and_rebuilds():
    notice = {
        "id": "pn1", "title": "t", "url": "http://x",
        "notice_date": "23/07/2026", "zones": ["A", "B"],
        "subregions": [], "raw_text": "A B",
        "raw_html": "<html>A B</html>",
    }
    assert import_official.process_notice(
        notice, "2026-07-23T18:00:00+00:00"
    )
    import_official.rebuild_if_changed(True, "2026-07-23T18:00:00+00:00")

    notice.update(
        zones=["A", "C"],
        raw_text="A C",
        raw_html="<html>A C</html>",
    )
    assert import_official.process_notice(
        notice, "2026-07-23T19:00:00+00:00"
    )
    import_official.rebuild_if_changed(True, "2026-07-23T19:00:00+00:00")

    pairs = {
        (row["locality_a"], row["locality_b"])
        for row in db.list_cooccurrences()
    }
    assert pairs == {("A", "C")}
