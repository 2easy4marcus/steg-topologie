# Grid Co-occurrence Cluster Inference — Design Spec

Date: 2026-07-24
Status: Approved, pending implementation plan

## Motivation

STEG (Tunisia's electricity utility) does not publish real grid topology
(substations, feeders, transformers). The current tracker groups outages by
governorate, which doesn't reflect how outages actually propagate — outages
follow shared electrical infrastructure, not administrative boundaries.

No public source for real grid topology exists (confirmed: STEG's own site,
Tunisia's national open-data catalog, AGEOS ArcGIS Hub, OpenStreetMap all
checked). This design does **not** attempt to recover real infrastructure.
Instead it statistically infers probable shared-circuit groupings from
which localities get cut together in STEG's own outage notices — analogous
to how word embeddings derive meaning from co-occurrence statistics.

**Hard constraint:** inferred clusters are statistical groupings only. They
must never be presented as, or be mistaken for, real physical
infrastructure identity or location. All UI surfaces of this feature must
carry a visible disclaimer.

## Scope (v1)

In scope:
- Incremental co-occurrence tracking as new notices are scraped
- PMI-weighted co-occurrence graph + Louvain community detection, run daily
- Stability scoring per locality (consistency of cluster membership across
  recent runs)
- On-demand geocoding of locality names (cached)
- A map layer (toggle, off by default) showing inferred clusters
- Automation via GitHub Actions hitting protected app endpoints (no
  Claude-managed scheduling, no git-committed DB)

Explicitly out of scope for v1 (deferred):
- word2vec/node2vec-style embeddings — needs a much larger notice corpus
  than exists yet; revisit once historical data is substantial (likely
  hundreds of notices)
- Backfilling historical notices from archives — starting from zero,
  accumulating going forward
- Matching Louvain cluster IDs across days for stable coloring — v1
  accepts that a locality's cluster *color* may shift day to day even
  when its membership doesn't; the stability score is the real signal
- SONEDE/water — no official per-incident source exists, unrelated to this
  feature

## Data & Scheduling

Three new tables (`db.py`):

- `localities`: `name TEXT PRIMARY KEY, lat REAL, lng REAL, governorate TEXT, geocoded_at TEXT`
  Populated lazily on first sighting of a locality name.
- `cooccurrences`: `locality_a TEXT, locality_b TEXT, notice_count INTEGER, last_seen TEXT, PRIMARY KEY (locality_a, locality_b)`
  `locality_a < locality_b` enforced to avoid duplicate ordering. Updated
  incrementally on each scrape, not recomputed from scratch.
- `clusters`: `run_date TEXT, cluster_id INTEGER, locality TEXT, stability REAL, PRIMARY KEY (run_date, locality)`
  One row per locality per daily clustering run. History retained so
  stability can be computed across `run_date`s.
- `locality_aliases`: `alias_raw_text TEXT PRIMARY KEY, canonical_locality TEXT`
  Maps a raw zone-line text variant to its canonical `localities` name —
  see the dedup pipeline in Inference Pipeline below.
- `locality_notice_counts`: `locality TEXT PRIMARY KEY, notice_count INTEGER`
  Tracks the true count of distinct notices each locality has appeared in
  (incremented once per notice, not once per pair). Added during
  implementation: `P(a)` in the PMI formula below needs the real
  `notices_containing(a) / total_notices`, not an approximation — an
  earlier draft approximated it by summing co-occurrence edge weights,
  which was caught in review as inflating the marginal for any locality
  that co-occurs with many different partners, distorting PMI/clustering
  as the corpus grows. This table is the correct, spec-faithful fix.

Automation: GitHub Actions is a pure external cron trigger, not a data
mover. Two new protected FastAPI endpoints, guarded by a shared-secret
header (`X-Cron-Secret`, checked against an env var on the host):

- `POST /api/internal/scrape` — runs existing `import_official.py` scrape
  logic in-process; upserts `official_notices`; extracts all locality pairs
  within each notice's zone/subregion list and upserts `cooccurrences`.
- `POST /api/internal/recluster` — runs the clustering job (below); writes
  `clusters` rows for today's `run_date`.

`.github/workflows/scrape.yml`: hourly `curl -X POST .../scrape`, daily
`curl -X POST .../recluster`. No repo checkout of the DB, no commits, no
redeploy required to see new data.

**Hosting (researched 2026-07-24, replaces earlier Fly.io assumption):**
Fly.io's free tier no longer exists (removed 2024) — cheapest always-on
app now runs ~$2-5/mo there, not free. Render's free tier has no
persistent disk (paid-only), and free web services sleep after 15 min
idle. Railway has no permanent free tier, trial credits only.

Chosen path: **Render (free web service) + Turso (free hosted
libSQL/SQLite-compatible DB)**. Since the DB lives on Turso instead of
local disk, Render's cold-start-after-idle behavior stops being a data
risk — the app has no local state to lose between sleeps, it just
reconnects. Turso's free tier (5GB storage, 500M row reads/mo, 10M row
writes/mo) comfortably covers this app's scale. Cost: $0, no card
required for either service's free tier at time of writing.

