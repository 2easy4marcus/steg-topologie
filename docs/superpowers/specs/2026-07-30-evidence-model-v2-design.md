# Evidence Model V2 — Technical Design

**Date:** 2026-07-30  
**Status:** Approved for implementation planning  
**Target branch:** `feat/evidence-model-v2`

## 1. Purpose

Upgrade Tunisia Outage Tracker from a notice co-occurrence prototype into a
traceable evidence pipeline with two model products:

1. national statistical service-zone clusters; and
2. a private Sfax/Kerkennah pilot that ranks plausible feeder or substation
   candidates.

The system must never present an inferred cluster or ranked asset as confirmed
STEG topology. Every result must identify its evidence build, quality state,
provenance, and uncertainty.

This specification covers data ingestion, evidence modeling, model methods,
validation, observability, local Docker execution, tests, and delivery workflow.
The public UI redesign and production infrastructure migration will receive
separate specifications after this work.

## 2. Goals

- Register every source with ownership, retrieval time, license state,
  checksum, coverage, and refresh policy.
- Preserve immutable raw source files and STEG page snapshots.
- Convert heterogeneous sources into explicit canonical entities.
- Quarantine malformed or provenance-uncertain records instead of silently
  repairing or discarding them.
- Prevent failed evidence or model builds from replacing the last known-good
  active build.
- Correct co-occurrence construction so table subregions define the primary
  outage scope.
- Produce stable, evidence-backed national clusters with uncertainty and
  temporal validation.
- Produce private, explainable Sfax/Kerkennah asset rankings when evidence is
  sufficient.
- Provide protected API monitoring, internal documentation, and repeatable
  Postman/Newman smoke tests.
- Run locally through Docker with local libSQL by default and optional Turso
  configuration.

## 3. Non-goals

- Claiming the real location or identity of a transformer, feeder, or
  substation.
- Publishing raw OSM-derived grid topology or private candidate rankings.
- Treating administrative boundaries as electrical boundaries.
- Using the 2014 blackout report as ordinary training data.
- Redesigning the public dashboard or model page in this technical change.
- Migrating away from Render or Turso in this change.
- Building a general-purpose GIS platform.

## 4. Available sources and intended roles

### 4.1 STEG outage notices

Primary observed-event evidence. A notice contributes its title, source URL,
date, time window, macro-region, subregion/table-column structure, extracted
localities, raw snapshot, parser version, and parse outcome.

Notices do not confirm topology. Editorial grouping is evidence with an
explicit scope-confidence value.

### 4.2 `delegations.geojson`

Canonical administrative geometry for delegation-level spatial joins. The
current file contains 272 features. Its source and license must be registered
before it can influence a public build.

### 4.3 `delegations.json`

TopoJSON alternative containing 271 geometries. It is not a second independent
source. The implementation must compare identifiers and geometry coverage,
record the discrepancy, then designate one canonical boundary artifact.

### 4.4 `tncirconscriptions.geojson`

Electoral constituencies. This source is not a grid or service-area proxy and
is excluded from model features by default. It may be retained in the registry
for future presentation or coverage analysis.

### 4.5 `tnlistedistrictsteg.xls`

STEG service organization prior containing 39 districts and 83 agencies.
Coordinates are incomplete: 45 rows lack coordinate pairs and one Gafsa
longitude is malformed. Valid coordinates may support service-unit proximity
features. Missing coordinates remain valid incomplete records. The malformed
coordinate must be quarantined until corrected from a cited source.

### 4.6 `tunisia.geojson`

Country boundary for map clipping and broad validation. It does not contribute
topology evidence.

### 4.7 `tunisia-260725.osm.pbf`

Private topology prior for the Sfax/Kerkennah pilot. It must not be baked into
the application image or committed to ordinary Git history. Its checksum,
extract date, geographic coverage, OSM attribution, and extraction commands
must be registered.

### 4.8 2014 blackout commission report

Event-chain case study describing protection, generation loss,
interconnection, load shedding, and restoration behavior. It is used to test
event-chain representation and explanation quality, not to create repeated
training observations.

## 5. Layered architecture

### 5.1 Source registry

Each source has:

- stable source identifier;
- title and owner;
- source URL or acquisition description;
- retrieval timestamp;
- content checksum;
- format and schema version;
- temporal and geographic coverage;
- license and publication status;
- refresh policy;
- public/private classification.

An unknown or incompatible license restricts the source to private research.

### 5.2 Raw evidence vault

Raw files and STEG HTML snapshots are immutable and content-addressed. A
re-download with different bytes creates a new artifact version. Model code
never consumes arbitrary files directly; it consumes accepted canonical
records linked to raw artifacts.

