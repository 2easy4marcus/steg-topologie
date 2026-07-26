# STEG Parsing Fix + Historical Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the STEG multi-region table parser (which silently mis-identifies a subregion's header for some columns), add a one-time historical backfill that crawls STEG's own paginated news archive to seed the clustering pipeline with real historical notices, and move the scrape cron from every 6 hours to once daily.

**Architecture:** The header-detection fix is a self-contained change to `steg_scraper._extract_zones`, switching from HTML-bold-based detection to a text-pattern match (subregion headers always start with "جهة"/"ولاية"). The per-notice DB-write logic in `import_official.run()` gets extracted into a shared `process_notice()` function so the live scraper and the new backfill crawler share exactly one code path for turning a scraped notice into DB writes. The backfill crawler (`app/backfill_official.py`) walks STEG's `/fr/news` archive pages, using only stable signals (href shape, page `<title>` tag) rather than guessing at decorative markup, and stops naturally once a page contributes nothing new. It's exposed as a manually-triggered protected endpoint, not a cron job.

**Tech Stack:** Python 3, BeautifulSoup (`bs4`), FastAPI, pytest, existing `libsql_client`-backed `app/db.py`.

---

## Task 1: Fix subregion header detection

**Files:**
- Modify: `app/steg_scraper.py`
- Test: `tests/test_steg_scraper.py` (new file)

**Context:** `_extract_zones` currently finds a `<strong>`/`<b>` tag inside each table `<td>` and trusts its text as the subregion header. STEG doesn't bold consistently — in the notice that surfaced this bug, two of three columns had their *first town name* bolded instead of the real header, which was left as plain text. The fix: subregion headers are a closed vocabulary that always starts with "جهة" or "ولاية" — match that on the cell's first line instead of relying on HTML formatting.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_steg_scraper.py`:

```python
# tests/test_steg_scraper.py
from bs4 import BeautifulSoup

from app import steg_scraper


def _cell_body(cell_html: str):
    """Wrap one <td> in the minimal structure _extract_zones expects
    (it looks for the first <table> inside the given element)."""
    return BeautifulSoup(f"<div><table><tr>{cell_html}</tr></table></div>", "html.parser")


def test_extract_zones_finds_header_when_bolded_correctly():
    # The one column that worked correctly in the wild: header is bolded,
    # towns are not.
    body = _cell_body(
        "<td><strong>جهة الوطن القبلي</strong><br>قليبية<br>حمام لغزاز</td>"
    )
    zones, subregions = steg_scraper._extract_zones(body)
    assert subregions == [{"name": "جهة الوطن القبلي", "zones": ["قليبية", "حمام لغزاز"]}]
    assert zones == ["قليبية", "حمام لغزاز"]


def test_extract_zones_finds_header_even_when_a_town_is_bolded_instead():
    # The bug: header is plain text, but the first town happens to be
    # bolded (STEG's inconsistent formatting). Must not treat the bolded
    # town as the header, and must not drop the real header from the cell.
    body = _cell_body(
        "<td>جهة بنزرت<br><strong>سجنان</strong><br>جومين<br>الماتلين</td>"
    )
    zones, subregions = steg_scraper._extract_zones(body)
    assert subregions == [{"name": "جهة بنزرت", "zones": ["سجنان", "جومين", "الماتلين"]}]
    assert zones == ["سجنان", "جومين", "الماتلين"]


