-- migrations/0004_private_candidates.sql
--
-- Two things: the canonical-geography pin, and the private candidate pilot.
--
-- `model_builds.canonical_build_id` is the pin. `locality_context` is keyed on
-- `canonical_build_id`, but nothing tied an evidence build to one canonical
-- import, so any geographic join would have resolved through "whichever
-- canonical import happens to be active right now" -- reintroducing exactly
-- the mid-build mutation the pinned build snapshot removed. Pinning at build
-- creation makes locality -> service unit a deterministic lookup for a given
-- build, which is what activates `build_pair_observations.geographic_confidence`
-- and what lets candidate generation be bounded by accepted geography. The
-- column is nullable: builds created before this migration have no pin, and
-- their geographic confidence stays NULL (unmeasured), never 0 (measured
-- disagreement) and never 1.
--
-- The two candidate tables are private and experimental. `score` is a ranking
-- index, not a probability, and no public route reads either table (see
-- tests/test_openapi_boundaries.py). Every row carries the four identities
-- that produced it -- evidence build, cluster run, topology snapshot, and the
-- model-config plus scoring versions -- plus the one scoring parameter a
-- caller may vary within a scoring version, because a rank without them
-- cannot be compared against any other rank.
--
-- PRAGMA foreign_keys is 0 on this deployment (see 0001_source_registry.sql),
-- so parent-identity requirements are enforced with triggers. One of those
-- triggers is a model rule rather than a reference: scores may only be stored
-- against a run whose status is 'experimental'. A run that failed its evidence
-- gates has no ranking, and the database refuses to hold one for it.

ALTER TABLE model_builds ADD COLUMN canonical_build_id TEXT;

CREATE TABLE IF NOT EXISTS asset_candidate_runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
    cluster_run_id TEXT NOT NULL CHECK (length(cluster_run_id) > 0),
    build_id TEXT NOT NULL CHECK (length(build_id) > 0),
    source_snapshot_id TEXT NOT NULL CHECK (length(source_snapshot_id) > 0),
    config_version TEXT NOT NULL CHECK (length(config_version) > 0),
    scoring_version TEXT NOT NULL CHECK (length(scoring_version) > 0),
    status TEXT NOT NULL
        CHECK (status IN ('experimental', 'insufficient_evidence', 'failed')),
    -- The candidate radius is stored, not just versioned. It is the single
    -- most influential scoring parameter and the pilot exists to recalibrate
    -- it, so two runs at 8km and 20km must not be indistinguishable here.
    -- Bounded above as well as below because `> 0` alone admits infinity.
    radius_km REAL NOT NULL CHECK (radius_km > 0 AND radius_km < 1e6),
    created_at TEXT NOT NULL
        CHECK (
            created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(created_at) IS NOT NULL
            AND (
            substr(created_at, -1) = 'Z'
            OR (
                substr(created_at, -6, 1) IN ('+', '-')
                AND substr(created_at, -3, 1) = ':'
                AND substr(created_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(created_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
        ),
    completed_at TEXT
        CHECK (
            completed_at IS NULL
            OR (
            completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(completed_at) IS NOT NULL
            AND (
            substr(completed_at, -1) = 'Z'
            OR (
                substr(completed_at, -6, 1) IN ('+', '-')
                AND substr(completed_at, -3, 1) = ':'
                AND substr(completed_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(completed_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
            )
        ),
    public_error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_asset_candidate_runs_cluster_run
ON asset_candidate_runs(cluster_run_id);

-- `score` is bounded because it is a convex combination of features that are
-- themselves bounded to [0, 1]; a value outside it means the weights or the
-- features were corrupted before the write.
CREATE TABLE IF NOT EXISTS asset_candidate_scores (
    run_id TEXT NOT NULL CHECK (length(run_id) > 0),
    cluster_id INTEGER NOT NULL CHECK (cluster_id >= 0),
    asset_id TEXT NOT NULL CHECK (length(asset_id) > 0),
    rank INTEGER NOT NULL CHECK (rank >= 1),
    score REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    component_json TEXT NOT NULL CHECK (json_valid(component_json)),
    sensitivity_json TEXT NOT NULL CHECK (json_valid(sensitivity_json)),
    PRIMARY KEY(run_id, cluster_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_candidate_scores_rank
ON asset_candidate_scores(run_id, cluster_id, rank);

CREATE TRIGGER guard_asset_candidate_runs_requires_parents
BEFORE INSERT ON asset_candidate_runs
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM cluster_runs WHERE run_id = NEW.cluster_run_id
        )
        THEN RAISE(ABORT, 'asset_candidate_runs references unknown cluster run')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM model_builds WHERE build_id = NEW.build_id
        )
        THEN RAISE(ABORT, 'asset_candidate_runs references unknown model build')
    END;
END;

CREATE TRIGGER guard_asset_candidate_scores_requires_experimental_run
BEFORE INSERT ON asset_candidate_scores
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM asset_candidate_runs WHERE run_id = NEW.run_id
        )
        THEN RAISE(ABORT, 'asset_candidate_scores references unknown run')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM asset_candidate_runs
            WHERE run_id = NEW.run_id AND status = 'experimental'
        )
        THEN RAISE(
            ABORT, 'asset_candidate_scores run is not an experimental run'
        )
    END;
END;
