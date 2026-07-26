"""
One-time (or occasionally re-run) historical backfill for STEG outage notices.

STEG's own site keeps a paginated archive of past news items at
/fr/news, /fr/news?page=1, /fr/news?page=2, ... (confirmed live: page=1
onward is full of historical outage notices, several per day). This module
crawls that archive, going back further with each page, until a full page
has zero unseen links (everything on it is already known to this crawl or
already in the DB) -- making repeated runs cheap and safe. A page whose new
links turn out to be non-outage notices (e.g. recruitment/tender news mixed
into the archive) does NOT stop the crawl by itself -- only "nothing left
unseen" does. Hard-capped at max_pages regardless, so a future site change
can't cause a runaway crawl.

Run manually (NOT on a schedule) via:
    python3 -m app.backfill_official
or against the deployed app:
    curl -X POST "$APP_URL/api/internal/backfill" -H "X-Cron-Secret: $CRON_SECRET"
"""

import logging
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from . import db
from . import import_official
from . import observability
from . import steg_scraper

ARCHIVE_URL = f"{steg_scraper.BASE_URL}/fr/news"
PAGE_DELAY_SECONDS = 1.5
DEFAULT_MAX_PAGES = 100

NEWS_HREF_RE = re.compile(r"^/fr/news/[^?]+$")

logger = logging.getLogger(__name__)

_status = {
    "running": False,
    "page": 0,
    "new_links_this_page": 0,
    "imported": 0,
    "total_in_db": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


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


def _crawl_archive_unlocked(
    max_pages: int = DEFAULT_MAX_PAGES,
    verbose: bool = True,
    on_progress=None,
) -> int:
    """Crawl the archive page by page, importing any outage notice not
    already known, until a page contributes nothing new or max_pages is
    reached. Returns the number of newly-imported notices.

    If on_progress is given, it is called as on_progress(page, new_links_count,
    imported_so_far) twice per page that has new links: once right after the
    new links are found (before processing them), and once again after the
    page's per-notice processing loop finishes. It is not called for a page
    with no new links (the "nothing new, stopping" case)."""
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    evidence_changed = False
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

        if verbose:
            print(f"  page {page}: {len(new_links)} new link(s), fetching details...")

        if on_progress is not None:
            on_progress(page, len(new_links), imported)

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
            evidence_changed = (
                import_official.process_notice(notice, now)
                or evidence_changed
            )
            imported += 1
            if verbose:
                print(f"  imported: {title}")

        if on_progress is not None:
            on_progress(page, len(new_links), imported)

        # NOTE: we deliberately do NOT stop just because this page's new
        # links turned out to be entirely non-outage notices (e.g. a
        # recruitment/tender item mixed into the archive). A single stray
        # non-outage link among mostly-already-known content does not mean
        # deeper, never-crawled pages are empty too -- an earlier version of
        # this function stopped on exactly that condition and, in
        # production, halted the entire backfill at page 0 (which is mostly
        # re-covering already-scraped ground) without ever reaching page 1+,
        # where the real historical notices live. The only sound "we've
        # caught up" signal is a page with zero *unseen* links at all (the
        # `if not new_links: break` check above) -- max_pages remains the
        # hard backstop against a genuinely pathological run of pure noise.
        if imported_before == imported and verbose:
            print(f"  page {page}: no outage notices among new links, continuing.")

        time.sleep(PAGE_DELAY_SECONDS)

    import_official.rebuild_if_changed(evidence_changed, now)
    if verbose:
        print(f"Done. {imported} new notice(s) imported, {db.count_official_notices()} total in DB.")
    return imported


def crawl_archive(
    max_pages: int = DEFAULT_MAX_PAGES,
    verbose: bool = True,
    on_progress=None,
) -> int:
    job_id, _ = observability.acquire_evidence_job_lock(ttl_minutes=30)
    try:
        return _crawl_archive_unlocked(max_pages, verbose, on_progress)
    finally:
        observability.release_job_lock("evidence-pipeline", job_id)


def get_status() -> dict:
    """A snapshot of the current/last backfill run's progress. Safe to call
    at any time, including before any run has ever happened (all zero/None
    defaults) or while a run is actively in progress."""
    return dict(_status)


def run_backfill_and_track_status(max_pages: int = DEFAULT_MAX_PAGES) -> None:
    """Run crawl_archive() while updating the module-level _status dict as
    it progresses, so a concurrent request (e.g. a status-polling endpoint)
    can observe live progress. Meant to be scheduled as a background task by
    the HTTP layer -- callers that want a return value or synchronous
    behavior should call crawl_archive() directly instead."""
    _status.update({
        "running": True,
        "page": 0,
        "new_links_this_page": 0,
        "imported": 0,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    })

    def _on_progress(page, new_links_count, imported_so_far):
        _status["page"] = page
        _status["new_links_this_page"] = new_links_count
        _status["imported"] = imported_so_far

    try:
        crawl_archive(max_pages=max_pages, verbose=True, on_progress=_on_progress)
    except Exception as e:
        # This runs in a detached background task -- if we don't log here,
        # an unexpected bug (as opposed to an expected FetchError) vanishes
        # into an opaque string in _status["error"] with no server-side
        # trace to debug it from.
        logger.exception("Backfill run failed")
        _status["error"] = str(e)
    finally:
        _status["running"] = False
        _status["finished_at"] = datetime.now(timezone.utc).isoformat()
        _status["total_in_db"] = db.count_official_notices()


if __name__ == "__main__":
    try:
        crawl_archive()
    except steg_scraper.FetchError as e:
        print(f"\n⚠ {e}\n\nBackfill stopped early -- whatever was imported before "
              f"the failure is still saved. Try again later.")
        sys.exit(1)
