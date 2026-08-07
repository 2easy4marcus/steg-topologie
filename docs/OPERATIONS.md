# Operations tracing

The public pipeline page at `/ops.html` shows sanitized progress. Detailed job
timelines are available only through the protected operations API. Never put
the operations secret in HTML, frontend JavaScript, screenshots, tickets, or
logs.

## Configuration

Generate a separate random value locally:

```bash
openssl rand -hex 32
```

Store it as `OPS_SECRET` in the Render service environment. It must differ
from `CRON_SECRET`. Keep the value in a password manager and in a local
Postman environment or shell session only.

For curl, set local variables without committing their values:

```bash
export APP_URL="https://<your-app>.onrender.com"
export OPS_SECRET="<value-from-password-manager>"
curl --fail-with-body \
  --header "X-Ops-Secret: ${OPS_SECRET}" \
  "${APP_URL}/api/internal/ops/jobs?limit=20"
```

Use an ID returned by that response to inspect the safe summary and timeline:

```bash
export JOB_ID="<job-id>"
curl --fail-with-body \
  --header "X-Ops-Secret: ${OPS_SECRET}" \
  "${APP_URL}/api/internal/ops/jobs/${JOB_ID}"
curl --fail-with-body \
  --header "X-Ops-Secret: ${OPS_SECRET}" \
  "${APP_URL}/api/internal/ops/jobs/${JOB_ID}/events?limit=200"
```

In Postman, create private environment variables named `app_url`,
`ops_secret`, and `job_id`. Set the header `X-Ops-Secret` to
`{{ops_secret}}`. Do not save a populated environment to the repository or
share it as an exported JSON file.

The API returns 503 when `OPS_SECRET` is not configured, 401 when the header
is missing or wrong, 404 for an unknown job, and 422 for an invalid cursor or
an excessive limit. Job pages are limited to 100 rows and event pages to 500.

`X-Ops-Secret` protects three more things beyond the job routes above:

| Route | What it gives you |
|---|---|
| `GET /api/internal/ops/summary` | Bounded in-process request metrics |
| `GET /api/internal/openapi.json` | The internal API schema |
| `/api/internal/ops/jobs…` | The job and event timelines above |

## Request metrics and what is retained

`GET /api/internal/ops/summary` reports `sample_count`, `p50_ms`, `p95_ms`,
`status_counts` bucketed as 2xx/4xx/5xx, the ten busiest routes, and the fifty
most recent samples.

**It is in-memory and bounded, and it does not survive a restart.** The ring
holds the last 1000 requests (`app/request_metrics.py`); older ones are
dropped. A sample is a method, route template, status, and two durations —
never a query string, a request or response body, or a header. Requests that
matched no route (404s) record the requested path instead of a template, so a
scan of that list shows what someone probed for. Nothing here is written to
the database or to the logs.

So: it answers "is the service slow or erroring *right now*", and it is the
wrong tool for anything historical. Render's free tier sleeps the service,
which empties the ring. For history, use the persisted job records below.

Persisted retention runs only after successful scheduled ingestion: job
summaries are kept for 90 days and sanitized events for 30 days, in batches of
at most 500. A cleanup failure does not fail the job.

## Trace a failure into Render

1. Find the failed job in `GET /api/internal/ops/jobs`.
2. Inspect its safe error code and events.
3. Copy `request_id` from the job or event.
4. Search that exact ID in the Render service logs.
5. Use the structured log fields `route`, `status`, `job_id`, `build_id`, and
   `cluster_run_id` to follow the operation.

The protected API intentionally omits internal exception details. Those stay
in Render and are connected to safe records by request ID.

## Error interpretation

| Signal | Area | First check |
|---|---|---|
| `steg_http_error` | Scraper/network | STEG reachability and the correlated Render request |
| `parse_failed` event | Parser | Parser version and the notice snapshot diagnostics in Render |
| `database_unavailable` | Database | Turso availability and service credentials |
| failed build event/status | Evidence build | Build ID, validation event, and stale parse count |
| failed cluster request/run | Clustering | Active build ID, readiness response, and cluster-run ID |
| `job_failed` | Generic scheduled job | Request ID and structured Render error |

Public messages and codes are deliberately broad. Do not add arbitrary
exception text to them.

## Rotate the operations secret

1. Generate a new random value locally.
2. Replace `OPS_SECRET` in Render and redeploy/restart the service.
3. Update the private Postman or password-manager value.
4. Confirm the old value returns 401 and the new value returns 200.
5. Remove the old value from local shell history and any temporary clipboard
   manager history.

Rotation does not require changing `CRON_SECRET`.

## Deploying the scoped evidence model (parser version 3)

Parser version 3 records which source table cell each locality came from, and
evidence builds now derive everything from a pinned per-build snapshot. Two
consequences on the deploy that first ships it, in this order:

