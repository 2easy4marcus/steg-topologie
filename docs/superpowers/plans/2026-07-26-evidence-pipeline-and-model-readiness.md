# Evidence Pipeline and Model Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a traceable, versioned evidence pipeline in which duplicate or failed STEG scrapes cannot corrupt the model, every cluster run records the evidence build it used, and clustering is gated by meaningful data-quality signals.

**Architecture:** Store immutable HTML snapshots, parser/normalizer-versioned parses, and one deterministic notice state containing the selected latest snapshot and last-known-valid active parse. Build aggregates under immutable build IDs, validate them, and activate them with a short atomic pointer switch. Persist cluster runs with their source build ID and expose provenance, readiness, and sanitized pipeline progress.

**Tech Stack:** Python 3, FastAPI, Pydantic, libSQL/Turso, BeautifulSoup, NetworkX, Louvain community detection, pytest, vanilla HTML/CSS/JavaScript.

---

## Scope

This plan includes:

- Evidence capture, parsing, normalization, and deterministic activation.
- Versioned aggregate builds.
- Idempotent live scraping and historical backfill.
- Atomic job locking and basic public pipeline status.
- Multi-signal model readiness.
- Build-linked cluster runs.
- Edge provenance.
- Model-page accessibility, including a non-graph edge table.
- Safe reparse/rebuild commands and production rollout.

Detailed operations tracing, event filtering, retention tooling, and protected diagnostic APIs are specified separately in:

`docs/superpowers/plans/2026-07-26-operations-console.md`

---

## Canonical data flow

```text
HTTP fetch attempt
  → immutable content-addressed snapshot
  → selected latest snapshot for that notice
  → parser-versioned and normalization-versioned parse attempt
  → last-known-valid active parse
  → canonical notice/locality observations
  → immutable aggregate evidence build
  → atomic active-build pointer switch
  → readiness evaluation
  → build-linked cluster run
  → evidence-backed API and visualization
```

## Deterministic snapshot and parse rules

1. Every successful changed fetch creates or reuses a snapshot identified by `(notice_id, content_hash)`.
2. The successful fetch updates `notice_state.latest_snapshot_id`.
3. Only a valid parse of `latest_snapshot_id` activates automatically.
4. Parsing an older snapshot never changes active evidence.
5. A failed parse of the latest snapshot leaves the previous `active_parse_id` unchanged.
6. An older parse can be activated only through an explicit rollback command.
7. Processing identity is `(snapshot_id, parser_version, normalization_version)`.
8. A parser or normalization version change reparses stored HTML without fetching STEG again.
9. A warning parse activates only when it contains at least two distinct canonical localities.
10. The UI reports when the active parse belongs to an older snapshot because the latest content failed parsing.

## Canonical terminology

| Term | Definition |
|---|---|
| Fetch attempt | One request to a STEG URL, whether successful or failed. |
| Snapshot | One unique successful HTML body for a notice and content hash. |
| Latest snapshot | The most recently selected successful content for a notice. |
| Parse attempt | Structured output for one snapshot, parser version, and normalization version. |
| Active parse | Last-known-valid parse used by the evidence model. |
| Valid notice | Active `ok` or eligible `warning` parse with at least two canonical localities. |
| Observation | One distinct valid notice mentioning a locality or locality pair. |
| Evidence build | Immutable derived locality and pair observations under one `build_id`. |
| Cluster run | Clustering output tied to one `build_id` and algorithm version. |
| Operational freshness | Time since the last successful scheduled scrape. |

---

## File map

- Create `app/evidence_models.py` — Pydantic contracts and enums.
- Create `app/evidence_pipeline.py` — snapshot, parse, activation, aggregate-build orchestration.
- Create `app/model_readiness.py` — model-quality and operational-health signals.
- Create `app/observability.py` — request IDs, safe public errors, basic job status, and job locks.
- Create `app/reparse_snapshots.py` — parser/normalizer migration command.
- Create `app/rebuild_evidence.py` — aggregate build command.
- Modify `app/db.py` — schema and focused persistence/query interfaces.
- Modify `app/steg_scraper.py` — structured, versioned parser output.
- Modify `app/locality_dedup.py` — normalization version.
- Modify `app/import_official.py` and `app/backfill_official.py` — idempotent ingestion.
- Modify `app/cluster_inference.py` — build-aware cluster runs.
- Modify `app/main.py` — middleware and API contracts.
- Modify `static/model.html` — readiness, provenance, stale-state warnings, and accessible edge table.
- Modify `.github/workflows/scrape.yml`, `DEPLOYMENT.md`, and `README.md`.
- Add focused tests for contracts, database state, atomicity, locking, ingestion, readiness, clustering, provenance, and accessibility.

