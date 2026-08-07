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

Operations retention runs only after successful scheduled ingestion: job
summaries are retained for 90 days and sanitized events for 30 days. Cleanup
uses batches of at most 500 and a cleanup failure does not fail the job.