STEG HTML snapshots remain in the evidence database because they are small and
already participate in notice rollback. Static GIS, spreadsheet, PDF, and PBF
artifacts remain files referenced by registry path and checksum; they are not
copied into database BLOB fields. The PBF is supplied through a private,
read-only local mount.

### 5.3 Canonical data layer

The canonical layer contains:

- `DatasetSource`
- `SourceArtifact`
- `SourceSnapshot`
- `OutageEvent`
- `OutageScope`
- `Locality`
- `LocalityAlias`
- `AdministrativeArea`
- `ServiceUnit`
- `GridAsset` (private)
- `EvidenceObservation`
- `EvidenceBuild`
- `QualityGateResult`
- `NationalClusterRun`
- `ClusterMembership`
- `AssetCandidateRun` (private)
- `AssetCandidateScore` (private)
- `PublicationDecision`

Every derived entity records source identifiers, transformation version,
confidence, creation time, and build identifier.

The source registry lives at `docs/data/sources.yaml` and is validated through
Pydantic before ingestion. Database evolution uses ordered, idempotent SQL
migrations recorded in a `schema_migrations` table. New evidence tables are not
added only through an ever-growing startup schema list.

### 5.4 Evidence build

An evidence build freezes:

- accepted artifact versions;
- parser and normalizer versions;
- canonical aliases;
- accepted and quarantined observations;
- geography versions;
- feature configuration;
- gate results.

Builds are immutable. A completed build becomes active only after validation.
A failed build remains inspectable and cannot replace the last known-good
active build.

### 5.5 Model products

Track A produces national locality/service-zone clusters. Track B privately
ranks plausible Sfax/Kerkennah grid assets that may explain Track A clusters.
Track B consumes a specific Track A run and cannot silently use a different
evidence build.

### 5.6 Publication boundary

Public outputs may contain cluster membership, stability, coverage, readiness
gates, evidence links, methodology, and uncertainty. Exact private topology,
raw uncertain sources, and feeder/substation candidate rankings remain
restricted.

## 6. Quality gates

Every gate returns `pass`, `warn`, `fail`, or `quarantine`, plus a stable reason
code, measured value, required value, and evidence references.

### 6.1 Source gates

- Checksum and retrieval metadata are mandatory.
- Missing license metadata blocks public use.
- Unsupported schemas fail ingestion without changing the active build.
- Duplicate artifacts with the same checksum reuse the existing raw version.

### 6.2 Geometry and coordinate gates

- GeoJSON must parse and use an identified coordinate reference system.
- Invalid geometries are repaired only through a recorded, deterministic
  transformation; otherwise they are quarantined.
- Point coordinates must fall within configured Tunisian bounds.
- Missing service-unit coordinates produce incomplete records, not fabricated
  coordinates.
- The malformed Gafsa longitude is quarantined until a cited correction exists.

### 6.3 Notice parsing gates

- Snapshot retrieval and parsing are independent outcomes.
- Title, date, outage window, subregion names, and locality extraction have
  separate status fields.
- The latest failed parse cannot erase an older valid parse.
- Header detection uses semantic text patterns, not bold formatting.
- Source table-column/subregion boundaries are preserved.

### 6.4 Model-readiness gates

The initial activation thresholds remain:

- at least 30 valid notices;
- at least 15 distinct outage dates;
- at least 10 unique localities;
- at least 20 locality pairs observed repeatedly;
- at least 80% valid active parses;
- at least 80% recent parse success;
- no single notice contributing more than 20% of all pair observations.

Thresholds are configuration with versioned rationale, not permanent constants.
Changes require backtest evidence and a new model configuration version.

### 6.5 Candidate-ranking gates

The private Sfax/Kerkennah model requires:

- a registered OSM snapshot with known coverage;
- accepted cluster geography;
- at least two independent temporal observations supporting the relationship;
- at least one topology or service-unit feature;
- an explicit source-completeness score.

When evidence is inadequate, the required result is `insufficient_evidence`,
not a forced ranking.

## 7. Feature engineering

### 7.1 Outage scope correction

Localities in the same STEG table column/subregion form the primary
co-occurrence scope. Localities in different columns do not receive a
full-strength pair observation merely because they appear in one notice.

If a notice has no recoverable subregion structure, whole-notice pairing is
allowed as a lower-confidence fallback. Every pair contribution records:

- notice and outage date;
- subregion or fallback scope;
- parse confidence;
- scope confidence;
- canonicalization confidence;
- geographic confidence.

