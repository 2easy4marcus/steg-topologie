# tests/test_backfill_official.py
from bs4 import BeautifulSoup

from app import backfill_official, db, steg_scraper


def _page_soup(notice_paths):
    """A minimal archive-listing page containing one <a href> per given
    /fr/news/<slug> path, plus pager/index links that must be ignored."""
    links = "".join(f'<a href="{p}">x</a>' for p in notice_paths)
    html = f"""
    <div>
      {links}
      <a href="/fr/news?page=1">suivant</a>
      <a href="/fr/news">first</a>
    </div>
    """
    return BeautifulSoup(html, "html.parser")


def _fake_detail(title, zones):
    return {"title": title, "raw_text": "raw", "zones": zones,
            "subregions": [], "time_window_sentence": "s"}


def test_archive_page_links_ignores_pager_and_index_links(monkeypatch):
    monkeypatch.setattr(
        steg_scraper, "fetch",
        lambda url: _page_soup(["/fr/news/notice-a", "/fr/news/notice-b"]),
    )
    links = backfill_official._archive_page_links(0)
    assert links == [
        "https://www.steg.com.tn/fr/news/notice-a",
        "https://www.steg.com.tn/fr/news/notice-b",
    ]


def test_crawl_archive_imports_new_notices_then_stops(monkeypatch):
    pages = {
        0: ["/fr/news/notice-a", "/fr/news/notice-b"],
        1: ["/fr/news/notice-b", "/fr/news/notice-c"],  # b repeats, c is new
        2: ["/fr/news/notice-b", "/fr/news/notice-c"],  # nothing new -> never fetched
    }
    fetch_calls = []

    def fake_fetch(url):
        fetch_calls.append(url)
        page = 0 if url == backfill_official.ARCHIVE_URL else int(url.split("page=")[1])
        return _page_soup(pages[page])

    details = {
        "https://www.steg.com.tn/fr/news/notice-a": _fake_detail(
            "إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 20/07/2026", ["Dekka", "Tozeur"]),
        "https://www.steg.com.tn/fr/news/notice-b": _fake_detail(
            "إشعار بانقطاع الكهرباء - جهة الوسط - 11:00 21/07/2026", ["Kebili"]),
        "https://www.steg.com.tn/fr/news/notice-c": _fake_detail(
            "Some unrelated recruitment notice", ["Ignored"]),
    }

    monkeypatch.setattr(steg_scraper, "fetch", fake_fetch)
    monkeypatch.setattr(steg_scraper, "parse_notice_detail", lambda url: details[url])

    imported = backfill_official.crawl_archive(max_pages=10, verbose=False)

    assert imported == 2  # notice-a and notice-b are real outage notices
    assert db.official_notice_exists("notice-a") is True
    assert db.official_notice_exists("notice-b") is True
    assert db.official_notice_exists("notice-c") is False  # no marker in title -> skipped
    assert f"{backfill_official.ARCHIVE_URL}?page=2" not in fetch_calls


def test_crawl_archive_respects_max_pages_safety_cap(monkeypatch):
    call_count = {"n": 0}

    def fake_fetch(url):
        call_count["n"] += 1
        page = 0 if url == backfill_official.ARCHIVE_URL else int(url.split("page=")[1])
        # A never-before-seen notice on every page -- would run forever
        # without the max_pages cap.
        return _page_soup([f"/fr/news/notice-{page}"])

    monkeypatch.setattr(steg_scraper, "fetch", fake_fetch)
    monkeypatch.setattr(
        steg_scraper, "parse_notice_detail",
        lambda url: _fake_detail("إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 20/07/2026", ["Dekka"]),
    )

    imported = backfill_official.crawl_archive(max_pages=3, verbose=False)

    assert imported == 3
    assert call_count["n"] == 3
