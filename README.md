# Tunisia Outage Tracker

An interactive local website combining STEG's official electricity outage
notices with crowdsourced reports from visitors (electricity and water),
backed by a SQLite database.

## What's inside

- `app/` — the `app` Python package:
  - `app/main.py` — FastAPI backend + serves the frontend.
  - `app/db.py` — SQLite schema and queries (`tracker.db` is created automatically on first run).
  - `app/steg_scraper.py` — parsing logic for STEG's official notices (steg.com.tn).
  - `app/import_official.py` — run this to pull STEG's current official notices into the database.
  - `app/locality_dedup.py` — resolves raw locality text to a stable canonical name.
  - `app/geocoding.py` — lazy, cached geocoding of locality names via Nominatim.
  - `app/cluster_inference.py` — statistical grid-cluster inference (PPMI + Louvain).
  - `app/governorates.py` — Tunisia's 24 governorates with map coordinates.
- `static/index.html` — the frontend: map, official notices list, crowdsourced reports list, and a report submission form.
- `requirements.txt`

## Setup

```
cd tunisia_outage_tracker
pip install -r requirements.txt
```

## Run it

```
uvicorn app.main:app --reload --port 8010
```

Then open **http://127.0.0.1:8010** in your browser.

The database (`tracker.db`) is created automatically the first time the app
starts — no manual setup needed. If you ever see a database error on
startup, just delete `tracker.db` and restart; it'll be recreated empty.

## Pull in official STEG notices

The website's "Avis officiels STEG" tab reads from the database, not live
from STEG each time. To populate/refresh it:

```
python3 -m app.import_official
```

Run this periodically (cron, Task Scheduler, or Claude's "schedule" skill) to
keep it current — it only adds notices it hasn't already stored, so it's
safe to run often (e.g. every 30–60 minutes).

## Using the site

- **Avis officiels STEG** — official notices, filterable by region (Arabic text, as published by STEG).
- **Signalements citoyens** — crowdsourced reports, filterable by service (electricity/water), status (active/restored), and governorate.
- **Signaler une coupure** — the submission form. No login required, same spirit as community trackers like the one that inspired this (Tunisia Pulse).
- The map shows active-report density per governorate (yellow-ish = mostly electricity, blue = mostly water), clickable for a quick breakdown.

## Known limitations

- **SONEDE (water)**: there's no official on-site source for water-cut notices
  the way there is for STEG electricity — SONEDE's news page covers projects
  and tenders, not per-incident cuts. So the "official" side of this app is
  electricity-only; water coverage relies entirely on crowdsourced reports.
- This runs locally only (`uvicorn` on your machine). If you want it reachable
  from other devices or the internet, it would need to be deployed somewhere
  (Render, Fly.io, a VPS, etc.) — the app is deploy-ready but I haven't set
  up any hosting for it.
- No authentication on report submission — anyone with access to the site can
  post a report, same tradeoff the original community trackers make.

## Grid co-occurrence clusters (new)

This adds a statistical, opt-in "inferred grid clusters" map layer — see
`docs/superpowers/specs/2026-07-24-grid-cooccurrence-clusters-design.md`
for the full design. It requires:

**Environment variables** (set wherever the app is deployed):
- `TURSO_DATABASE_URL` — e.g. `libsql://<your-db>.turso.io`. If unset, the
  app falls back to a local `tracker.db` file (fine for local dev/tests).
- `TURSO_AUTH_TOKEN` — Turso auth token (only needed alongside a real
  `TURSO_DATABASE_URL`).
- `CRON_SECRET` — any random string; must match the `CRON_SECRET` GitHub
  Actions secret below. Without it, `/api/internal/scrape` and
  `/api/internal/recluster` reject all requests with 401.

**GitHub Actions secrets** (repo Settings → Secrets and variables → Actions):
- `APP_URL` — the deployed app's base URL (e.g. `https://your-app.onrender.com`)
- `CRON_SECRET` — must match the env var above

Once both are set, `.github/workflows/scrape.yml` runs hourly (scrape) and
daily (recluster) automatically — no manual steps needed after initial setup.

**Turso setup** (one-time): create a free account at turso.tech, create a
database, and get the URL/token from their dashboard or `turso db show`/
`turso db tokens create` CLI commands.

**Running tests locally:**

```
pip install -r requirements.txt
pytest -v
```

Tests use an isolated temporary local file DB per test (see
`tests/conftest.py`) — they never touch your real `tracker.db`.

**Note on this project's dev sandbox:** if you see `sqlite3.OperationalError:
disk I/O error` (or similar) running the app directly against `tracker.db`
on a network-mounted/FUSE filesystem, that's an environment limitation of
that specific mount, not an app bug — it doesn't affect a normal local
disk or a real deployment.
# Evidence and model APIs

The model is built from versioned, traceable STEG evidence. Public
read-only endpoints:

- `GET /api/status` — active evidence/cluster identifiers and safe health.
- `GET /api/status/ingestion` — persisted scrape/backfill progress.
- `GET /api/model-status` — backward-compatible model summary.
- `GET /api/model-readiness` — model-quality and operational-health signals.
- `GET /api/clusters` — cluster members plus source evidence build metadata.
- `GET /api/edge-evidence?locality_a=...&locality_b=...` — supporting STEG
  notices for an active statistical edge.

Clusters and edges are experimental statistical relationships. They do not
represent confirmed transformers, feeders, or physical grid locations.