This prevents large notices and inconsistent source formatting from silently
creating strong false edges.

### 7.2 Temporal features

- distinct notice count;
- distinct outage-date count;
- first and last observation date;
- recency;
- recurrence across independent dates;
- outage-window overlap;
- optional seasonal and weekday indicators once coverage supports them.

Multiple notices from the same outage date cannot masquerade as independent
temporal confirmation.

### 7.3 Geographic and service-unit features

- delegation membership;
- delegation adjacency;
- locality centroid distance;
- shared or nearby STEG district/agency;
- service-unit coordinate completeness;
- spatial-join confidence.

These are bounded regularizers. They cannot create a cluster edge without
outage evidence.

### 7.4 Private topology features

For Sfax/Kerkennah:

- asset type and voltage where available;
- line/substation connectivity;
- graph distance;
- geographic distance;
- topology component membership;
- source age and completeness;
- plausible service catchment.

OSM absence is not evidence that an asset does not exist.

## 8. Track A — national clustering

The model builds a weighted locality graph. Edge weights combine:

- positive pointwise mutual information;
- recurrence across distinct outage dates;
- parse confidence;
- subregion-scope confidence;
- canonical-name confidence;
- bounded geographic/service-unit agreement.

Community detection runs across deterministic bootstrap samples and a
versioned parameter set. A locality receives membership probability and
stability, not only a hard cluster identifier.

Cluster identifiers are matched across runs using member-overlap similarity.
The matcher computes Jaccard similarity between previous and new cluster member
sets, then performs maximum-weight one-to-one matching. A previous identifier
is reused only when Jaccard similarity is at least 0.50. Otherwise a new,
monotonically allocated identifier is created. During a split, only the
best-matching child may inherit the old identifier. During a merge, the
best-matching predecessor supplies the identifier. All significant
predecessor relationships are retained as lineage records even when they do
not control the identifier.

Public output includes edge evidence, run/build identifiers, algorithm version,
stability, coverage, and publication state.

## 9. Track B — private Sfax/Kerkennah candidate ranking

The pilot extracts plausible OSM grid assets and connectivity for the selected
region. Candidate generation is bounded by geography and topology before
scoring.

Each candidate score decomposes into:

- outage-cluster fit;
- topology consistency;
- STEG service-unit prior;
- geographic distance;
- repeated temporal support;
- source coverage/completeness penalty.

Components are normalized to comparable ranges and combined through a
versioned monotonic scoring configuration. The first pilot reports both the
configured ranking and sensitivity to reasonable weight changes. Scores are
ranking indices, not calibrated probabilities, unless future confirmed labels
support calibration.

The result contains competing candidates and component scores. It is an
experimental ranking, never a confirmed physical identity. No exact candidate
data enters public APIs in this phase.

## 10. Validation

### 10.1 Temporal holdout

Model evaluation trains on earlier outage dates and evaluates later dates.
Random notice splitting is prohibited because notices from the same period can
leak near-duplicate information.

### 10.2 Baselines

Track A must outperform or explain differences from:

- raw co-occurrence;
- geography-only grouping;
- STEG-district-only grouping.

### 10.3 Robustness

Measure:

- cluster agreement across bootstrap samples;
- sensitivity to parser-confidence thresholds;
- sensitivity to removal of the largest notices;
- split/merge frequency;
- membership uncertainty;
- held-out pair recovery.

### 10.4 Case-study validation

The 2014 blackout report tests whether the evidence model can represent an
event chain and distinguish transmission, generation, protection,
interconnection, shedding, and restoration evidence. It does not contribute
ordinary co-occurrence counts.

## 11. Observability and API documentation

### 11.1 Protected operations interface

A dedicated `OPS_SECRET`, separate from `CRON_SECRET`, protects operational
endpoints and the functional internal page.

The operations view exposes:

- request rate;
- p50 and p95 latency;
- 2xx, 4xx, and 5xx counts;
- slow route templates;
- Turso latency and failures;
- scrape, backfill, evidence-build, cluster, and candidate-run state;
- active and last known-good build identifiers;
- current readiness gates;
- sanitized recent events and request IDs.

It never displays tokens, authorization headers, request bodies, citizen
comments, or internal stack traces.

Recent request samples use a bounded in-memory ring buffer. Structured logs go
to Render. Only job events and low-frequency aggregates are persisted; the
application must not write one Turso row per public request.

### 11.2 API documentation

The application provides:

- a public OpenAPI schema containing safe public endpoints;
- an `OPS_SECRET`-protected internal schema for jobs and diagnostics;
- request/response and stable-error examples;
- explicit API and schema versions.

