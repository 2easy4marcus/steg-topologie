-- migrations/0003_cluster_lineage.sql
--
-- Stable cluster identity, lineage, and validation runs.
--
-- Cluster ids come from a persistent monotonic allocator, never from
-- MAX(cluster_id) over surviving rows. Reconstructing from the maximum would
-- reissue ids that retention has already deleted, so two runs months apart
-- could reuse one id for unrelated clusters. The allocator is a singleton row
-- incremented in one atomic statement, matching the no-interactive-transaction
-- pattern the rest of this database uses.
--
-- Lineage records every eligible predecessor relationship, not only the one
-- that supplied the inherited id, so a split or a merge stays legible after
-- the fact. `role` distinguishes them.
--
-- Validation runs are stored with the identities that produced them -- build,
-- config, algorithm, validation version, random seed -- because a score
-- without them cannot be compared against any other score.
--
-- PRAGMA foreign_keys is 0 on this deployment (see 0001_source_registry.sql),
-- so parent-identity requirements are enforced with triggers.

ALTER TABLE cluster_runs ADD COLUMN config_version TEXT;

CREATE TABLE IF NOT EXISTS cluster_id_allocator (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    next_cluster_id INTEGER NOT NULL CHECK (next_cluster_id >= 0)
);

INSERT INTO cluster_id_allocator(singleton_id, next_cluster_id)
SELECT 1, 0
WHERE NOT EXISTS (SELECT 1 FROM cluster_id_allocator WHERE singleton_id = 1);

CREATE TABLE IF NOT EXISTS cluster_lineage (
    run_id TEXT NOT NULL CHECK (length(run_id) > 0),
    cluster_id INTEGER NOT NULL CHECK (cluster_id >= 0),
    previous_run_id TEXT NOT NULL CHECK (length(previous_run_id) > 0),
    previous_cluster_id INTEGER NOT NULL CHECK (previous_cluster_id >= 0),
    jaccard_similarity REAL NOT NULL
        CHECK (jaccard_similarity >= 0.0 AND jaccard_similarity <= 1.0),
    role TEXT NOT NULL
        CHECK (role IN ('inherited', 'split', 'merged', 'related')),
    PRIMARY KEY (run_id, cluster_id, previous_run_id, previous_cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_cluster_lineage_run
ON cluster_lineage(run_id);

CREATE TABLE IF NOT EXISTS cluster_validation_runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
    build_id TEXT NOT NULL CHECK (length(build_id) > 0),
    config_version TEXT NOT NULL CHECK (length(config_version) > 0),
    algorithm_version TEXT NOT NULL CHECK (length(algorithm_version) > 0),
    validation_version TEXT NOT NULL CHECK (length(validation_version) > 0),
    random_seed INTEGER NOT NULL,
    bootstrap_runs INTEGER NOT NULL CHECK (bootstrap_runs >= 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    mean_membership_agreement REAL
        CHECK (mean_membership_agreement IS NULL
               OR (mean_membership_agreement >= 0.0
                   AND mean_membership_agreement <= 1.0)),
    held_out_edge_recall REAL
        CHECK (held_out_edge_recall IS NULL
               OR (held_out_edge_recall >= 0.0
                   AND held_out_edge_recall <= 1.0)),
    raw_cooccurrence_baseline REAL
        CHECK (raw_cooccurrence_baseline IS NULL
               OR (raw_cooccurrence_baseline >= 0.0
                   AND raw_cooccurrence_baseline <= 1.0)),
    geography_baseline REAL
        CHECK (geography_baseline IS NULL
               OR (geography_baseline >= 0.0
                   AND geography_baseline <= 1.0)),
    service_unit_baseline REAL
        CHECK (service_unit_baseline IS NULL
               OR (service_unit_baseline >= 0.0
                   AND service_unit_baseline <= 1.0)),
    largest_notice_removed_agreement REAL
        CHECK (largest_notice_removed_agreement IS NULL
               OR (largest_notice_removed_agreement >= 0.0
                   AND largest_notice_removed_agreement <= 1.0)),
    config_sensitivity_agreement REAL
        CHECK (config_sensitivity_agreement IS NULL
               OR (config_sensitivity_agreement >= 0.0
                   AND config_sensitivity_agreement <= 1.0)),
    split_count INTEGER NOT NULL DEFAULT 0 CHECK (split_count >= 0),
    merge_count INTEGER NOT NULL DEFAULT 0 CHECK (merge_count >= 0),
    report_json TEXT NOT NULL CHECK (length(report_json) > 0),
    evaluated_at TEXT NOT NULL CHECK (length(evaluated_at) > 0)
);

CREATE TRIGGER guard_cluster_lineage_requires_run
BEFORE INSERT ON cluster_lineage
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM cluster_runs WHERE run_id = NEW.run_id
        )
        THEN RAISE(ABORT, 'cluster_lineage references unknown run')
    END;
END;

CREATE TRIGGER guard_cluster_validation_runs_requires_run
BEFORE INSERT ON cluster_validation_runs
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM cluster_runs WHERE run_id = NEW.run_id
        )
        THEN RAISE(ABORT, 'cluster_validation_runs references unknown run')
    END;
END;
