# Deployment Guide — Tunisia Outage Tracker

This walks through going from the project folder on your computer to a live, publicly reachable site with automatic hourly scraping. No git repo exists yet on your machine — this starts from scratch.

## 0. Prerequisites

- A GitHub account (for hosting the code + running the free cron via GitHub Actions)
- A Render account (free tier — for hosting the app itself: render.com)
- A Turso account (free tier — for the hosted database: turso.tech)

All three are free for this project's scale.

## 1. Put the project in git and push it to GitHub

From inside the `tunisia_outage_tracker` folder on your computer:

```bash
git init
git add -A
git commit -m "Initial commit"
```

Create a new empty repository on GitHub (github.com → New repository — don't initialize it with a README, since you already have one), then:

```bash
git remote add origin https://github.com/<your-username>/<your-repo-name>.git
git branch -M main
git push -u origin main
```

## 2. Create the Turso database

1. Sign up at turso.tech and install their CLI (`curl -sSfL https://get.tur.so/install.sh | bash`, or see their site for other install methods).
2. Log in: `turso auth login`
3. Create a database: `turso db create tunisia-outage-tracker`
4. Get its connection URL: `turso db show tunisia-outage-tracker --url` — copy this, you'll need it as `TURSO_DATABASE_URL`.
5. Create an auth token: `turso db tokens create tunisia-outage-tracker` — copy this, you'll need it as `TURSO_AUTH_TOKEN`.

Keep both values somewhere safe for the next steps.

## 3. Deploy the app to Render

1. In the Render dashboard: New → Web Service → connect your GitHub repo.
2. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance type:** Free
3. Add environment variables (Render dashboard → your service → Environment):
   - `TURSO_DATABASE_URL` = the URL from step 2
   - `TURSO_AUTH_TOKEN` = the token from step 2
   - `CRON_SECRET` = any random string you make up (e.g. run `openssl rand -hex 32` locally and paste the result) — this is the shared secret that authorizes the scrape/recluster endpoints, so it should be long and random, not something guessable.
   - `OPS_SECRET` = a second, independently generated random string used only
     by operators calling the protected diagnostics API. Never expose it in
     frontend JavaScript and do not reuse `CRON_SECRET`.
4. Deploy. Render will build and give you a URL like `https://tunisia-outage-tracker.onrender.com`.

Note: Render's free tier sleeps the service after 15 minutes of no traffic and wakes it back up on the next request (a few seconds of delay). This is fine here — the DB lives in Turso, not on Render's disk, so nothing is lost between sleeps, and the hourly cron below will also naturally keep it from sleeping for too long at a time.

## 4. Wire up GitHub Actions (the automatic hourly scrape + daily recluster)

The workflow file (`.github/workflows/scrape.yml`) is already in the repo. It just needs two secrets configured on GitHub:

1. On GitHub: your repo → Settings → Secrets and variables → Actions → New repository secret.
2. Add `APP_URL` = your Render URL from step 3 (e.g. `https://tunisia-outage-tracker.onrender.com`, no trailing slash).
3. Add `CRON_SECRET` = the exact same random string you set on Render in step 3.

That's it — the workflow will now run automatically: hourly it hits `/api/internal/scrape`, and once a day it hits `/api/internal/recluster`. You can also trigger it manually any time from the GitHub repo's Actions tab (it has `workflow_dispatch` enabled) to test it immediately rather than waiting for the next scheduled hour.

## 5. Verify it's actually working

- Visit your Render URL in a browser — you should see the site, with the official-notices/reports tabs (likely empty until the first scrape runs).
- After the first scrape (wait up to an hour, or trigger it manually from the Actions tab), reload the site — official notices should start appearing.
- The "Afficher les groupes de réseau inférés (bêta)" map toggle will stay empty until at least 30 notices and 10 distinct localities have accumulated (the data floor described in the design spec) — that'll take some real time of the scraper running, this is expected, not a bug.
- To sanity-check the endpoints directly:
  ```bash
  curl -X POST https://<your-app>.onrender.com/api/internal/scrape -H "X-Cron-Secret: <your-secret>"
  ```
  Should return JSON like `{"notices_processed": N, "total_in_db": N}`, not a 401.

## Ongoing costs

Everything above is free at this project's scale: Render's free web service tier, Turso's free tier (5GB storage, 500M reads/10M writes per month), and GitHub Actions' free minutes for public repos. If the repo is private, GitHub Actions free minutes are limited per month but hourly + daily cron here uses very little of that allowance.
# Evidence pipeline rollout

The evidence migration is dry-run-first. Use this production order:

1. Deploy the schema and application code.
2. Verify `GET /api/status`.
3. Complete the historical backfill and verify `GET /api/status/ingestion`.
4. Preview parser and normalizer migrations:

   ```bash
   python -m app.reparse_snapshots
   ```

5. Apply the reparse:

   ```bash
   python -m app.reparse_snapshots --apply
   ```

6. Preview the evidence rebuild:

   ```bash
   python -m app.rebuild_evidence
   ```

7. Apply and activate the validated build:

   ```bash
   python -m app.rebuild_evidence --apply
   ```

8. Inspect `GET /api/model-readiness`.
9. Trigger reclustering only when model quality and operational health pass.
10. Inspect representative edges through `GET /api/edge-evidence`.

An explicit notice rollback also defaults to a preview:

```bash
python -m app.rollback_notice NOTICE_ID PARSE_ID --reason "reason"
python -m app.rollback_notice NOTICE_ID PARSE_ID --reason "reason" --apply
```

Reparse, rebuild, and rollback failures preserve the previously active parse,
evidence build, and cluster run.

## Operations diagnostics

After deployment, verify the sanitized dashboard at `/ops.html`. Detailed job
summaries and event timelines require `X-Ops-Secret` and are intended for
Postman or curl, not the browser frontend.

Follow [docs/OPERATIONS.md](docs/OPERATIONS.md) for safe environment setup,
request-ID correlation with Render logs, error-code interpretation, secret
rotation, retention, and the sensitive-response verification checklist.
