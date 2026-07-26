# Operations Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Postman-like production visibility for scraper, backfill, evidence-build, and clustering jobs without exposing secrets, citizen content, request bodies, or database credentials.

**Architecture:** Keep structured request metadata in Render stdout logs and persist only bounded, sanitized job events in Turso. Expose minimal public health/progress endpoints and protect detailed diagnostic endpoints with a separate operations secret intended for Postman/curl, never frontend JavaScript.

**Depends on:** `2026-07-26-evidence-pipeline-and-model-readiness.md`, especially request IDs, job IDs, stable public error codes, and atomic job locks.

---

## Access model

### Public

```text
GET /api/status
GET /api/status/ingestion
```

Public data may include running state, progress counters, timestamps, safe error codes, request IDs, active build ID, and active cluster-run ID.

### Protected

```text
GET /api/internal/ops/jobs
GET /api/internal/ops/jobs/{job_id}
GET /api/internal/ops/jobs/{job_id}/events
```

Protected endpoints require `X-Ops-Secret`. The secret:

- Is separate from `CRON_SECRET`.
- Exists only in Render environment variables and the operator's Postman/curl environment.
- Is never embedded in HTML or JavaScript.
- Is never written to logs or Turso.

### Never collected

- Request and response bodies.
- Authorization, cron, or operations headers.
- Turso URL or token.
- Query parameter values.
- Citizen report text or personal information.
- Raw STEG HTML in operations events.

---

### Task 1: Extend structured logs with job correlation

**Files:**
- Modify: `app/observability.py`
- Modify: `app/main.py`
- Create: `tests/test_request_logging.py`

- [ ] Preserve the core plan's request log and add job/build correlation fields when the request starts or inspects a long-running job:

```json
{
  "timestamp": "2026-07-26T12:00:00Z",
  "request_id": "req-123",
  "method": "POST",
  "route": "/api/internal/backfill",
  "status": 202,
  "duration_ms": 41.7,
  "job_id": "job-123",
  "build_id": null,
  "cluster_run_id": null
}
```

- [ ] Use route templates, not raw URLs or query strings.

- [ ] Redact all disallowed fields before serialization.

- [ ] Test normal responses, exceptions, 404 routes, Unicode paths, supplied/generated request IDs, and optional job/build/run correlation.

```bash
pytest tests/test_request_logging.py -v
```

- [ ] Commit.

```bash
git add app/observability.py app/main.py tests/test_request_logging.py
git commit -m "feat: correlate operations logs"
```

---

### Task 2: Persist bounded job events

**Files:**
- Modify: `app/db.py`
- Modify: `app/observability.py`
- Modify: `app/import_official.py`
- Modify: `app/backfill_official.py`
- Modify: `app/evidence_pipeline.py`
- Modify: `app/cluster_inference.py`
- Create: `tests/test_job_events.py`

- [ ] Add:

```sql
CREATE TABLE IF NOT EXISTS job_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    public_message TEXT NOT NULL,
    current_page INTEGER,
    request_id TEXT
);
```

- [ ] Permit only these event types:

```text
job_started
page_started
page_completed
notice_imported
notice_unchanged
notice_skipped
notice_failed
parse_failed
build_started
build_validated
build_activated
cluster_started
cluster_activated
job_completed
job_failed
```

- [ ] Generate public messages from approved templates. Never persist arbitrary `str(exc)` as a public message.

- [ ] Avoid one event per SQL statement or locality pair.

- [ ] Verify ordering, redaction, allowed event types, and request-ID correlation.

```bash
pytest tests/test_job_events.py -v
```

- [ ] Commit.

```bash
git add app/db.py app/observability.py app/import_official.py \
  app/backfill_official.py app/evidence_pipeline.py app/cluster_inference.py \
  tests/test_job_events.py
git commit -m "feat: persist sanitized job events"
```

---

### Task 3: Add protected diagnostic APIs

**Files:**
- Modify: `app/main.py`
- Modify: `app/db.py`
- Create: `tests/test_ops_api.py`

- [ ] Add `OPS_SECRET` configuration and constant-time comparison.

