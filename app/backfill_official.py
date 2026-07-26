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

        imported_before = imported
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

        # If this page's new links turned out to be entirely non-outage
        # notices (e.g. recruitment/tender news, which the archive mixes in
        # with outage notices), treat it the same as "nothing new" and stop
        # -- otherwise a page full of unrelated news would let the crawl
        # walk on forever without ever finding another real notice.
        if imported == imported_before:
            if verbose:
                print(f"  page {page}: no outage notices among new links, stopping.")
            break

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
