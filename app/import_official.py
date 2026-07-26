#!/usr/bin/env python3
"""
Fetches STEG's current official outage notices and upserts them into the
SQLite database (tracker.db) used by the website.

Run this periodically (cron / Task Scheduler / Claude's "schedule" skill) to
keep the "Avis officiels" section of the site up to date:

    python3 -m app.import_official

Exits with status 1 (and a short message, not a traceback) if STEG's site
can't be reached after retries -- that's expected to happen occasionally
since it's a small, sometimes-slow government site. Just try again later.
"""

import sys
from datetime import datetime, timezone

from . import db
from . import evidence_pipeline
from . import locality_dedup
from . import observability
from . import steg_scraper


def _localities_in_notice(notice: dict) -> list:
    if notice.get("subregions"):
        raw = [z for sub in notice["subregions"] for z in sub.get("zones", [])]
    else:
        raw = notice.get("zones", [])
    return [locality_dedup.resolve_locality(z) for z in raw if z]


def process_notice(notice: dict, now: str) -> None:
    """Upsert one scraped notice and update the derived locality/co-occurrence
    tables from it. Shared by the live scrape (run(), below) and the
    historical backfill (app/backfill_official.py) -- this is the one place
    that decides how a notice turns into DB writes."""
    notice["scraped_at"] = now
    db.upsert_official_notice(notice)
    return evidence_pipeline.persist_notice(notice, fetched_at=now)


def rebuild_if_changed(changed: bool, now: str, job_id: str | None = None):
    if not changed:
        return None
    return evidence_pipeline.build_model_evidence(
        created_at=now, job_id=job_id
    )


def run(verbose: bool = True) -> int:
    db.init_db()
    job_id, _ = observability.acquire_evidence_job_lock(ttl_minutes=15)
    started_at = datetime.now(timezone.utc).isoformat()
    db.start_ingestion_run(job_id, "scrape", started_at)
    observability.record_job_event(
        job_id, "job_started", occurred_at=started_at
    )
    try:
        notices = steg_scraper.scrape_current_notices()
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        imported = 0
        unchanged = 0
        for n in notices:
            notice_changed = process_notice(n, now)
            changed = notice_changed or changed
            imported += int(notice_changed)
            unchanged += int(not notice_changed)
            observability.record_job_event(
                job_id,
                "notice_imported" if notice_changed else "notice_unchanged",
                occurred_at=now,
            )
            if verbose:
                print(f"  upserted: {n['title']}")
        rebuild_if_changed(changed, now, job_id)
        db.update_ingestion_run(
            job_id,
            links_discovered=len(notices),
            notices_imported=imported,
            notices_unchanged=unchanged,
            last_progress_at=now,
        )
        db.finish_ingestion_run(job_id, "completed", now)
        observability.record_job_event(
            job_id, "job_completed", occurred_at=now
        )
        if verbose:
            if not notices:
                print("Done. 0 notice(s) currently on STEG's homepage "
                      "(this is normal when no cuts are announced right now).")
            print(f"Done. {len(notices)} notice(s) processed, "
                  f"{db.count_official_notices()} total in DB.")
        return len(notices)
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        code = (
            "steg_http_error"
            if isinstance(exc, steg_scraper.FetchError)
            else "job_failed"
        )
        db.finish_ingestion_run(
            job_id,
            "failed",
            finished_at,
            public_error_code=code,
            internal_error_detail=str(exc),
        )
        observability.record_job_event(
            job_id, "job_failed", occurred_at=finished_at
        )
        raise
    finally:
        observability.release_job_lock("evidence-pipeline", job_id)


if __name__ == "__main__":
    try:
        run()
    except steg_scraper.FetchError as e:
        print(f"\n⚠ {e}\n\nNothing was changed in the database. Try again in a few minutes.")
        sys.exit(1)