def test_extract_zones_headerless_cell_falls_back_to_all_lines_as_zones():
    # No line matches the جهة/ولاية pattern -- preserve today's fallback
    # behavior (no header, everything is a zone).
    body = _cell_body("<td>قليبية<br>حمام لغزاز</td>")
    zones, subregions = steg_scraper._extract_zones(body)
    assert subregions == [{"name": None, "zones": ["قليبية", "حمام لغزاز"]}]
    assert zones == ["قليبية", "حمام لغزاز"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd app/.. && python3 -m pytest tests/test_steg_scraper.py -v`
Expected: the first two tests FAIL (current code extracts the wrong header or drops the real one); the third PASSES already (no regression there).

- [ ] **Step 3: Implement the fix**

In `app/steg_scraper.py`, add this regex near the other module-level regexes (right after `TITLE_RE`):

```python
SUBREGION_HEADER_RE = re.compile(r"^(?:جهة|ولاية)\s+\S")
```

Replace `_extract_zones` (currently lines 103-122) with:

```python
def _extract_zones(body) -> tuple:
    subregions = []
    table = body.select_one("table")
    if table:
        for cell in table.select("td"):
            lines = [
                _clean_zone_line(line)
                for line in cell.get_text("\n", strip=True).split("\n")
            ]
            lines = [l for l in lines if l]
            header = None
            if lines and SUBREGION_HEADER_RE.match(lines[0]):
                header = lines[0]
                lines = lines[1:]
            if header or lines:
                subregions.append({"name": header, "zones": lines})
        flat = [z for sub in subregions for z in sub["zones"]]
        if flat:
            return flat, subregions

    zones = [li.get_text(strip=True) for li in body.select("li") if li.get_text(strip=True)]
    return zones, subregions
```

This removes the `<strong>`/`<b>` lookup entirely — the header is now identified purely from text content, regardless of which line (if any) STEG happened to bold.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_steg_scraper.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python3 -m pytest -q`
Expected: all existing tests still pass (this change only affects `_extract_zones`'s internal header-detection logic, not its return shape).

- [ ] **Step 6: Commit**

```bash
git add app/steg_scraper.py tests/test_steg_scraper.py
git commit -m "fix: detect subregion headers by text pattern, not HTML bold tags"
```

---

## Task 2: Extract shared `process_notice()` helper

**Files:**
- Modify: `app/import_official.py`
- Test: `tests/test_import_official_cooccurrence.py`

**Context:** `import_official.run()` currently inlines the per-notice DB-write logic (upsert notice, resolve+dedupe localities, bump locality counts, bump co-occurrence pairs) inside its loop. The backfill crawler (Task 4) needs to do exactly the same thing per notice. Extract it into a standalone function so there's exactly one place that decides how a scraped notice becomes DB writes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_import_official_cooccurrence.py` (append at the end of the file):

```python
def test_process_notice_upserts_and_records_cooccurrence():
    notice = {
        "id": "pn1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": ["Dekka", "Tozeur"],
        "subregions": [], "raw_text": "raw",
    }
    import_official.process_notice(notice, "2026-07-23T18:00:00+00:00")

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    assert rows[("Dekka", "Tozeur")] == 1
    assert db.count_official_notices() == 1
    assert notice["scraped_at"] == "2026-07-23T18:00:00+00:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_import_official_cooccurrence.py::test_process_notice_upserts_and_records_cooccurrence -v`
Expected: FAIL with `AttributeError: module 'app.import_official' has no attribute 'process_notice'`

- [ ] **Step 3: Implement the extraction**

Replace `app/import_official.py`'s `run()` function (currently lines 33-55) with:

```python
def process_notice(notice: dict, now: str) -> None:
    """Upsert one scraped notice and update the derived locality/co-occurrence
    tables from it. Shared by the live scrape (run(), below) and the
    historical backfill (app/backfill_official.py) -- this is the one place
    that decides how a notice turns into DB writes."""
    notice["scraped_at"] = now
    db.upsert_official_notice(notice)

    localities = sorted(set(_localities_in_notice(notice)))
    for locality in localities:
        db.increment_locality_notice_count(locality)
    for a, b in itertools.combinations(localities, 2):
        db.increment_cooccurrence(a, b, seen_at=now)


def run(verbose: bool = True) -> int:
    db.init_db()
    notices = steg_scraper.scrape_current_notices()
    now = datetime.now(timezone.utc).isoformat()
    for n in notices:
        process_notice(n, now)
        if verbose:
            print(f"  upserted: {n['title']}")
    if verbose:
        if not notices:
            print("Done. 0 notice(s) currently on STEG's homepage "
                  "(this is normal when no cuts are announced right now).")
        print(f"Done. {len(notices)} notice(s) processed, "
              f"{db.count_official_notices()} total in DB.")
    return len(notices)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_import_official_cooccurrence.py -v`
Expected: all tests in this file PASS, including the new one.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python3 -m pytest -q`
Expected: all tests pass — `run()`'s external behavior is unchanged, only its internals were refactored.

- [ ] **Step 6: Commit**

```bash
git add app/import_official.py tests/test_import_official_cooccurrence.py
git commit -m "refactor: extract process_notice() so backfill can reuse the scrape write path"
```

---

## Task 3: Add `db.official_notice_exists()` helper

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_db_official_notices.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_official_notices.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_db_official_notices.py -v`
Expected: FAIL with `AttributeError: module 'app.db' has no attribute 'official_notice_exists'`

- [ ] **Step 3: Implement the helper**

In `app/db.py`, add this function right after `count_official_notices()` (currently ending at line 192):

```python
def official_notice_exists(notice_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM official_notices WHERE id = ?", [notice_id]
        ).fetchone()
        return row is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_db_official_notices.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db_official_notices.py
git commit -m "feat: add db.official_notice_exists() helper"
```

---

## Task 4: Build the historical backfill crawler

**Files:**
- Create: `app/backfill_official.py`
- Modify: `app/steg_scraper.py` (add title extraction to `parse_notice_detail`)
- Test: `tests/test_backfill_official.py` (new file)
- Test: `tests/test_steg_scraper.py` (append)

**Context:** STEG's own `/fr/news` archive lists past news items across many pages (confirmed live: page 1 onward is full of historical outage notices). Rather than guessing at the archive listing's decorative CSS classes, the crawler only relies on two stable signals: the shape of notice URLs (`/fr/news/<slug>`) to discover candidate links, and each detail page's own `<title>` tag (confirmed live against `steg.com.tn/fr/news/denonciation-corruption` → `<title>Dénonciation Corruption | Société Tunisienne de l'Electricité et du Gaz</title>`, i.e. Drupal always renders "{node title} | {site name}") to get the notice's real title text — the same title format `TITLE_RE` already parses.

- [ ] **Step 1: Write the failing test for title extraction**

Append to `tests/test_steg_scraper.py`:

```python
def test_parse_notice_detail_extracts_title_from_page_title_tag(monkeypatch):
    html = """
    <html><head><title>إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 20/07/2026 | Société Tunisienne de l'Electricité et du Gaz</title></head>
    <body><div class="field-name-body"><div class="field-item">
      خلال الفترة بين 11:00 صباحا على مستوى المناطق التالية:
      <ul><li>Dekka</li></ul>
    </div></div></body></html>
    """
    monkeypatch.setattr(steg_scraper, "fetch", lambda url: BeautifulSoup(html, "html.parser"))

    detail = steg_scraper.parse_notice_detail("http://example.test/notice")

    assert detail["title"] == "إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 20/07/2026"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_steg_scraper.py::test_parse_notice_detail_extracts_title_from_page_title_tag -v`
Expected: FAIL with `KeyError: 'title'`

- [ ] **Step 3: Add title extraction to `parse_notice_detail`**

In `app/steg_scraper.py`, add this helper right before `parse_notice_detail`:

```python
def _page_title(soup) -> str:
    """The node's own title, as rendered in <title>. Drupal 7 (confirmed
    live against this site) renders content pages as
    "{node title} | {site name}" -- split off the site-name suffix."""
    tag = soup.select_one("title")
    if not tag:
        return ""
    return tag.get_text(strip=True).split(" | ")[0].strip()
```

Replace `parse_notice_detail` (currently lines 125-144) with:

```python
def parse_notice_detail(url: str) -> dict:
    soup = fetch(url)
    title = _page_title(soup)
    body = soup.select_one(".field-name-body .field-item")
    if not body:
        return {"title": title, "raw_text": None, "zones": [], "subregions": [], "time_window_sentence": None}

    zones, subregions = _extract_zones(body)
    raw_text = body.get_text("\n", strip=True)

    time_window_sentence = None
    m = re.search(r"خلال\s+(.+?)(?:،|,)?\s*على مستوى المناطق التالية", raw_text, re.DOTALL)
    if m:
        time_window_sentence = m.group(1).strip()

    return {
        "title": title,
        "raw_text": raw_text,
        "zones": zones,
        "subregions": subregions,
        "time_window_sentence": time_window_sentence,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_steg_scraper.py -v`
Expected: all tests PASS. Also run `python3 -m pytest -q` for the full suite — `scrape_current_notices()` builds its own dict from `detail[...]` keys explicitly, so the new `"title"` key is additive and harmless there.

- [ ] **Step 5: Commit**

```bash
git add app/steg_scraper.py tests/test_steg_scraper.py
git commit -m "feat: extract notice title from detail page's own <title> tag"
```

- [ ] **Step 6: Write the failing tests for the crawler**

Create `tests/test_backfill_official.py`:

```python
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
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_backfill_official.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.backfill_official'`

- [ ] **Step 8: Implement the crawler**

Create `app/backfill_official.py`:

```python
"""
One-time (or occasionally re-run) historical backfill for STEG outage notices.

STEG's own site keeps a paginated archive of past news items at
/fr/news, /fr/news?page=1, /fr/news?page=2, ... (confirmed live: page=1
onward is full of historical outage notices, several per day). This module
crawls that archive, going back further with each page, until a full page
turns up nothing new (everything on it is already known to this crawl or
already in the DB) -- making repeated runs cheap and safe. Hard-capped at
max_pages regardless, so a future site change can't cause a runaway crawl.

Run manually (NOT on a schedule) via:
    python3 -m app.backfill_official
or against the deployed app:
    curl -X POST "$APP_URL/api/internal/backfill" -H "X-Cron-Secret: $CRON_SECRET"
"""

import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from . import db
from . import import_official
from . import steg_scraper

ARCHIVE_URL = f"{steg_scraper.BASE_URL}/fr/news"
PAGE_DELAY_SECONDS = 1.5
DEFAULT_MAX_PAGES = 100

NEWS_HREF_RE = re.compile(r"^/fr/news/[^?]+$")


def _archive_page_links(page: int) -> list:
    """Every distinct /fr/news/<slug> URL found on one archive listing page.
    page=0 is https://www.steg.com.tn/fr/news itself, page=1 is ?page=1,
    etc. -- matches Drupal's own pager numbering."""
    url = ARCHIVE_URL if page == 0 else f"{ARCHIVE_URL}?page={page}"
    soup = steg_scraper.fetch(url)
    hrefs = set()
    for a in soup.select("a[href]"):
        path = a["href"].replace(steg_scraper.BASE_URL, "")
        if NEWS_HREF_RE.match(path):
            hrefs.add(urljoin(steg_scraper.BASE_URL, path))
    return sorted(hrefs)


def crawl_archive(max_pages: int = DEFAULT_MAX_PAGES, verbose: bool = True) -> int:
    """Crawl the archive page by page, importing any outage notice not
    already known, until a page contributes nothing new or max_pages is
    reached. Returns the number of newly-imported notices."""
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    seen_ids = set()

    for page in range(max_pages):
        links = _archive_page_links(page)
        new_links = [
            url for url in links
            if steg_scraper.slugify_id(url) not in seen_ids
            and not db.official_notice_exists(steg_scraper.slugify_id(url))
        ]

        if not new_links:
            if verbose:
                print(f"  page {page}: nothing new, stopping.")
            break

        for url in new_links:
            notice_id = steg_scraper.slugify_id(url)
            seen_ids.add(notice_id)
            detail = steg_scraper.parse_notice_detail(url)
            title = detail.get("title") or ""
            if steg_scraper.NOTICE_TITLE_MARKER not in title:
                continue  # not an outage notice -- e.g. recruitment/tender news
            m = steg_scraper.TITLE_RE.search(title)
            notice = {
                "id": notice_id,
                "title": title,
                "url": url,
                "region": m.group("region").strip() if m else None,
                "notice_date": m.group("date") if m else None,
                "notice_time": m.group("time") if m else None,
                "time_window_sentence": detail["time_window_sentence"],
                "zones": detail["zones"],
                "subregions": detail["subregions"],
                "raw_text": detail["raw_text"],
            }
            import_official.process_notice(notice, now)
            imported += 1
            if verbose:
                print(f"  imported: {title}")

        time.sleep(PAGE_DELAY_SECONDS)

    if verbose:
        print(f"Done. {imported} new notice(s) imported, {db.count_official_notices()} total in DB.")
    return imported


if __name__ == "__main__":
    try:
        crawl_archive()
    except steg_scraper.FetchError as e:
        print(f"\n⚠ {e}\n\nBackfill stopped early -- whatever was imported before "
              f"the failure is still saved. Try again later.")
        sys.exit(1)
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_backfill_official.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 10: Run the full suite to check for regressions**

Run: `python3 -m pytest -q`
Expected: all tests pass.

- [ ] **Step 11: Commit**

```bash
git add app/backfill_official.py tests/test_backfill_official.py
git commit -m "feat: paginated historical backfill crawler for STEG's news archive"
```

---

## Task 5: Expose backfill as a protected internal endpoint

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_app_internal_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_internal_endpoints.py`:

```python
def test_internal_backfill_requires_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    resp = client.post("/api/internal/backfill")
    assert resp.status_code == 401


def test_internal_backfill_succeeds_with_correct_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    monkeypatch.setattr(app_module.backfill_official, "crawl_archive", lambda verbose=True: 5)
    resp = client.post("/api/internal/backfill", headers={"X-Cron-Secret": "test-secret"})
    assert resp.status_code == 200
    assert resp.json()["notices_processed"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_app_internal_endpoints.py::test_internal_backfill_requires_secret -v`
Expected: FAIL with 404 (route doesn't exist yet) instead of the expected 401.

- [ ] **Step 3: Implement the endpoint**

In `app/main.py`, add `backfill_official` to the existing import block (currently lines 25-28):

```python
from . import backfill_official
from . import cluster_inference
from . import db
from . import import_official
from .governorates import GOVERNORATE_NAMES, GOVERNORATES
```

Add a response model right after `ScrapeResult` (currently lines 63-65):

```python
class BackfillResult(BaseModel):
    notices_processed: int
    total_in_db: int
```

Add the endpoint right after `internal_scrape` (currently ending at line 179, before `internal_recluster`):

```python
@app.post("/api/internal/backfill", response_model=BackfillResult, dependencies=[Depends(verify_cron_secret)])
def internal_backfill():
    try:
        count = backfill_official.crawl_archive(verbose=False)
    except backfill_official.steg_scraper.FetchError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backfill job failed: {e}")
    return BackfillResult(notices_processed=count, total_in_db=db.count_official_notices())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_app_internal_endpoints.py -v`
Expected: all tests PASS, including the two new ones.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python3 -m pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_app_internal_endpoints.py
git commit -m "feat: add protected /api/internal/backfill endpoint"
```

---

## Task 6: Move scrape cadence from 6h to 24h

**Files:**
- Modify: `.github/workflows/scrape.yml`

**Context:** Scrape moves to once daily at 02:00 UTC, 30 minutes before the existing daily recluster job at 02:30 UTC, so recluster always has same-day fresh data. This also matters less now that backfill exists to seed history, and reduces STEG-site load from 4x/day to 1x/day.

- [ ] **Step 1: Update the workflow file**

Replace the full contents of `.github/workflows/scrape.yml` with:

```yaml
name: Scrape and recluster

on:
  schedule:
    - cron: "0 2 * * *"  # once daily at 02:00 UTC -- scrape
    - cron: "30 2 * * *"  # once daily at 02:30 UTC -- recluster
  workflow_dispatch: {}    # allow manual runs from the Actions tab

jobs:
  scrape:
    if: github.event.schedule == '0 2 * * *' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    steps:
      - name: Hit /api/internal/scrape
        run: |
          curl --fail-with-body --max-time 600 -X POST "${{ secrets.APP_URL }}/api/internal/scrape" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" \
            -w "\nHTTP %{http_code} in %{time_total}s\n"

  recluster:
    if: github.event.schedule == '30 2 * * *'
    runs-on: ubuntu-latest
    steps:
      - name: Hit /api/internal/recluster
        run: |
          curl --fail-with-body --max-time 600 -X POST "${{ secrets.APP_URL }}/api/internal/recluster" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" \
            -w "\nHTTP %{http_code} in %{time_total}s\n"
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/scrape.yml
git commit -m "chore: reduce scrape cadence from every 6h to once daily"
```

---

## Task 7: Full verification + live backfill smoke check

**Files:** none (verification only)

- [ ] **Step 1: Run the complete test suite**

Run: `python3 -m pytest -v`
Expected: every test passes (this project's suite plus everything added in Tasks 1-5), no warnings turned errors.

- [ ] **Step 2: Local smoke test of the backfill endpoint against a throwaway local DB**

```bash
export TURSO_DATABASE_URL="file:/tmp/backfill_smoke.db"
rm -f /tmp/backfill_smoke.db
export CRON_SECRET="smoke-test-secret"
python3 -m uvicorn app.main:app --port 8199 &
SERVER_PID=$!
sleep 3
curl -s -X POST "http://127.0.0.1:8199/api/internal/backfill" -H "X-Cron-Secret: smoke-test-secret"
echo
kill $SERVER_PID
```

Expected: a JSON response like `{"notices_processed": N, "total_in_db": N}` with `N` clearly greater than 0 — this hits the *real* STEG site, so it validates the `_page_title()` title-tag assumption (Task 4, Step 3) against production, not just mocks. If `N` is 0, inspect one real notice detail page's raw `<title>` tag directly (e.g. via browser dev tools) and adjust `_page_title()` accordingly before deploying.

- [ ] **Step 3: Deploy and run the backfill once against production**

Once Tasks 1-6 are pushed and Render has redeployed:

```bash
curl -X POST "$APP_URL/api/internal/backfill" -H "X-Cron-Secret: $CRON_SECRET"
```

Then check `/api/model-status` to confirm `notices_so_far` and `localities_so_far` have jumped well past the data floor (`notices_needed: 30`, `localities_needed: 10`), and that `/model.html` now renders real clusters after the next `/api/internal/recluster` run.
