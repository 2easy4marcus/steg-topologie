# STEG notice parsing fix + historical backfill — design

Date: 2026-07-26

## Problem

Two related production issues surfaced after the first real days of running the deployed tracker:

1. **Multi-region table parsing is wrong for some columns.** STEG occasionally publishes one notice covering several macro-regions at once (e.g. "جهة الشمال"), rendered as an HTML table with one column per sub-region. Our parser (`steg_scraper._extract_zones`) identifies each column's sub-region header by looking for a `<strong>`/`<b>` tag inside the cell. STEG's editors don't bold consistently: in the notice that surfaced this bug, the "جهة الوطن القبلي" column had its header correctly bolded, but the "جهة زغوان" and "جهة بنزرت" columns instead had their *first town name* bolded, with the real header left as plain text. Our parser then treated that first town as the header and silently dropped it from the zone list, corrupting both the displayed subregion name and the locality/co-occurrence data derived from it.

2. **Not enough accumulated data for the clustering pipeline.** The GitHub Actions cron has only been running correctly (bug-free, no failed runs) for a couple of days, so the co-occurrence graph doesn't yet clear the data floor (`MIN_NOTICES=30`, `MIN_LOCALITIES=10`) needed for `/api/internal/recluster` to produce clusters. Investigation (live fetch of `steg.com.tn/fr/news` and its pagination) confirms STEG's own site keeps a paginated archive of past notices — `?page=1` onward is full of historical `إشعار بانقطاع الكهرباء` notices, roughly 7–14 per day (one per macro-region, at two daily time slots), going back through at least 9 pages at time of writing. This is recoverable without waiting weeks for the live cron to accumulate the same volume.

## Part 1 — Fix subregion header detection

**Current logic** (`app/steg_scraper.py::_extract_zones`): for each `<td>` in the notice's table, look for a `<strong>`/`<b>` child; if found, its text is the header and gets excluded from the zone list; if not found, every line in the cell is treated as a zone.

**Fix:** Tunisia's region/subregion names are a closed, known vocabulary and always start with "جهة" or "ولاية" (visible today in `REGION_OPTIONS` and in every subregion label STEG has used: "جهة بنزرت", "جهة زغوان", "جهة الوطن القبلي", etc.). Replace the bold-tag lookup with a text-pattern check on the cell's **first line**:

- If the first line matches `^(جهة|ولاية)\s+`, it's the header — regardless of whether it's wrapped in `<strong>`/`<b>` — and the remaining lines are zones.
- Otherwise, fall back to today's behavior: no header (`name: None`), every line is a zone. This preserves correct handling of the plain `<ul>` single-region case and any future cell that genuinely has no header.

This removes the dependency on STEG's inconsistent HTML formatting entirely, relying only on the text content, which has been stable.

**Testing:** add a fixture reproducing the exact broken HTML (bold on first town instead of header) alongside the already-working case (bold on real header) and the plain-list case, asserting correct `{name, zones}` output for all three.

## Part 2 — Historical backfill

**Shared processing helper.** Extract the per-notice DB-write logic currently inlined in `import_official.run()`'s loop (upsert notice, resolve+dedupe localities, increment locality counts, increment co-occurrence pairs) into a standalone function, e.g. `import_official.process_notice(notice: dict, now: str)`. Both the live scrape and the new backfill call this, so there's exactly one place that decides how a scraped notice turns into DB writes.

**New module `app/backfill_official.py`:**
- `crawl_archive(max_pages=100) -> int`: iterates `GET /fr/news?page=N` starting at `N=0`, using the existing `fetch()` (retry/backoff already built in) plus a short delay between requests (politeness — this is a small government site).
- For each page: parse the listing for notice links/titles. Live fetches of `/fr/news` and `/fr/news?page=1` confirm the archive lists items with the same title text format as the homepage panel ("إشعار بانقطاع الكهرباء - <region> - <time> <date>") each linking to `/fr/news/<slug>` — the exact CSS selector for the archive's item markup (likely different from the homepage's `.panel`/`a.accordion-toggle` structure) needs to be confirmed against the raw HTML during implementation, the same way `list_homepage_notices()` was originally built.
- Skip notices already in the DB (new `db.official_notice_exists(id) -> bool` helper) — this makes the crawl idempotent and gives the natural stop condition.
- **Stop when a full page yields zero *new* notices** (everything on it was already imported) — meaning we've caught up with previously-backfilled or live-scraped data. Hard-capped at `max_pages` (default 100) regardless, so a future site change can't cause a runaway crawl.
- For each new notice found, call `parse_notice_detail()` + `process_notice()`, same as the live path.
- Returns the count of newly-imported notices.

**New protected endpoint** `POST /api/internal/backfill` in `app/main.py`, gated by the same `verify_cron_secret` dependency as `/api/internal/scrape` and `/api/internal/recluster`. Not wired into the GitHub Actions cron — it's a manual, one-off (or occasionally-rerun) trigger:

```
curl -X POST "$APP_URL/api/internal/backfill" -H "X-Cron-Secret: $CRON_SECRET"
```

Response mirrors `ScrapeResult`'s shape: `{"notices_processed": N, "total_in_db": M}`.

**Testing:** mock the paginated archive fetch (fixture pages with known notices, one page repeating already-seen ids to prove the stop condition fires), assert the crawl stops at the right page and imports the expected count; assert `max_pages` cap is respected with a fixture that never repeats.

## Part 3 — Cron cadence: 6h → 24h

`.github/workflows/scrape.yml`: change the scrape schedule from `"0 */6 * * *"` to once daily. Keep the existing daily recluster cron as-is (`"30 2 * * *"`), but move the scrape to a time that gives recluster fresh data to work with the same day — scrape at `"0 2 * * *"` (02:00 UTC, 30 minutes before recluster). Update the `if:` conditions on both jobs to match the new cron strings.

## Files touched

- `app/steg_scraper.py` — header-detection fix
- `app/import_official.py` — extract `process_notice()` helper
- `app/backfill_official.py` — new module
- `app/db.py` — add `official_notice_exists(id)` helper
- `app/main.py` — new `/api/internal/backfill` endpoint
- `.github/workflows/scrape.yml` — cadence change
- `tests/` — new/updated tests for all of the above

## Out of scope

- Making backfill run automatically/repeatedly on a schedule (explicitly a manual trigger per the design discussion).
- Changing the data-floor thresholds (`MIN_NOTICES`/`MIN_LOCALITIES`) — backfill should clear them on its own given the volume of historical notices confirmed available.