---

### Task 0: Secure and verify the baseline

**Files:**
- Modify: `.gitignore`
- Verify: `.github/workflows/scrape.yml`

- [ ] Rotate the previously exposed cron secret.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set the new value in Render and GitHub Actions. Never commit it.

- [ ] Ensure generated files are ignored.

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
tracker.db
tracker.db-*
server.log
.DS_Store
```

- [ ] Run the current suite.

```bash
pytest -q
```

Expected: the existing suite passes before schema changes.

- [ ] Commit.

```bash
git add .gitignore
git commit -m "chore: harden repository hygiene"
```

---

### Task 1: Define canonical contracts

**Files:**
- Create: `app/evidence_models.py`
- Create: `tests/test_evidence_models.py`

- [ ] Define and test:

```python
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class ParseStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"


class BuildStatus(str, Enum):
    BUILDING = "building"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ParsedLocality(BaseModel):
    raw_name: str
    canonical_name: str
    subregion_name: str | None = None
    ordinal: int = Field(ge=0)


class ParsedNoticeEvidence(BaseModel):
    notice_id: str
    snapshot_id: str
    source_url: str
    title: str
    notice_date_raw: str | None = None
    notice_date_iso: date | None = None
    parser_version: str
    normalization_version: str
    parse_status: ParseStatus
    localities: list[ParsedLocality]
    warnings: list[str]


class PublicJobError(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ClusterRunMetadata(BaseModel):
    cluster_run_id: str
    build_id: str
    active_build_id: str
    algorithm_version: str
    is_current: bool
    completed_at: datetime
```

- [ ] Tests must reject invalid enum values, non-ISO normalized dates, negative ordinals, and missing build metadata.

- [ ] Verify.

```bash
pytest tests/test_evidence_models.py -v
```

- [ ] Commit.

```bash
git add app/evidence_models.py tests/test_evidence_models.py
git commit -m "feat: define evidence contracts"
```

---

### Task 2: Add versioned evidence, builds, and cluster-run schema

**Files:**
- Modify: `app/db.py`
- Create: `tests/test_db_evidence.py`

- [ ] Add idempotent migrations for:

```sql
CREATE TABLE IF NOT EXISTS notice_fetch_attempts (
    id TEXT PRIMARY KEY,
    notice_id TEXT,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    content_hash TEXT,
    public_error_code TEXT,
    internal_error_detail TEXT
);

CREATE TABLE IF NOT EXISTS notice_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_html TEXT NOT NULL,
    first_fetched_at TEXT NOT NULL,
    UNIQUE(notice_id, content_hash)
);

CREATE TABLE IF NOT EXISTS notice_parses (
    parse_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    notice_id TEXT NOT NULL,
    title TEXT NOT NULL,
    notice_date_raw TEXT,
    notice_date_iso TEXT,
    parser_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    parse_warnings TEXT NOT NULL DEFAULT '[]',
    parsed_at TEXT NOT NULL,
    UNIQUE(snapshot_id, parser_version, normalization_version)
);

CREATE TABLE IF NOT EXISTS notice_localities (
    parse_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    subregion_name TEXT,
    PRIMARY KEY(parse_id, ordinal)
);

CREATE TABLE IF NOT EXISTS notice_state (
    notice_id TEXT PRIMARY KEY,
    latest_snapshot_id TEXT,
    active_parse_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_builds (
    build_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    notice_count INTEGER NOT NULL DEFAULT 0,
    locality_count INTEGER NOT NULL DEFAULT 0,
    pair_count INTEGER NOT NULL DEFAULT 0,
    public_error_code TEXT,
    internal_error_detail TEXT
);

CREATE TABLE IF NOT EXISTS build_locality_counts (
    build_id TEXT NOT NULL,
    locality TEXT NOT NULL,
    notice_count INTEGER NOT NULL,
    PRIMARY KEY(build_id, locality)
);

CREATE TABLE IF NOT EXISTS build_cooccurrences (
    build_id TEXT NOT NULL,
    locality_a TEXT NOT NULL,
    locality_b TEXT NOT NULL,
    notice_count INTEGER NOT NULL,
    distinct_date_count INTEGER NOT NULL,
    first_observed_on TEXT,
    last_observed_on TEXT,
    PRIMARY KEY(build_id, locality_a, locality_b)
);

CREATE TABLE IF NOT EXISTS model_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    active_build_id TEXT
);