This does require one code change beyond what's in this doc: `db.py`'s
`sqlite3.connect(DB_PATH)` swaps for a libSQL client connection
(`libsql-client` or `libsql-experimental` Python package), which speaks
near-identical SQL, so table/query definitions in this spec don't change
— just the connection layer.

Runner-up, if avoiding a hosted DB dependency matters more than avoiding
DevOps: Oracle Cloud Free Tier (genuine Always Free small VM with real
persistent disk, zero DB code change, but you own SSH/systemd/reverse-proxy
setup yourself).

Rejected alternative: committing `tracker.db` to git and having the Action
push updates. Rejected because `tracker.db` also holds live
user-submitted `user_reports` — a concurrent write from a citizen
submitting a report at the same moment the Action checks out/commits/pushes
would silently diverge/clobber data between the app's live disk copy and
the Action's stale git checkout. The endpoint-trigger approach keeps the
app's disk as the single source of truth for all writes, in-process,
eliminating that race. It also avoids unbounded `.git` history growth from
binary diffs and removes the redeploy-to-see-new-data latency.

## Inference Pipeline

**Co-occurrence update** (inside `/scrape`): for each notice, every pairwise
combination of localities in its zone/subregion list gets
`cooccurrences.notice_count += 1`, `last_seen = now`. Incremental.

**Recluster job** (inside `/recluster`, daily):

1. Build a weighted graph: node = locality, edge weight = PPMI (positive
   PMI; negative values clipped to 0, since Louvain requires non-negative
   weights):
   `PMI(a,b) = log( P(a,b) / (P(a) * P(b)) )`
   where `P(a,b) = cooccur_count(a,b) / total_notices`,
   `P(a) = notices_containing(a) / total_notices`.
2. Run Louvain community detection (`python-louvain` + `networkx`) →
   cluster_id per locality.
3. Write `clusters` rows for today's `run_date`.
4. Compute stability per locality: Jaccard similarity of its cluster
   co-membership set today vs. each of the last 7 `run_date`s, averaged.
   High, consistent overlap → high stability → more visually prominent on
   the map. New/volatile groupings → low stability → faded.

**Data-floor tuning signals:** the 30-notice/10-locality floor (Error
Handling) is a starting point, not fixed. Watch these once real data
accumulates and adjust the floor if they misbehave:
- Louvain modularity score (Q) per run — below ~0.3 (the typical rule of
  thumb for "real" community structure), treat data as still too sparse
  regardless of raw counts.
- Fraction of localities landing in singleton clusters — if this stays
  high (>50%) even after the raw floor is met, the floor is too low.
- Median stability score across localities — if it stays near 0 well past
  the floor, clustering is still noisy day-to-day; raise the floor or
  extend the 7-day stability lookback window.

**Geocoding**: lazy, on first-seen locality name only, via Nominatim (OSM),
respecting its 1 req/sec rate limit and required `User-Agent` header.
Result cached permanently in `localities`; a name is never re-queried once
resolved.

**Locality dedup (approved, not deferred):** raw zone-line text goes
through a normalize → exact-match → fuzzy-match → alias pipeline before
becoming (or matching) a `localities` row:

1. Normalize: strip/collapse whitespace; unify Arabic alef forms
   (أ/إ/آ → ا) and ta-marbuta/ha (ة/ه) variants; strip diacritics
   (tashkeel).
2. Exact match on the normalized text against existing `localities` names
   (fast path, no fuzzy cost for the common case).
3. If no exact match, fuzzy match via `rapidfuzz` (similarity ≥ 90%,
   token-sort ratio) against existing normalized names.
4. Match found → record as an alias, not a new locality: new
   `locality_aliases` table (`alias_raw_text TEXT PRIMARY KEY, canonical_locality TEXT`).
   All co-occurrence/cluster logic operates on the canonical name.
5. No match at any threshold → new canonical `localities` row, using this
   text as-is.

Raw text is always retained (in `locality_aliases` for variants, or as the
canonical name itself) for display/audit — normalization only affects
matching, not what's shown to users.

New dependencies: `networkx`, `python-louvain`, `geopy` (or raw `requests`
against Nominatim directly), `rapidfuzz`.

