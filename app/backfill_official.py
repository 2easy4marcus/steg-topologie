"""
One-time (or occasionally re-run) historical backfill for STEG outage notices.

STEG's own site keeps a paginated archive of past news items at
/fr/news, /fr/news?page=1, /fr/news?page=2, ... ordered newest-first
(confirmed live: ~10 pages, with pages 2+ holding progressively older
outage notices). This module walks that archive from page 0 down to the end
of pagination, importing every outage notice not already in the DB.

Crucially, it walks the WHOLE archive each run rather than stopping at the
first already-known page: the daily scraper already owns the newest notices,
so the shallow pages are routinely fully known while the unseen history sits
further down. Already-known notices just get their detail fetch skipped.
The crawl stops only when a page offers no link it hasn't already walked
past this run (pagination exhausted), and is hard-capped at max_pages so a
future site change can't cause a runaway crawl.

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
    already in the DB, until pagination is exhausted or max_pages is reached.
    Returns the number of newly-imported notices.

    An already-known or non-outage page does not end the crawl -- see the
    module docstring for why that distinction matters.

    If on_progress is given, it is called as on_progress(page,
    fresh_links_count, imported_so_far) twice per page that has fresh links:
    once right after they're found (before processing), and once after the
    page's per-notice loop finishes. It is not called for the final
    pagination-exhausted page."""
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    evidence_changed = False
    seen_ids = set()

    for page in range(max_pages):
        links = _archive_page_links(page)

        # Two DIFFERENT questions, which earlier versions of this function
        # wrongly conflated:
        #
        #   1. "Have I already seen this link during THIS crawl?" (seen_ids)
        #      -> the only sound signal that pagination is exhausted. If a
        #         page offers nothing we haven't already walked past, we've
        #         either run off the end of the archive or Drupal is handing
        #         back the last page again. Stop.
        #
        #   2. "Is this notice already in the DB from a PREVIOUS run?"
        #      (db.official_notice_exists) -> only a reason to skip the
        #      expensive detail fetch for that one notice. NOT a reason to
        #      stop crawling.
        #
        # Conflating them broke production twice: STEG's archive is ordered
        # newest-first and the daily scraper already owns the newest notices,
        # so pages 0-1 are typically fully known while pages 2-9 hold the
        # unseen history this backfill exists to collect. Treating "this page
        # is already in the DB" as "we've caught up" made the crawl quit at
        # page 1 and never reach any real history.
        fresh_links = [
            url for url in links
            if steg_scraper.slugify_id(url) not in seen_ids
        ]

        if not fresh_links:
            if verbose:
                print(f"  page {page}: no links we haven't already walked -- "
                      f"end of archive, stopping.")
            break

        if verbose:
            print(f"  page {page}: {len(fresh_links)} link(s) to check...")

        if on_progress is not None:
            on_progress(page, len(fresh_links), imported)

        imported_before = imported
        for url in fresh_links:
            notice_id = steg_scraper.slugify_id(url)
            seen_ids.add(notice_id)
            if db.official_notice_exists(notice_id):
                continue  # already have it -- skip the fetch, keep crawling
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
            on_progress(page, len(fresh_links), imported)

        # A page that yielded no imports is NOT a stop signal -- it just means
        # everything on it was either already in the DB or wasn't an outage
        # notice. Deeper pages can still hold unseen history. Only the
        # pagination-exhausted check above stops the crawl; max_pages is the
        # hard backstop.
        if imported_before == imported and verbose:
            print(f"  page {page}: nothing to import here, continuing deeper.")

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
    started_at = datetime.now(timezone.utc).isoformat()
    db.start_ingestion_run(job_id, "backfill", started_at)
    observability.record_job_event(
        job_id, "job_started", occurred_at=started_at
    )
    progress = {"last_page": -1, "links": 0}

    def persist_progress(page, new_links_count, imported_so_far):
        if page != progress["last_page"]:
            progress["last_page"] = page
            progress["links"] += new_links_count
        db.update_ingestion_run(
            job_id,
            current_page=page,
            pages_scanned=page + 1,
            links_discovered=progress["links"],
            notices_imported=imported_so_far,
            last_progress_at=datetime.now(timezone.utc).isoformat(),
        )
        observability.record_job_event(
            job_id,
            "page_completed",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            current_page=page,
        )
        if on_progress is not None:
            on_progress(page, new_links_count, imported_so_far)

    try:
        imported = _crawl_archive_unlocked(
            max_pages, verbose, persist_progress
        )
        finished_at = datetime.now(timezone.utc).isoformat()
        db.finish_ingestion_run(
            job_id,
            "completed",
            finished_at,
        )
        observability.record_job_event(
            job_id,
            "job_completed",
            occurred_at=finished_at,
        )
        observability.cleanup_after_scheduled_job(
            succeeded=True,
            now=finished_at,
        )
        return imported
    except Exception as exc:
        db.finish_ingestion_run(
            job_id,
            "failed",
            datetime.now(timezone.utc).isoformat(),
            public_error_code=(
                "steg_http_error"
                if isinstance(exc, steg_scraper.FetchError)
                else "job_failed"
            ),
            internal_error_detail=str(exc),
        )
        observability.record_job_event(
            job_id,
            "job_failed",
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        raise
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