### 11.3 Postman and Newman

Postman collections are generated from OpenAPI. Repository environment
templates contain variable names and safe defaults but no secret values.
Newman runs smoke tests in CI against the local Docker service.

Postman is a test client, not the production monitoring system.

## 12. Docker development contract

- Base image: pinned Python 3.13 slim variant.
- Runtime user: non-root.
- Database: local file-backed libSQL by default.
- Persistence: named Docker volume.
- Turso: enabled only when explicit URL and token variables are supplied.
- Source data: `docs/data` mounted read-only.
- Large OSM PBF: excluded from image and ordinary Git history.
- Configuration: `.env.example` with no secrets.
- Health check: public application status endpoint.
- Tests: separate Compose test profile runs the complete suite.
- Build context: `.dockerignore` excludes Git metadata, caches, local
  databases, secrets, raw large artifacts, and generated outputs.

The local path must not contact production Turso unless the developer
deliberately supplies production variables.

## 13. Failure and recovery behavior

- Bad records are quarantined with stable reason codes and source references.
- Parsing or normalization failure does not destroy older valid evidence.
- Failed evidence builds remain inactive.
- Failed model runs do not replace the last known-good model.
- Public responses identify stale results when the active evidence build is
  newer than the published model.
- Jobs use locks, heartbeats, bounded retries, and idempotent writes.
- Public errors expose stable codes; internal logs retain request IDs and
  sanitized details.
- Candidate ranking may return `insufficient_evidence`.

## 14. Delivery sequence

### Phase 0 — reproducibility and source governance

Add Docker, Compose, test profile, source registry, checksums, licenses,
dataset README, fixture strategy, and large-artifact exclusions.

### Phase 1 — visibility

Add request metrics, protected operations APIs and functional page, split
OpenAPI schemas, generated Postman collection, and Newman smoke tests.

### Phase 2 — canonical data

Import and validate boundaries, aliases, STEG service units, provenance,
geometry, coordinates, and quarantine records.

### Phase 3 — evidence build

Add subregion-scoped observations, fallback confidence, immutable evidence
builds, and activation gates.

### Phase 4 — national cluster V2

Implement weighted graph construction, stable cluster identity, bootstrap
stability, temporal validation, baselines, lineage, and public-safe outputs.

### Phase 5 — private Sfax/Kerkennah pilot

Extract the private OSM topology prior, generate candidates, score evidence,
validate explanations, and retain results behind internal authorization.

## 15. Testing strategy

### Data tests

- source manifest and checksum validation;
- schema and coordinate validation;
- geometry and spatial joins;
- bilingual alias normalization;
- malformed and missing coordinate behavior;
- quarantine reason codes;
- immutable artifact behavior.

### Model tests

- same-subregion and fallback edge construction;
- no accidental cross-column full-strength edges;
- PPMI and recurrence calculations;
- distinct-date semantics;
- large-notice contribution controls;
- deterministic seeded runs;
- stable cluster matching and split/merge lineage;
- temporal holdout and baseline comparison;
- insufficient-evidence candidate behavior.

### System and security tests

- Docker build, startup, persistence, and health check;
- local database isolation from Turso;
- public/internal OpenAPI separation;
- `OPS_SECRET` and `CRON_SECRET` separation;
- no secret or sensitive-body logging;
- failed-build rollback;
- job lock and retry behavior;
- Newman smoke tests;
- existing API regression suite.

## 16. Git and pull-request workflow

Implementation occurs on `feat/evidence-model-v2`. Commits remain small and
phase-oriented. Existing untracked research files are never staged through a
broad `git add -A`.

The pull request includes:

- architecture and migration summary;
- source/provenance changes;
- test and validation results;
- screenshots of the functional operations page;
- public/private API changes;
- rollback procedure;
- known evidence limitations;
- confirmation that raw topology and secrets are absent from Git history.

## 17. Acceptance criteria

The technical upgrade is ready to merge when:

1. a clean checkout runs locally through Docker without Turso credentials;
2. all existing and new tests pass in the Docker test profile;
3. every active model observation traces to a registered source artifact;
4. malformed source records are quarantined rather than silently corrected;
5. cross-subregion notice structure no longer creates full-strength false
   co-occurrence edges;
6. a failed build cannot replace the active model;
7. Track A reports temporal validation, uncertainty, stability, and lineage;
8. Track B remains private and supports `insufficient_evidence`;
9. internal operations and documentation require `OPS_SECRET`;
10. public logs and APIs expose no secrets, raw private topology, or internal
    candidate rankings;
11. the PR documents rollback and all remaining limitations.