## API & Frontend

New endpoint: `GET /api/clusters` → latest `run_date`'s rows joined with
`localities` for coordinates:
`[{locality, cluster_id, stability, lat, lng}]`.
Below the data floor (see Error Handling), returns
`{data: [], insufficient_data: true, notices_so_far: N, needed: 30}`
instead. Existing `/api/official`, `/api/reports`, `/api/stats` unchanged.

Request/response shapes for this endpoint and the two internal endpoints
are defined as Pydantic models (`ClusterPoint`, `ClustersResponse`,
`ScrapeResult`, `RerecomputeResult`) rather than raw dicts — consistent
validation/serialization, and self-documenting via FastAPI's generated
OpenAPI schema. This is a tech-choice addition to the plan, not a change
to the shapes already described above.

Frontend: a new toggle on the existing map, "Show inferred grid clusters
(beta)", off by default. When on: one dot per locality, color = cluster_id
(hashed to a ~12-color palette, repeating if more clusters exist), opacity
scaled by stability score. Tapping/hovering a dot shows a tooltip:
"Cluster #N — N localities, based on X notices, stability Y%". A persistent
legend line is shown whenever the layer is on:
"Statistical grouping from outage co-occurrence — not verified STEG
infrastructure." This disclaimer is non-negotiable per the accuracy
constraint above — it is not relegated to a help doc.

Known limitation accepted for v1: Louvain is a greedy bottom-up modularity
optimizer with no memory of previous runs — it assigns arbitrary integer
labels each time, in traversal order, not by identity. Concretely: today's
run might label {A,B,C} as cluster 2; tomorrow, after new notices arrive,
the same (or 95%-overlapping) group could come out as cluster 5, purely
from internal ordering, not because the grouping actually changed. So a
locality's *color* can shift day to day even when its real grouping
hasn't. This is cosmetic, not a correctness bug — the stability score
already independently tracks real membership consistency via Jaccard
overlap regardless of what integer ID gets assigned, so it's safe to
defer. Deferred fix (v2): after each run, match each new cluster to the
prior run's cluster it overlaps with most (greedy or, more rigorously, a
Hungarian-algorithm optimal assignment across all pairs), reuse that ID,
and only mint new IDs for clusters with no good match.

## Error Handling

- `/scrape`: reuses `steg_scraper.py`'s existing retry/backoff and
  `FetchError`. On failure: HTTP 502 + short message; DB untouched (each
  notice upserts atomically, no partial corruption).
- `/scrape` and `/recluster`: missing or incorrect `X-Cron-Secret` → 401,
  no work performed.
- `/recluster` is idempotent per day: if today's `run_date` already has
  rows in `clusters`, a repeat call (Action retry, double-fire) is a 200
  no-op rather than a duplicate/conflicting run.
- Data floor: below a minimum (e.g. <30 notices or <10 distinct
  localities), `/recluster` skips Louvain entirely; `/api/clusters` returns
  the `insufficient_data` response above. No fabricated clusters from
  single-digit sample sizes.
- Geocoding failure (no match / rate-limited): `localities` row saved with
  `lat=NULL`; that locality is skipped on the map (not plotted). Retried
  only on subsequent scrapes for rows still NULL — no infinite retry storm,
  no permanent silent skip.
- Isolated locality (never co-occurred with anything): still forms a
  singleton cluster with `stability = 0` (no history to compare against);
  rendered, heavily faded, not dropped from the dataset.

## Testing

- Unit: PPMI calculation against a fixed 3-notice/4-locality fixture with
  hand-verified expected values.
- Unit: Louvain on a toy graph with an obvious 2-cluster structure; assert
  correct partition.
- Unit: stability/Jaccard calculation across a mocked multi-day `run_date`
  fixture.
- Integration: `/scrape` and `/recluster` auth (401 without/with wrong
  secret, 200 with correct one).
- Integration: `/recluster` idempotency (second same-day call is a no-op).
- Integration: insufficient-data path returns the correctly flagged empty
  response.
- Geocoding: mock Nominatim; assert a cache hit skips the network call on
  a repeated lookup of the same name.
- Extend existing scraper fixture tests to verify cooccurrence-pair
  extraction from the real STEG zone/subregion HTML fixtures already in
  the repo.

## Open Questions / Deferred

- Data-floor thresholds (30 notices / 10 localities) are a starting point;
  tuning signals to watch are listed in Inference Pipeline above.
- Matching cluster IDs across daily runs for color stability — deferred to
  v2, detailed in API & Frontend above; cosmetic only, not correctness.
- Hosting (Render + Turso) and locality dedup (normalize/fuzzy/alias) are
  both resolved above, no longer open.
