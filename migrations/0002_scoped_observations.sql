-- migrations/0002_scoped_observations.sql
--
-- Subregion-scoped evidence observations for Evidence Model V2.
--
-- Scope identity is (notice_id, scope_kind, scope_ordinal) and never the
-- display heading. Two STEG table cells carrying identical heading text are
-- two distinct scopes, and a cell with no heading is still a scope.
-- `scope_name` is display-only: it may repeat inside one notice and it may be
-- NULL. This is why `notice_localities` gains `scope_ordinal` and why the
-- parser version is bumped alongside this migration.
--
-- Each build first pins its own immutable source population into
-- `build_notice_parses` and `build_locality_observations`. Scoped pairs,
-- marginals, readiness metrics, and counts derive only from those rows, so a
-- parser activation landing in the middle of a build cannot change what the
-- build measured. Population is idempotent: every populate step deletes the
-- build's own rows before reinserting them, so a retried build converges on
-- the same population instead of failing on a primary-key conflict.
--
-- Confidence components are stored separately, never blended. Components that
-- have not been measured yet at this stage of the pipeline are NULL rather
-- than defaulted to 1.0: a default would fabricate evidence the build does not
-- have. `geographic_confidence` is one such column -- it is populated once
-- canonical geography is joined, not here.
--
-- PRAGMA foreign_keys is 0 on this deployment (see 0001_source_registry.sql),
-- so parent-identity requirements are enforced with triggers, matching the
-- convention established there.

ALTER TABLE notice_localities ADD COLUMN scope_ordinal INTEGER;

CREATE TABLE IF NOT EXISTS build_notice_parses (
    build_id TEXT NOT NULL CHECK (length(build_id) > 0),
    notice_id TEXT NOT NULL CHECK (length(notice_id) > 0),
    parse_id TEXT NOT NULL CHECK (length(parse_id) > 0),
    outage_date TEXT,
    parse_status TEXT NOT NULL
        CHECK (parse_status IN ('ok', 'warning', 'failed')),
    PRIMARY KEY (build_id, notice_id)
);

CREATE TABLE IF NOT EXISTS build_locality_observations (
    build_id TEXT NOT NULL CHECK (length(build_id) > 0),
    notice_id TEXT NOT NULL CHECK (length(notice_id) > 0),
    scope_kind TEXT NOT NULL
        CHECK (scope_kind IN ('subregion', 'notice_fallback')),
    scope_ordinal INTEGER NOT NULL CHECK (scope_ordinal >= 0),
    scope_name TEXT,
    canonical_name TEXT NOT NULL CHECK (length(canonical_name) > 0),
    PRIMARY KEY (
        build_id, notice_id, scope_kind, scope_ordinal, canonical_name
    )
);

CREATE TABLE IF NOT EXISTS build_pair_observations (
    build_id TEXT NOT NULL CHECK (length(build_id) > 0),
    notice_id TEXT NOT NULL CHECK (length(notice_id) > 0),
    outage_date TEXT,
    scope_kind TEXT NOT NULL
        CHECK (scope_kind IN ('subregion', 'notice_fallback')),
    scope_ordinal INTEGER NOT NULL CHECK (scope_ordinal >= 0),
    scope_name TEXT,
    locality_a TEXT NOT NULL CHECK (length(locality_a) > 0),
    locality_b TEXT NOT NULL CHECK (length(locality_b) > 0),
    parse_confidence REAL NOT NULL
        CHECK (parse_confidence >= 0.0 AND parse_confidence <= 1.0),
    scope_confidence REAL NOT NULL
        CHECK (scope_confidence >= 0.0 AND scope_confidence <= 1.0),
    canonicalization_confidence REAL NOT NULL
        CHECK (canonicalization_confidence >= 0.0
               AND canonicalization_confidence <= 1.0),
    temporal_confidence REAL
        CHECK (temporal_confidence IS NULL
               OR (temporal_confidence >= 0.0 AND temporal_confidence <= 1.0)),
    geographic_confidence REAL
        CHECK (geographic_confidence IS NULL
               OR (geographic_confidence >= 0.0
                   AND geographic_confidence <= 1.0)),
    config_version TEXT NOT NULL CHECK (length(config_version) > 0),
    CHECK (locality_a < locality_b),
    PRIMARY KEY (
        build_id, notice_id, scope_kind, scope_ordinal, locality_a, locality_b
    )
);

CREATE INDEX IF NOT EXISTS idx_pair_observations_build
ON build_pair_observations(build_id);