CREATE TABLE IF NOT EXISTS cluster_runs (
    run_id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    cluster_count INTEGER NOT NULL DEFAULT 0,
    locality_count INTEGER NOT NULL DEFAULT 0,
    public_error_code TEXT,
    internal_error_detail TEXT
);

CREATE TABLE IF NOT EXISTS cluster_members (
    run_id TEXT NOT NULL,
    locality TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    stability REAL NOT NULL,
    PRIMARY KEY(run_id, locality)
);

CREATE TABLE IF NOT EXISTS cluster_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    active_cluster_run_id TEXT
);
```

- [ ] Add foreign keys where the deployed libSQL adapter enforces them. Keep atomic application validation because connection-level foreign-key settings must not be the only protection.

- [ ] Add indexes for notice IDs, snapshot IDs, parse IDs, ISO dates, canonical names, build IDs, and cluster run IDs.

- [ ] Test uniqueness, referential behavior, and that internal errors never appear in public query models.

```bash
pytest tests/test_db_evidence.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/db.py tests/test_db_evidence.py
git commit -m "feat: add versioned evidence schema"
```

---

### Task 3: Implement atomic state transitions and job locks

**Files:**
- Modify: `app/db.py`
- Create: `app/evidence_pipeline.py`
- Create: `app/observability.py`
- Create: `tests/test_evidence_atomicity.py`
- Create: `tests/test_job_locks.py`

- [ ] Add:

```sql
CREATE TABLE IF NOT EXISTS job_locks (
    lock_name TEXT PRIMARY KEY,
    owner_job_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
```

- [ ] Acquire a lock with one atomic upsert:

```sql
INSERT INTO job_locks(
    lock_name, owner_job_id, acquired_at, heartbeat_at, expires_at
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT(lock_name) DO UPDATE SET
    owner_job_id = excluded.owner_job_id,
    acquired_at = excluded.acquired_at,
    heartbeat_at = excluded.heartbeat_at,
    expires_at = excluded.expires_at
WHERE job_locks.expires_at < excluded.acquired_at;
```

Check the affected-row count. Zero means another non-expired owner holds the lock.

- [ ] Heartbeats and releases must include both `lock_name` and `owner_job_id`. A process cannot renew or release another job's lock.

- [ ] Use explicit lock expiry and heartbeat intervals:

| Job | Lock TTL | Heartbeat |
|---|---:|---:|
| Scrape | 15 minutes | 60 seconds |
| Backfill | 30 minutes | 60 seconds |
| Evidence rebuild | 15 minutes | 60 seconds |
| Reclustering | 15 minutes | 60 seconds |

- [ ] Implement short atomic state transitions:

  - Selecting a latest snapshot updates `notice_state.latest_snapshot_id`.
  - Automatic parse activation verifies the parse belongs to that latest snapshot and has an eligible status.
  - Build activation verifies `status='completed'` in the same atomic operation.
  - Cluster-run activation verifies the run is completed and references the current active build.

- [ ] Add injected-failure tests proving previous active parse, build, and cluster run survive failure.

```bash
pytest tests/test_evidence_atomicity.py tests/test_job_locks.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/db.py app/evidence_pipeline.py app/observability.py \
  tests/test_evidence_atomicity.py tests/test_job_locks.py
git commit -m "feat: add atomic state and job locks"
```

---

### Task 4: Version parser and normalization output

**Files:**
- Modify: `app/steg_scraper.py`
- Modify: `app/locality_dedup.py`
- Modify: `app/evidence_pipeline.py`
- Modify: `tests/test_steg_scraper.py`
- Create: `tests/test_evidence_versions.py`

- [ ] Define:

```python
# app/steg_scraper.py
PARSER_VERSION = "2"

# app/locality_dedup.py
NORMALIZATION_VERSION = "1"
```

- [ ] Preserve the source date and normalize it to ISO `YYYY-MM-DD`.

- [ ] Preserve headers beginning with `جهة` or `ولاية`. Never promote the first town into a header.

- [ ] Emit stable warning/error codes:

```text
missing_subregion_header
empty_locality_list
unmatched_notice_title
missing_notice_date
invalid_notice_date
```

- [ ] Record failed parses but never activate them.

- [ ] Automatically activate only an eligible parse of `notice_state.latest_snapshot_id`.

- [ ] Store reparses of older snapshots without changing active evidence.

- [ ] Verify.

```bash
pytest tests/test_steg_scraper.py tests/test_evidence_versions.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/steg_scraper.py app/locality_dedup.py app/evidence_pipeline.py \
  tests/test_steg_scraper.py tests/test_evidence_versions.py
git commit -m "feat: version parsing and normalization"
```

---

### Task 5: Implement versioned aggregate builds

**Files:**
- Modify: `app/evidence_pipeline.py`
- Modify: `app/db.py`
- Create: `tests/test_model_builds.py`

- [ ] Build only from active parses.

- [ ] Ensure each `(notice_id, canonical_name)` and `(notice_id, locality_a, locality_b)` contributes once.

- [ ] Derive:

```text
first_observed_on = MIN(notice_date_iso)
last_observed_on  = MAX(notice_date_iso)
```

Never substitute a scrape timestamp. Leave both null when supporting outage dates are unavailable.

- [ ] Populate rows under a new inactive build ID.

- [ ] Validate:

  - No self-edges.
  - Canonically sorted pairs.
  - Stored counts match independently computed counts.
  - Every edge resolves to a supporting notice.
  - Build status is `completed`.

- [ ] Atomically activate the validated build.

- [ ] Test a failure before activation and verify the previous build remains active.

```bash
pytest tests/test_model_builds.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/evidence_pipeline.py app/db.py tests/test_model_builds.py
git commit -m "feat: build versioned model evidence"
```

---

### Task 6: Integrate idempotent scrape and backfill

**Files:**
- Modify: `app/import_official.py`
- Modify: `app/backfill_official.py`
- Modify: `app/evidence_pipeline.py`
- Modify: `tests/test_import_official_cooccurrence.py`
- Modify: `tests/test_backfill_official.py`

- [ ] Route live and historical notices through one processing path.

- [ ] Reprocessing identical `(snapshot_id, parser_version, normalization_version)` must perform no evidence or aggregate write.

- [ ] A changed active parse marks evidence dirty. After ingestion completes, create one versioned aggregate build.

- [ ] A failed latest parse leaves the active parse and current model build available.

- [ ] Preserve the existing natural archive stop condition and maximum-page safety cap.

- [ ] Acquire the evidence-pipeline job lock before mutation. Return `409 job_already_running` with the owner job ID when unavailable.

- [ ] Verify duplicate, changed-content, parser-version, normalization-version, failed-parse, and concurrent-job cases.

```bash
pytest tests/test_import_official_cooccurrence.py tests/test_backfill_official.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/import_official.py app/backfill_official.py app/evidence_pipeline.py \
  tests/test_import_official_cooccurrence.py tests/test_backfill_official.py
git commit -m "fix: make ingestion idempotent"
```

---

### Task 7: Add request IDs and persistent public pipeline status

**Files:**
- Modify: `app/observability.py`
- Modify: `app/db.py`
- Modify: `app/import_official.py`
- Modify: `app/backfill_official.py`
- Modify: `app/main.py`
- Create: `tests/test_pipeline_status.py`

- [ ] Add `ingestion_runs` with:

```text
id, job_type, status, started_at, finished_at, current_page,
pages_scanned, links_discovered, notices_imported,
notices_unchanged, notices_skipped, notices_failed,
last_progress_at, request_id, public_error_code,
internal_error_detail
```

- [ ] Add `X-Request-ID` middleware. Emit structured stdout metadata containing only timestamp, request ID, method, route template, status, and duration.

- [ ] Never log bodies, headers, secrets, query values, citizen comments, Turso credentials, or raw HTML.

- [ ] Map failures to stable public codes:

```text
steg_timeout
steg_http_error
steg_parse_error
database_unavailable
job_already_running
job_failed
```

- [ ] Add:

```text
GET /api/status
GET /api/status/ingestion
```

These public endpoints expose safe progress and request IDs, never internal error detail.

- [ ] Verify persistence across application restart and logging redaction.

```bash
pytest tests/test_pipeline_status.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/observability.py app/db.py app/import_official.py \
  app/backfill_official.py app/main.py tests/test_pipeline_status.py
git commit -m "feat: expose safe pipeline status"
```

---

### Task 8: Compute model quality and parser health separately

**Files:**
- Create: `app/model_readiness.py`
- Modify: `app/db.py`
- Modify: `app/main.py`
- Create: `tests/test_model_readiness.py`

- [ ] Use initial model-quality thresholds:

```python
MIN_VALID_NOTICES = 30
MIN_DISTINCT_OUTAGE_DATES = 15
MIN_LOCALITIES = 10
MIN_REPEATED_PAIRS = 20
MIN_ACTIVE_OK_RATIO = 0.80
MAX_LARGEST_NOTICE_PAIR_SHARE = 0.20
```

- [ ] Compute model-quality signals from active parses and the active evidence build.

- [ ] Add operational signals:

```python
MIN_RECENT_PARSE_SUCCESS_RATIO = 0.80
RECENT_PARSE_WINDOW_DAYS = 30
MAX_SCRAPE_AGE_HOURS = 48
```

`recent_parse_success_ratio` uses the latest parse attempt for each selected latest snapshot attempted during the window. Failed attempts remain in the denominator.

- [ ] Model-quality failure blocks new cluster runs. Operational-health failure blocks automatic scheduled clustering but leaves the previous active model and cluster run available.

- [ ] Preserve every current `/api/model-status` field and add optional nested `model_quality` and `operational_health`.

- [ ] Add:

```text
GET /api/model-readiness
```

- [ ] Verify exact boundaries, stale-scraper behavior, failed-parse visibility, and backward-compatible JSON.

```bash
pytest tests/test_model_readiness.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/model_readiness.py app/db.py app/main.py \
  tests/test_model_readiness.py
git commit -m "feat: evaluate evidence readiness"
```

---

### Task 9: Tie clustering to evidence builds

**Files:**
- Modify: `app/cluster_inference.py`
- Modify: `app/db.py`
- Modify: `app/main.py`
- Modify: `tests/test_cluster_inference.py`
- Create: `tests/test_cluster_api.py`

- [ ] Define:

```python
ALGORITHM_VERSION = "ppmi-louvain-v1"
```

- [ ] Read co-occurrences from one explicit active `build_id`.

- [ ] Write one `cluster_runs` row before clustering and store all members under its `run_id`.

- [ ] Permit a new run on the same calendar date when `build_id` or algorithm version differs.

- [ ] Activate only a completed run whose `build_id` still equals the active evidence build.

- [ ] Return from `/api/clusters`:

```json
{
  "cluster_run_id": "run-123",
  "build_id": "build-123",
  "active_build_id": "build-123",
  "algorithm_version": "ppmi-louvain-v1",
  "is_current": true,
  "clusters": []
}
```

- [ ] When the active build changes, keep old runs accessible but mark them stale until a new run activates.

- [ ] Verify build changes, same-day reruns, algorithm changes, stale detection, and failed-run rollback.

```bash
pytest tests/test_cluster_inference.py tests/test_cluster_api.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/cluster_inference.py app/db.py app/main.py \
  tests/test_cluster_inference.py tests/test_cluster_api.py
git commit -m "feat: link clusters to evidence builds"
```

---

### Task 10: Add edge provenance

**Files:**
- Modify: `app/db.py`
- Modify: `app/main.py`
- Create: `tests/test_edge_evidence_api.py`

- [ ] Add:

```http
GET /api/edge-evidence?locality_a=قليبية&locality_b=حمام لغزاز
```

- [ ] Return canonical pair names, distinct notice/date counts, first/last observation dates, active build ID, and newest-first source notices with title, ISO date, STEG URL, and subregions.

- [ ] Reject identical names with HTTP 422 and missing active edges with HTTP 404.

- [ ] Query active parses and the active build only.

- [ ] Never expose raw HTML or internal errors.

```bash
pytest tests/test_edge_evidence_api.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/db.py app/main.py tests/test_edge_evidence_api.py
git commit -m "feat: expose edge provenance"
```

---

### Task 11: Update the model page and accessible alternatives

**Files:**
- Modify: `static/model.html`
- Create: `tests/test_model_page.py`
- Create: `tests/test_accessibility.py`

- [ ] Show model-quality and operational-health signals separately.

- [ ] Show active evidence build ID, cluster run ID, algorithm version, and stale/current status.

- [ ] Show a warning when active evidence comes from an older snapshot after a newer parse failure.

- [ ] Add an edge table equivalent to the graph with:

```text
Locality A, Locality B, supporting notices, distinct dates,
first observation, last observation, cluster, evidence action
```

Graph filters and edge-table filters must stay synchronized.

- [ ] Selecting a graph edge or table row opens the same evidence panel.

- [ ] Use:

```text
Inferred statistical relationship — not a confirmed transformer, feeder, or physical grid location.
```

- [ ] Accessibility requirements:

  - Arabic text uses `dir="auto"`.
  - Updates use `aria-live="polite"`.
  - Status is not communicated by color alone.
  - Evidence links have descriptive names.
  - Keyboard focus remains visible and stable.
  - Graph updates never steal focus.
  - Edge table supports full keyboard and screen-reader access.
  - Reduced-motion and narrow-view states remain usable.
  - Run one rendered-page accessibility audit.

```bash
pytest tests/test_model_page.py tests/test_accessibility.py -v
pytest -q
```

- [ ] Commit.

```bash
git add static/model.html tests/test_model_page.py tests/test_accessibility.py
git commit -m "feat: explain model evidence accessibly"
```

---

### Task 12: Add safe reparse, rollback, and rebuild commands

**Files:**
- Create: `app/reparse_snapshots.py`
- Create: `app/rebuild_evidence.py`
- Create: `app/rollback_notice.py`
- Create: `tests/test_reparse_snapshots.py`
- Create: `tests/test_rebuild_evidence.py`
- Create: `tests/test_rollback_notice.py`
- Modify: `DEPLOYMENT.md`

- [ ] Provide dry-run-first commands:

```bash
python -m app.reparse_snapshots
python -m app.reparse_snapshots --apply
python -m app.rebuild_evidence
python -m app.rebuild_evidence --apply
python -m app.rollback_notice NOTICE_ID PARSE_ID
python -m app.rollback_notice NOTICE_ID PARSE_ID --apply
```

- [ ] Reparse creates missing version combinations from stored HTML.

- [ ] Rebuild creates and validates a new inactive evidence build before activation.

- [ ] Rollback requires an explicit notice and parse ID, verifies ownership, defaults to dry-run, and records the reason.

- [ ] Failures preserve active parse, build, and cluster-run pointers.

```bash
pytest tests/test_reparse_snapshots.py tests/test_rebuild_evidence.py \
  tests/test_rollback_notice.py -v
pytest -q
```

- [ ] Commit.

```bash
git add app/reparse_snapshots.py app/rebuild_evidence.py app/rollback_notice.py \
  tests/test_reparse_snapshots.py tests/test_rebuild_evidence.py \
  tests/test_rollback_notice.py DEPLOYMENT.md
git commit -m "feat: add evidence maintenance commands"
```

---

### Task 13: Update automation and verify production

**Files:**
- Modify: `.github/workflows/scrape.yml`
- Modify: `README.md`
- Modify: `DEPLOYMENT.md`

- [ ] Keep scraping once every 24 hours.

- [ ] Trigger automatic reclustering only after successful ingestion, successful evidence-build activation, passing model quality, and passing operational health.

- [ ] Print HTTP status, request ID, duration, and safe error body in GitHub Actions.

- [ ] Run the complete suite.

```bash
pytest -q
```

- [ ] Deploy and verify:

```text
GET /api/status
GET /api/status/ingestion
GET /api/model-status
GET /api/model-readiness
GET /api/clusters
GET /api/edge-evidence
```

- [ ] Confirm:

  - Duplicate notices cannot inflate counts.
  - Older reparses cannot replace newer selected content.
  - Failed latest parses remain visible without erasing valid evidence.
  - Concurrent jobs cannot acquire the same lock.
  - Expired locks recover after a crash.
  - Failed builds and cluster runs preserve active pointers.
  - Cluster responses identify their evidence build.
  - Every displayed edge resolves to supporting notices.
  - Public responses contain no internal diagnostics or secrets.

- [ ] Commit.

```bash
git add .github/workflows/scrape.yml README.md DEPLOYMENT.md
git commit -m "docs: finalize evidence pipeline rollout"
```

---

## Completion criteria

- [ ] Unchanged notices cannot inflate evidence.
- [ ] Parser and normalization changes can reprocess stored HTML.
- [ ] Snapshot selection and parse activation are deterministic.
- [ ] Failed fetches/parses preserve last-known-valid evidence.
- [ ] Aggregate builds activate atomically.
- [ ] Every cluster run records its evidence build and algorithm version.
- [ ] Stale clusters are identifiable.
- [ ] Parser failures remain visible in operational health.
- [ ] Job locking is atomic and crash-recoverable.
- [ ] Every displayed edge links to supporting STEG notices.
- [ ] The graph has an equivalent accessible edge table.
- [ ] The full test suite and rendered accessibility audit pass.