- [ ] Return HTTP 503 when the server has no configured operations secret.

- [ ] Add pagination with bounded limits:

```text
GET /api/internal/ops/jobs?limit=20&cursor=eyJpZCI6ImpvYi0xMjMifQ
GET /api/internal/ops/jobs/{job_id}
GET /api/internal/ops/jobs/{job_id}/events?limit=200&cursor=eyJpZCI6ImV2ZW50LTQ1NiJ9
```

- [ ] Maximum limits:

```text
jobs: 100
events: 500
```

- [ ] Return safe fields only. Internal exception text remains in Render logs and is correlated through `request_id`.

- [ ] Verify missing, wrong, and correct secrets; pagination; limit enforcement; unknown job IDs; and response redaction.

```bash
pytest tests/test_ops_api.py -v
```

- [ ] Commit.

```bash
git add app/main.py app/db.py tests/test_ops_api.py
git commit -m "feat: expose protected job diagnostics"
```

---

### Task 4: Build the public pipeline-status page

**Files:**
- Create: `static/ops.html`
- Modify: `static/index.html`
- Modify: `static/model.html`
- Create: `tests/test_ops_page.py`

- [ ] Show only public `/api/status` and `/api/status/ingestion` data.

- [ ] Include cards for:

```text
Latest scrape
Current backfill
Active evidence build
Latest cluster run
Model readiness
Operational health
```

- [ ] Show request IDs so an operator can copy them into Render log search.

- [ ] Poll every 5 seconds while a job runs and every 60 seconds while idle.

- [ ] Do not add a field for entering `OPS_SECRET`.

- [ ] Add links among tracker, model, and pipeline-status pages.

- [ ] Verify loading, empty, running, failed, stale, mobile, keyboard, and screen-reader states.

```bash
pytest tests/test_ops_page.py -v
```

- [ ] Commit.

```bash
git add static/ops.html static/index.html static/model.html tests/test_ops_page.py
git commit -m "feat: add public pipeline status page"
```

---

### Task 5: Add retention and cleanup

**Files:**
- Modify: `app/db.py`
- Modify: `app/observability.py`
- Create: `tests/test_ops_retention.py`

- [ ] Retain:

```text
ingestion job summaries: 90 days
job events: 30 days
```

- [ ] Cleanup runs only after a successful scheduled job.

- [ ] Cleanup deletes in bounded batches of at most 500 rows.

- [ ] Failed cleanup never changes job success and emits a structured Render warning.

- [ ] Verify boundary dates, batching, and failure isolation.

```bash
pytest tests/test_ops_retention.py -v
```

- [ ] Commit.

```bash
git add app/db.py app/observability.py tests/test_ops_retention.py
git commit -m "feat: retain bounded operations history"
```

---

### Task 6: Document Postman and Render workflows

**Files:**
- Create: `docs/OPERATIONS.md`
- Modify: `DEPLOYMENT.md`

- [ ] Document environment configuration:

```text
OPS_SECRET=<separate-random-secret>
```

- [ ] Provide Postman/curl examples using environment variables without writing a real secret into documentation.

- [ ] Document:

  - Finding a failed job in the protected API.
  - Copying its request ID.
  - Searching the request ID in Render logs.
  - Interpreting public error codes.
  - Distinguishing scraper, parser, database, build, and cluster failures.
  - Rotating `OPS_SECRET`.

- [ ] Add a verification checklist ensuring no sensitive fields appear in public or protected operations responses.

- [ ] Commit.

```bash
git add docs/OPERATIONS.md DEPLOYMENT.md
git commit -m "docs: add operations tracing guide"
```

---

## Completion criteria

- [ ] Every API response has a request ID.
- [ ] Render receives structured request metadata without sensitive values.
- [ ] Long-running jobs emit bounded, sanitized events.
- [ ] Public endpoints expose progress but no detailed diagnostics.
- [ ] Protected diagnostics require a separate operations secret.
- [ ] No frontend code stores or requests the operations secret.
- [ ] Request IDs connect Postman responses, job events, and Render logs.
- [ ] Retention prevents unbounded Turso growth.
