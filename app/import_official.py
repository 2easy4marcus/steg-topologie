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

import itertools
import sys
from datetime import datetime, timezone

from . import db
from . import locality_dedup
from . import steg_scraper


def _localities_in_notice(notice: dict) -> list:
    if notice.get("subregions"):
        raw = [z for sub in notice["subregions"] for z in sub.get("zones", [])]
    else:
        raw = notice.get("zones", [])
    return [locality_dedup.resolve_locality(z) for z in raw if z]


def run(verbose: bool = True) -> int:
    db.init_db()
    notices = steg_scraper.scrape_current_notices()
    now = datetime.now(timezone.utc).isoformat()
    for n in notices:
        n["scraped_at"] = now
        db.upsert_official_notice(n)

        localities = sorted(set(_localities_in_notice(n)))
        for locality in localities:
            db.increment_locality_notice_count(locality)
        for a, b in itertools.combinations(localities, 2):
            db.increment_cooccurrence(a, b, seen_at=now)

        if verbose:
            print(f"  upserted: {n['title']}")
    if verbose:
        if not notices:
            print("Done. 0 notice(s) currently on STEG's homepage "
                  "(this is normal when no cuts are announced right now).")
        print(f"Done. {len(notices)} notice(s) processed, "
              f"{db.count_official_notices()} total in DB.")
    return len(notices)


if __name__ == "__main__":
    try:
        run()
    except steg_scraper.FetchError as e:
        print(f"\n⚠ {e}\n\nNothing was changed in the database. Try again in a few minutes.")
        sys.exit(1)