CREATE INDEX IF NOT EXISTS idx_locality_observations_build
ON build_locality_observations(build_id);

CREATE TABLE IF NOT EXISTS quality_gate_results (
    build_id TEXT NOT NULL CHECK (length(build_id) > 0),
    gate_key TEXT NOT NULL CHECK (length(gate_key) > 0),
    outcome TEXT NOT NULL
        CHECK (outcome IN ('pass', 'warn', 'fail', 'quarantine')),
    measured_value REAL,
    required_value REAL,
    reason_code TEXT NOT NULL CHECK (length(reason_code) > 0),
    config_version TEXT NOT NULL CHECK (length(config_version) > 0),
    evaluated_at TEXT NOT NULL CHECK (length(evaluated_at) > 0),
    PRIMARY KEY (build_id, gate_key)
);

CREATE TABLE IF NOT EXISTS publication_decisions (
    product_type TEXT NOT NULL
        CHECK (product_type IN ('evidence_build', 'cluster_run')),
    product_id TEXT NOT NULL CHECK (length(product_id) > 0),
    build_id TEXT,
    decision TEXT NOT NULL
        CHECK (decision IN ('published', 'experimental', 'blocked')),
    reason_code TEXT NOT NULL CHECK (length(reason_code) > 0),
    config_version TEXT NOT NULL CHECK (length(config_version) > 0),
    decided_at TEXT NOT NULL CHECK (length(decided_at) > 0),
    PRIMARY KEY (product_type, product_id)
);

-- Required parent/build identity. A build's derived rows may only exist for a
-- build that exists, and a pair observation may only exist for a notice this
-- build actually pinned.

CREATE TRIGGER guard_build_notice_parses_requires_build
BEFORE INSERT ON build_notice_parses
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM model_builds WHERE build_id = NEW.build_id
        )
        THEN RAISE(ABORT, 'build_notice_parses references unknown build')
    END;
END;

CREATE TRIGGER guard_build_locality_observations_requires_pin
BEFORE INSERT ON build_locality_observations
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM build_notice_parses
            WHERE build_id = NEW.build_id AND notice_id = NEW.notice_id
        )
        THEN RAISE(ABORT,
                   'build_locality_observations references unpinned notice')
    END;
END;

CREATE TRIGGER guard_build_pair_observations_requires_pin
BEFORE INSERT ON build_pair_observations
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM build_notice_parses
            WHERE build_id = NEW.build_id AND notice_id = NEW.notice_id
        )
        THEN RAISE(ABORT,
                   'build_pair_observations references unpinned notice')
    END;
END;

CREATE TRIGGER guard_quality_gate_results_requires_build
BEFORE INSERT ON quality_gate_results
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM model_builds WHERE build_id = NEW.build_id
        )
        THEN RAISE(ABORT, 'quality_gate_results references unknown build')
    END;
END;

-- INSERT twins. SQLite fires BEFORE INSERT for INSERT OR REPLACE, but not for
-- a plain UPDATE or for ON CONFLICT DO UPDATE, so an insert-only guard leaves
-- the reference it protects free to be rewritten to a parent that does not
-- exist. With PRAGMA foreign_keys at 0 these triggers are the whole integrity
-- model, so each one needs both halves.

CREATE TRIGGER guard_build_notice_parses_update_references
BEFORE UPDATE OF build_id ON build_notice_parses
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM model_builds WHERE build_id = NEW.build_id
        )
        THEN RAISE(ABORT, 'build_notice_parses references unknown build')
    END;
END;

CREATE TRIGGER guard_build_locality_observations_update_references
BEFORE UPDATE OF build_id, notice_id ON build_locality_observations
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM build_notice_parses
            WHERE build_id = NEW.build_id AND notice_id = NEW.notice_id
        )
        THEN RAISE(ABORT,
                   'build_locality_observations references unpinned notice')
    END;
END;

CREATE TRIGGER guard_build_pair_observations_update_references
BEFORE UPDATE OF build_id, notice_id ON build_pair_observations
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM build_notice_parses
            WHERE build_id = NEW.build_id AND notice_id = NEW.notice_id
        )
        THEN RAISE(ABORT,
                   'build_pair_observations references unpinned notice')
    END;
END;

CREATE TRIGGER guard_quality_gate_results_update_references
BEFORE UPDATE OF build_id ON quality_gate_results
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM model_builds WHERE build_id = NEW.build_id
        )
        THEN RAISE(ABORT, 'quality_gate_results references unknown build')
    END;
END;