1. **Reparse before trusting the first build.** Run

   ```bash
   python -m app.reparse_snapshots            # dry run, prints the count
   python -m app.reparse_snapshots --apply
   ```

   Parses written by version 2 have no cell ordinals. Until they are
   reparsed, every such notice is treated as one whole-notice scope at
   `notice_fallback` confidence (0.35), which pairs localities across cell
   boundaries exactly as version 1 did — down-weighted, but still inferred.
   Reparse reconstructs the boundaries from the stored subregion headings.

2. **Expect one unready window.** Readiness metrics read the pinned snapshot,
   which no pre-existing build has. Until the next evidence build completes,
   `/api/model-readiness` reports `valid_notices`, `distinct_outage_dates`,
   and `active_ok_ratio` as 0, and `POST /api/internal/recluster` returns
   `insufficient_data`. Scheduled ingestion rebuilds automatically; to close
   the window immediately, run a rebuild rather than waiting for a scrape.

## Sensitive-response checklist

Before deployment, inspect public and protected operations responses and
confirm they contain none of the following:

- request or response bodies;
- `Authorization`, `X-Cron-Secret`, or `X-Ops-Secret` values;
- Turso URL or token;
- query parameter values copied from requests;
- citizen comments, report text, or personal information;
- raw STEG HTML;
- arbitrary exception messages or stack traces.

Also confirm `/ops.html` neither requests `/api/internal/` nor provides a
secret-entry field. Request IDs, timestamps, bounded counters, artifact IDs,
approved event messages, and stable public error codes are allowed.

## API contract artifacts and smoke tests

The public and internal contracts are split, and the public one is an explicit
allow-list rather than whatever FastAPI happens to expose. FastAPI's automatic
`openapi_url`, `docs_url`, and `redoc_url` are all disabled; `/openapi.json`
and `/docs` are hand-written routes serving the allow-listed schema, and
`/redoc` does not exist.

| Artifact | Where | Contains |
|---|---|---|
| Public schema, served | `GET /openapi.json` | Exactly `/api/status`, `/api/model-readiness`, `/api/stats` |
| Public schema, committed | `build/openapi-public.json` | The same, byte-deterministic |
| Internal schema | `GET /api/internal/openapi.json` (`X-Ops-Secret`) | `/api/internal/*` only |
| Public Postman collection | `postman/tunisia-outage-tracker.postman_collection.json` | Three 200-expecting requests |
| Security-smoke collection | `postman/tunisia-outage-tracker-security-smoke.postman_collection.json` | Unauthenticated `/api/internal/ops/summary`, expects 401 |
| Postman environment | `postman/environment.example.json` | `baseUrl` and an **empty** `opsSecret` |

Regenerate after any change to a public route. Both steps are deterministic,
so a dirty `git status` afterwards means the committed artifacts were stale:

```bash
npm install                      # once, for openapi-to-postmanv2
npm run export:openapi
npm run generate:postman
git status --short build postman
```

Run the smoke tests against a live stack. The second collection is the one
that matters for the boundary — it asserts that the internal summary refuses
an unauthenticated caller:

```bash
docker compose up -d --build
docker compose ps                # app must be "healthy", not just "running"
npm run smoke                    # both collections, failure propagates
```

Zero failed assertions is the pass condition. `npm run smoke` sequences the
two collections with `&&`, so a failure in the first stops the run.

The example environment ships `opsSecret` empty on purpose, and
`tests/test_openapi_boundaries.py` fails if a real secret ever lands in any
committed artifact.

## Rollback

Every activation is last-known-good: nothing is unseated until its replacement
is complete and validated, so a failed step leaves production exactly as it
was. That is asserted in `tests/test_evidence_atomicity.py`, not just
intended.

| Went wrong | What is still serving | How to recover |
|---|---|---|
| Scrape or parse failed | The previously active parse per notice | Re-run; a failed parse can never replace a valid one |
| Evidence build failed | The previously active build | Re-run `python -m app.rebuild_evidence --apply` |
| Cluster run failed, or validation refused it | The previously active cluster run | Re-run the recluster once readiness passes |
| A specific notice parsed wrongly | — | `python -m app.rollback_notice NOTICE_ID PARSE_ID --reason "..."` (preview), then `--apply` |

A cluster run will not activate unless four things hold at once: the run is
completed, its build is the currently active build, a completed validation run
exists for it, and a `published` decision is stored. A refusal is recorded as
a `cluster_activation_refused` job event before the error is re-raised, so it
is visible in the ops timeline rather than only in a stack trace.

To go back to an earlier *model configuration* rather than an earlier run,
change `app/model/config.py` and **bump `CONFIG.version`**. Stored quality
gates and publication decisions are keyed on that string; reusing it silently
makes old and new decisions look comparable when they are not.

## Bootstrapping a database

Use `init_db()`. Migrations 0002 and 0004 `ALTER` tables owned by the base
schema (`SCHEMA_STATEMENTS`), so `migrations.apply_all()` on its own no longer
builds a database from nothing. `init_db()` creates the schema and then
applies the migrations in order; it is the only supported bootstrap.

Migrations are checksummed and recorded. A migration file edited after it was
applied is refused rather than re-run — to change applied behaviour, add a new
migration.
