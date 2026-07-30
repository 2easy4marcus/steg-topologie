-- Source-provenance registry and canonical-build staging.
--
-- Parity note: for the three registry tables (dataset_sources,
-- source_artifacts, quarantine_records) the CHECK constraints below mirror,
-- one for one, the Pydantic contracts in app/data/models.py.  Identifier
-- columns use the same ^[a-z0-9][a-z0-9_-]*$ rule (expressed as a pair of
-- GLOBs, which are case-sensitive in SQLite); non-empty text columns mirror
-- `min_length=1`; timestamp columns mirror the "must be timezone-aware"
-- validator by requiring a trailing `Z` or a `+HH:MM` / `-HH:MM` offset AND
-- being parseable by SQLite's datetime(), so a bare 'Z' is rejected.  The
-- canonical-build tables have no Pydantic counterpart yet; SQL is their only
-- contract.
--
-- PRAGMA foreign_keys is 0 and PRAGMA recursive_triggers is 0, so every
-- REFERENCES clause here is documentation and all integrity is trigger-only.
-- Because DELETE triggers do NOT fire for INSERT OR REPLACE conflict
-- resolution with recursive_triggers off, REPLACE would silently delete rows
-- past every restrict_/immutable_ trigger.  Each table those triggers protect
-- therefore has a BEFORE INSERT duplicate-key check, which fires *before*
-- conflict resolution and so turns INSERT OR REPLACE (and INSERT OR IGNORE,
-- and upserts) into an abort.  Rewrite a row with an explicit UPDATE.
--
-- Two consequences worth stating up front: canonical_builds.status may only go
-- building -> completed|failed and is frozen entirely while canonical_state
-- points at the build, and canonical_state's singleton row is seeded at the
-- bottom of this file so activation is a plain UPDATE that affects one row.

CREATE TABLE IF NOT EXISTS dataset_sources (
    source_id TEXT PRIMARY KEY
        CHECK (
            source_id GLOB '[a-z0-9]*'
            AND source_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    title TEXT NOT NULL CHECK (length(title) > 0),
    owner TEXT NOT NULL CHECK (length(owner) > 0),
    source_url TEXT,
    geographic_coverage TEXT,
    temporal_coverage TEXT,
    license_id TEXT CHECK (license_id IS NULL OR length(trim(license_id)) > 0),
    publication_class TEXT NOT NULL
        CHECK (publication_class IN ('public', 'private_research')),
    refresh_policy TEXT NOT NULL CHECK (length(refresh_policy) > 0),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    acquisition_description TEXT NOT NULL
        CHECK (length(acquisition_description) > 0),
    CHECK (
        publication_class != 'public'
        OR (license_id IS NOT NULL AND length(trim(license_id)) > 0)
    )
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    artifact_id TEXT PRIMARY KEY
        CHECK (
            artifact_id GLOB '[a-z0-9]*'
            AND artifact_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    source_id TEXT NOT NULL REFERENCES dataset_sources(source_id)
        CHECK (
            source_id GLOB '[a-z0-9]*'
            AND source_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    relative_path TEXT NOT NULL CHECK (length(relative_path) > 0),
    checksum_sha256 TEXT NOT NULL
        CHECK (
            length(checksum_sha256) = 64
            AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    byte_size INTEGER NOT NULL
        CHECK (typeof(byte_size) = 'integer' AND byte_size >= 0),
    retrieved_at TEXT NOT NULL
        CHECK (
            retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(retrieved_at) IS NOT NULL
            AND (
            substr(retrieved_at, -1) = 'Z'
            OR (
                substr(retrieved_at, -6, 1) IN ('+', '-')
                AND substr(retrieved_at, -3, 1) = ':'
                AND substr(retrieved_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(retrieved_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
        ),
    registered_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        CHECK (
            registered_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(registered_at) IS NOT NULL
            AND (
            substr(registered_at, -1) = 'Z'
            OR (
                substr(registered_at, -6, 1) IN ('+', '-')
                AND substr(registered_at, -3, 1) = ':'
                AND substr(registered_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(registered_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
        ),
    media_type TEXT NOT NULL CHECK (length(media_type) > 0),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    license_id TEXT CHECK (license_id IS NULL OR length(trim(license_id)) > 0),
    UNIQUE (source_id, checksum_sha256)
);

-- Quarantine records are deliberately mutable: a record can be re-triaged
-- (reason_code / safe_detail corrected) and nothing references it, so it gets
-- no append-only trigger.  Only its foreign keys are guarded.
CREATE TABLE IF NOT EXISTS quarantine_records (
    quarantine_id TEXT PRIMARY KEY
        CHECK (
            quarantine_id GLOB '[a-z0-9]*'
            AND quarantine_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    source_id TEXT NOT NULL REFERENCES dataset_sources(source_id)
        CHECK (
            source_id GLOB '[a-z0-9]*'
            AND source_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    artifact_id TEXT REFERENCES source_artifacts(artifact_id)
        CHECK (
            artifact_id IS NULL
            OR (
                artifact_id GLOB '[a-z0-9]*'
                AND artifact_id NOT GLOB '*[^a-z0-9_-]*'
            )
        ),
    record_key TEXT NOT NULL CHECK (length(record_key) > 0),
    reason_code TEXT NOT NULL CHECK (length(reason_code) > 0),
    safe_detail TEXT NOT NULL CHECK (length(safe_detail) > 0),
    quarantined_at TEXT NOT NULL
        CHECK (
            quarantined_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(quarantined_at) IS NOT NULL
            AND (
            substr(quarantined_at, -1) = 'Z'
            OR (
                substr(quarantined_at, -6, 1) IN ('+', '-')
                AND substr(quarantined_at, -3, 1) = ':'
                AND substr(quarantined_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(quarantined_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
        )
);

-- A canonical build owns a complete staged copy of the reference geography.
-- Rows are written under `building`, validated, then marked `completed`
-- (or `failed`).  Only a `completed` build may become the active build.
CREATE TABLE IF NOT EXISTS canonical_builds (
    canonical_build_id TEXT PRIMARY KEY
        CHECK (
            canonical_build_id GLOB '[a-z0-9]*'
            AND canonical_build_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    status TEXT NOT NULL
        CHECK (status IN ('building', 'completed', 'failed')),
    started_at TEXT NOT NULL
        CHECK (
            started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(started_at) IS NOT NULL
            AND (
            substr(started_at, -1) = 'Z'
            OR (
                substr(started_at, -6, 1) IN ('+', '-')
                AND substr(started_at, -3, 1) = ':'
                AND substr(started_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(started_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
        ),
    finished_at TEXT
        CHECK (
            finished_at IS NULL
            OR (
            finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(finished_at) IS NOT NULL
            AND (
            substr(finished_at, -1) = 'Z'
            OR (
                substr(finished_at, -6, 1) IN ('+', '-')
                AND substr(finished_at, -3, 1) = ':'
                AND substr(finished_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(finished_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
            )
        ),
    failure_reason TEXT
        CHECK (failure_reason IS NULL OR length(failure_reason) > 0),
    CHECK (
        (status = 'building'
            AND finished_at IS NULL AND failure_reason IS NULL)
        OR (status = 'completed'
            AND finished_at IS NOT NULL AND failure_reason IS NULL)
        OR (status = 'failed' AND finished_at IS NOT NULL)
    )
);

-- Singleton pointer to the build every read resolves through.  Switching the
-- active build is a single guarded UPDATE of `active_build_id`; a failed
-- build never touches this row.
CREATE TABLE IF NOT EXISTS canonical_state (
    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
    active_build_id TEXT
        REFERENCES canonical_builds(canonical_build_id),
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        CHECK (
            updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]*'
            AND datetime(updated_at) IS NOT NULL
            AND (
            substr(updated_at, -1) = 'Z'
            OR (
                substr(updated_at, -6, 1) IN ('+', '-')
                AND substr(updated_at, -3, 1) = ':'
                AND substr(updated_at, -5, 2) GLOB '[0-9][0-9]'
                AND substr(updated_at, -2, 2) GLOB '[0-9][0-9]'
            )
            )
        )
);

-- `canonical_build_id` leads the primary key on every canonical table: the
-- geography is staged per build, so a new build must be able to stage the same
-- area/unit ids as the currently active one.  Every id reference into these
-- tables is therefore build-scoped too.
CREATE TABLE IF NOT EXISTS administrative_areas (
    area_id TEXT NOT NULL
        CHECK (
            area_id GLOB '[a-z0-9]*'
            AND area_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    canonical_build_id TEXT NOT NULL
        REFERENCES canonical_builds(canonical_build_id),
    area_level TEXT NOT NULL CHECK (length(area_level) > 0),
    name_ar TEXT NOT NULL CHECK (length(name_ar) > 0),
    name_fr TEXT,
    -- resolves to (canonical_build_id, parent_area_id) in this same build;
    -- a single-column REFERENCES clause cannot express that, so the guard
    -- triggers below are the only statement of the constraint.
    parent_area_id TEXT,
    geometry_wkt TEXT NOT NULL CHECK (length(geometry_wkt) > 0),
    source_id TEXT NOT NULL REFERENCES dataset_sources(source_id),
    artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
    source_record_key TEXT NOT NULL CHECK (length(source_record_key) > 0),
    transformation_version TEXT NOT NULL
        CHECK (length(transformation_version) > 0),
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
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    PRIMARY KEY (canonical_build_id, area_id)
);

CREATE TABLE IF NOT EXISTS service_units (
    unit_id TEXT NOT NULL
        CHECK (
            unit_id GLOB '[a-z0-9]*'
            AND unit_id NOT GLOB '*[^a-z0-9_-]*'
        ),
    canonical_build_id TEXT NOT NULL
        REFERENCES canonical_builds(canonical_build_id),
    unit_type TEXT NOT NULL CHECK (length(unit_type) > 0),
    name TEXT NOT NULL CHECK (length(name) > 0),
    region TEXT NOT NULL CHECK (length(region) > 0),
    governorate TEXT NOT NULL CHECK (length(governorate) > 0),
    latitude REAL CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)),
    longitude REAL
        CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180)),
    coordinate_complete INTEGER NOT NULL
        CHECK (coordinate_complete IN (0, 1)),
    source_id TEXT NOT NULL REFERENCES dataset_sources(source_id),
    artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
    source_record_key TEXT NOT NULL CHECK (length(source_record_key) > 0),
    transformation_version TEXT NOT NULL
        CHECK (length(transformation_version) > 0),
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
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (
        (coordinate_complete = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL)
        OR
        (coordinate_complete = 0 AND latitude IS NULL AND longitude IS NULL)
    ),
    PRIMARY KEY (canonical_build_id, unit_id)
);

-- Locality context is scoped to the build that staged it, exactly like the
-- two tables above, and its area/unit references resolve within that build.
CREATE TABLE IF NOT EXISTS locality_context (
    canonical_build_id TEXT NOT NULL
        REFERENCES canonical_builds(canonical_build_id),
    locality TEXT NOT NULL CHECK (length(locality) > 0),
    context_build_id TEXT NOT NULL CHECK (length(context_build_id) > 0),
    -- both resolve within this row's canonical_build_id (see guards below)
    delegation_area_id TEXT,
    service_unit_id TEXT,
    spatial_confidence REAL NOT NULL
        CHECK (spatial_confidence >= 0 AND spatial_confidence <= 1),
    source_id TEXT NOT NULL REFERENCES dataset_sources(source_id),
    artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
    transformation_version TEXT NOT NULL
        CHECK (length(transformation_version) > 0),
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
    PRIMARY KEY (canonical_build_id, locality)
);

-- Every guard/restrict trigger below does an EXISTS lookup on a referencing
-- column; each of those columns is indexed here so the guards stay O(log n).
-- Lookups by canonical_build_id alone need no index of their own: it is the
-- leading column of each canonical table's primary key.

CREATE INDEX IF NOT EXISTS idx_source_artifacts_source
ON source_artifacts(source_id);

CREATE INDEX IF NOT EXISTS idx_quarantine_source_artifact
ON quarantine_records(source_id, artifact_id);

CREATE INDEX IF NOT EXISTS idx_quarantine_records_artifact
ON quarantine_records(artifact_id);

CREATE INDEX IF NOT EXISTS idx_administrative_areas_parent
ON administrative_areas(canonical_build_id, parent_area_id);

CREATE INDEX IF NOT EXISTS idx_administrative_areas_source
ON administrative_areas(source_id);

CREATE INDEX IF NOT EXISTS idx_administrative_areas_artifact
ON administrative_areas(artifact_id);

CREATE INDEX IF NOT EXISTS idx_administrative_areas_canonical_build
ON administrative_areas(canonical_build_id);

CREATE INDEX IF NOT EXISTS idx_service_units_source
ON service_units(source_id);

CREATE INDEX IF NOT EXISTS idx_service_units_artifact
ON service_units(artifact_id);

CREATE INDEX IF NOT EXISTS idx_service_units_canonical_build
ON service_units(canonical_build_id);

CREATE INDEX IF NOT EXISTS idx_locality_context_build
ON locality_context(context_build_id);

CREATE INDEX IF NOT EXISTS idx_locality_context_canonical_build
ON locality_context(canonical_build_id);

CREATE INDEX IF NOT EXISTS idx_locality_context_source
ON locality_context(source_id);

CREATE INDEX IF NOT EXISTS idx_locality_context_artifact
ON locality_context(artifact_id);

CREATE INDEX IF NOT EXISTS idx_locality_context_delegation_area
ON locality_context(canonical_build_id, delegation_area_id);

CREATE INDEX IF NOT EXISTS idx_locality_context_service_unit
ON locality_context(canonical_build_id, service_unit_id);

CREATE INDEX IF NOT EXISTS idx_canonical_builds_status
ON canonical_builds(status);

CREATE INDEX IF NOT EXISTS idx_canonical_state_active_build
ON canonical_state(active_build_id);

CREATE TRIGGER guard_dataset_sources_insert_duplicate
BEFORE INSERT ON dataset_sources
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'dataset_sources row already exists')
    END;
END;

CREATE TRIGGER guard_canonical_builds_insert_duplicate
BEFORE INSERT ON canonical_builds
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'canonical_builds row already exists')
    END;
END;

CREATE TRIGGER guard_source_artifacts_insert_references
BEFORE INSERT ON source_artifacts
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
        )
        THEN RAISE(ABORT, 'source_artifacts row already exists')
    END;
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE source_id = NEW.source_id
              AND checksum_sha256 = NEW.checksum_sha256
        )
        THEN RAISE(ABORT, 'source_artifacts checksum already registered')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'source_artifacts.source_id missing')
    END;
END;

CREATE TRIGGER guard_source_artifacts_update_references
BEFORE UPDATE OF source_id ON source_artifacts
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'source_artifacts.source_id missing')
    END;
END;

CREATE TRIGGER guard_quarantine_records_insert_references
BEFORE INSERT ON quarantine_records
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM quarantine_records
            WHERE quarantine_id = NEW.quarantine_id
        )
        THEN RAISE(ABORT, 'quarantine_records row already exists')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'quarantine_records.source_id missing')
    END;
    SELECT CASE
        WHEN NEW.artifact_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM source_artifacts
                 WHERE artifact_id = NEW.artifact_id
                   AND source_id = NEW.source_id
             )
        THEN RAISE(ABORT, 'quarantine_records.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_quarantine_records_update_references
BEFORE UPDATE OF source_id, artifact_id ON quarantine_records
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'quarantine_records.source_id missing')
    END;
    SELECT CASE
        WHEN NEW.artifact_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM source_artifacts
                 WHERE artifact_id = NEW.artifact_id
                   AND source_id = NEW.source_id
             )
        THEN RAISE(ABORT, 'quarantine_records.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_administrative_areas_insert_references
BEFORE INSERT ON administrative_areas
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM administrative_areas
            WHERE canonical_build_id = NEW.canonical_build_id
          AND area_id = NEW.area_id
        )
        THEN RAISE(ABORT, 'administrative_areas row already exists')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'administrative_areas.canonical_build_id missing')
    END;
    SELECT CASE
        WHEN NEW.parent_area_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM administrative_areas
                 WHERE canonical_build_id = NEW.canonical_build_id
                   AND area_id = NEW.parent_area_id
             )
        THEN RAISE(ABORT, 'administrative_areas.parent_area_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'administrative_areas.source_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
              AND source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'administrative_areas.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_administrative_areas_update_references
BEFORE UPDATE OF canonical_build_id, parent_area_id, source_id, artifact_id
ON administrative_areas
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'administrative_areas.canonical_build_id missing')
    END;
    SELECT CASE
        WHEN NEW.parent_area_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM administrative_areas
                 WHERE canonical_build_id = NEW.canonical_build_id
                   AND area_id = NEW.parent_area_id
             )
        THEN RAISE(ABORT, 'administrative_areas.parent_area_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'administrative_areas.source_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
              AND source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'administrative_areas.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_service_units_insert_references
BEFORE INSERT ON service_units
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM service_units
            WHERE canonical_build_id = NEW.canonical_build_id
          AND unit_id = NEW.unit_id
        )
        THEN RAISE(ABORT, 'service_units row already exists')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'service_units.canonical_build_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'service_units.source_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
              AND source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'service_units.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_service_units_update_references
BEFORE UPDATE OF canonical_build_id, source_id, artifact_id ON service_units
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'service_units.canonical_build_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'service_units.source_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
              AND source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'service_units.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_locality_context_insert_references
BEFORE INSERT ON locality_context
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM locality_context
            WHERE canonical_build_id = NEW.canonical_build_id
          AND locality = NEW.locality
        )
        THEN RAISE(ABORT, 'locality_context row already exists')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'locality_context.canonical_build_id missing')
    END;
    SELECT CASE
        WHEN NEW.delegation_area_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM administrative_areas
                 WHERE canonical_build_id = NEW.canonical_build_id
                   AND area_id = NEW.delegation_area_id
             )
        THEN RAISE(ABORT, 'locality_context.delegation_area_id missing')
    END;
    SELECT CASE
        WHEN NEW.service_unit_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM service_units
                 WHERE canonical_build_id = NEW.canonical_build_id
                   AND unit_id = NEW.service_unit_id
             )
        THEN RAISE(ABORT, 'locality_context.service_unit_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'locality_context.source_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
              AND source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'locality_context.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_locality_context_update_references
BEFORE UPDATE OF canonical_build_id, delegation_area_id, service_unit_id,
                 source_id, artifact_id
ON locality_context
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_builds
            WHERE canonical_build_id = NEW.canonical_build_id
        )
        THEN RAISE(ABORT, 'locality_context.canonical_build_id missing')
    END;
    SELECT CASE
        WHEN NEW.delegation_area_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM administrative_areas
                 WHERE canonical_build_id = NEW.canonical_build_id
                   AND area_id = NEW.delegation_area_id
             )
        THEN RAISE(ABORT, 'locality_context.delegation_area_id missing')
    END;
    SELECT CASE
        WHEN NEW.service_unit_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM service_units
                 WHERE canonical_build_id = NEW.canonical_build_id
                   AND unit_id = NEW.service_unit_id
             )
        THEN RAISE(ABORT, 'locality_context.service_unit_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM dataset_sources
            WHERE source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'locality_context.source_id missing')
    END;
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM source_artifacts
            WHERE artifact_id = NEW.artifact_id
              AND source_id = NEW.source_id
        )
        THEN RAISE(ABORT, 'locality_context.artifact_id missing')
    END;
END;

CREATE TRIGGER guard_canonical_state_insert_reference
BEFORE INSERT ON canonical_state
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM canonical_state
            WHERE state_id = NEW.state_id
        )
        THEN RAISE(ABORT, 'canonical_state row already exists')
    END;
    SELECT CASE
        WHEN NEW.active_build_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM canonical_builds
                 WHERE canonical_build_id = NEW.active_build_id
             )
        THEN RAISE(ABORT, 'canonical_state.active_build_id missing')
    END;
    SELECT CASE
        WHEN NEW.active_build_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM canonical_builds
                 WHERE canonical_build_id = NEW.active_build_id
                   AND status = 'completed'
             )
        THEN RAISE(
            ABORT, 'canonical_state.active_build_id is not a completed build'
        )
    END;
END;

CREATE TRIGGER guard_canonical_state_update_reference
BEFORE UPDATE OF active_build_id ON canonical_state
BEGIN
    SELECT CASE
        WHEN NEW.active_build_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM canonical_builds
                 WHERE canonical_build_id = NEW.active_build_id
             )
        THEN RAISE(ABORT, 'canonical_state.active_build_id missing')
    END;
    SELECT CASE
        WHEN NEW.active_build_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM canonical_builds
                 WHERE canonical_build_id = NEW.active_build_id
                   AND status = 'completed'
             )
        THEN RAISE(
            ABORT, 'canonical_state.active_build_id is not a completed build'
        )
    END;
END;

CREATE TRIGGER restrict_dataset_sources_delete
BEFORE DELETE ON dataset_sources
WHEN EXISTS (
         SELECT 1 FROM source_artifacts
         WHERE source_id = OLD.source_id
     )
     OR EXISTS (
         SELECT 1 FROM quarantine_records
         WHERE source_id = OLD.source_id
     )
     OR EXISTS (
         SELECT 1 FROM administrative_areas
         WHERE source_id = OLD.source_id
     )
     OR EXISTS (
         SELECT 1 FROM service_units
         WHERE source_id = OLD.source_id
     )
     OR EXISTS (
         SELECT 1 FROM locality_context
         WHERE source_id = OLD.source_id
     )
BEGIN
    SELECT RAISE(ABORT, 'dataset_sources.source_id is referenced');
END;

CREATE TRIGGER restrict_dataset_sources_primary_key_update
BEFORE UPDATE OF source_id ON dataset_sources
WHEN NEW.source_id IS NOT OLD.source_id
     AND (
         EXISTS (
             SELECT 1 FROM source_artifacts
             WHERE source_id = OLD.source_id
         )
         OR EXISTS (
             SELECT 1 FROM quarantine_records
             WHERE source_id = OLD.source_id
         )
         OR EXISTS (
             SELECT 1 FROM administrative_areas
             WHERE source_id = OLD.source_id
         )
         OR EXISTS (
             SELECT 1 FROM service_units
             WHERE source_id = OLD.source_id
         )
         OR EXISTS (
             SELECT 1 FROM locality_context
             WHERE source_id = OLD.source_id
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'dataset_sources.source_id is referenced');
END;

CREATE TRIGGER restrict_source_artifacts_delete
BEFORE DELETE ON source_artifacts
WHEN EXISTS (
         SELECT 1 FROM quarantine_records
         WHERE artifact_id = OLD.artifact_id
     )
     OR EXISTS (
         SELECT 1 FROM administrative_areas
         WHERE artifact_id = OLD.artifact_id
     )
     OR EXISTS (
         SELECT 1 FROM service_units
         WHERE artifact_id = OLD.artifact_id
     )
     OR EXISTS (
         SELECT 1 FROM locality_context
         WHERE artifact_id = OLD.artifact_id
     )
BEGIN
    SELECT RAISE(ABORT, 'source_artifacts.artifact_id is referenced');
END;

CREATE TRIGGER restrict_source_artifacts_primary_key_update
BEFORE UPDATE OF artifact_id ON source_artifacts
WHEN NEW.artifact_id IS NOT OLD.artifact_id
     AND (
         EXISTS (
             SELECT 1 FROM quarantine_records
             WHERE artifact_id = OLD.artifact_id
         )
         OR EXISTS (
             SELECT 1 FROM administrative_areas
             WHERE artifact_id = OLD.artifact_id
         )
         OR EXISTS (
             SELECT 1 FROM service_units
             WHERE artifact_id = OLD.artifact_id
         )
         OR EXISTS (
             SELECT 1 FROM locality_context
             WHERE artifact_id = OLD.artifact_id
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'source_artifacts.artifact_id is referenced');
END;

-- source_artifacts is append-only, absolutely: an artifact row may never be
-- updated or deleted, even when nothing references it.  The `restrict_`
-- triggers above already produce the more specific "is referenced" message,
-- so the WHEN clauses here deliberately exclude exactly the cases those
-- triggers (and guard_source_artifacts_update_references) handle.  That keeps
-- at most one trigger firing per statement, so the error message a caller
-- sees does not depend on SQLite's unspecified trigger ordering.

CREATE TRIGGER immutable_source_artifacts_update
BEFORE UPDATE ON source_artifacts
WHEN EXISTS (
         SELECT 1 FROM dataset_sources WHERE source_id = NEW.source_id
     )
     AND NOT (
         NEW.artifact_id IS NOT OLD.artifact_id
         AND (
             EXISTS (
                 SELECT 1 FROM quarantine_records
                 WHERE artifact_id = OLD.artifact_id
             )
             OR EXISTS (
                 SELECT 1 FROM administrative_areas
                 WHERE artifact_id = OLD.artifact_id
             )
             OR EXISTS (
                 SELECT 1 FROM service_units
                 WHERE artifact_id = OLD.artifact_id
             )
             OR EXISTS (
                 SELECT 1 FROM locality_context
                 WHERE artifact_id = OLD.artifact_id
             )
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'source_artifacts are immutable');
END;

CREATE TRIGGER immutable_source_artifacts_delete
BEFORE DELETE ON source_artifacts
WHEN NOT (
    EXISTS (
        SELECT 1 FROM quarantine_records
        WHERE artifact_id = OLD.artifact_id
    )
    OR EXISTS (
        SELECT 1 FROM administrative_areas
        WHERE artifact_id = OLD.artifact_id
    )
    OR EXISTS (
        SELECT 1 FROM service_units
        WHERE artifact_id = OLD.artifact_id
    )
    OR EXISTS (
        SELECT 1 FROM locality_context
        WHERE artifact_id = OLD.artifact_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'source_artifacts are immutable');
END;

CREATE TRIGGER restrict_administrative_areas_delete
BEFORE DELETE ON administrative_areas
WHEN EXISTS (
         SELECT 1 FROM administrative_areas
         WHERE canonical_build_id = OLD.canonical_build_id
           AND parent_area_id = OLD.area_id
     )
     OR EXISTS (
         SELECT 1 FROM locality_context
         WHERE canonical_build_id = OLD.canonical_build_id
           AND delegation_area_id = OLD.area_id
     )
BEGIN
    SELECT RAISE(ABORT, 'administrative_areas.area_id is referenced');
END;

CREATE TRIGGER restrict_administrative_areas_primary_key_update
BEFORE UPDATE OF area_id ON administrative_areas
WHEN NEW.area_id IS NOT OLD.area_id
     AND (
         EXISTS (
             SELECT 1 FROM administrative_areas
             WHERE canonical_build_id = OLD.canonical_build_id
               AND parent_area_id = OLD.area_id
         )
         OR EXISTS (
             SELECT 1 FROM locality_context
             WHERE canonical_build_id = OLD.canonical_build_id
               AND delegation_area_id = OLD.area_id
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'administrative_areas.area_id is referenced');
END;

CREATE TRIGGER restrict_service_units_delete
BEFORE DELETE ON service_units
WHEN EXISTS (
    SELECT 1 FROM locality_context
    WHERE canonical_build_id = OLD.canonical_build_id
      AND service_unit_id = OLD.unit_id
)
BEGIN
    SELECT RAISE(ABORT, 'service_units.unit_id is referenced');
END;

CREATE TRIGGER restrict_service_units_primary_key_update
BEFORE UPDATE OF unit_id ON service_units
WHEN NEW.unit_id IS NOT OLD.unit_id
     AND EXISTS (
         SELECT 1 FROM locality_context
         WHERE canonical_build_id = OLD.canonical_build_id
           AND service_unit_id = OLD.unit_id
     )
BEGIN
    SELECT RAISE(ABORT, 'service_units.unit_id is referenced');
END;

CREATE TRIGGER restrict_canonical_builds_delete
BEFORE DELETE ON canonical_builds
WHEN EXISTS (
         SELECT 1 FROM canonical_state
         WHERE active_build_id = OLD.canonical_build_id
     )
     OR EXISTS (
         SELECT 1 FROM administrative_areas
         WHERE canonical_build_id = OLD.canonical_build_id
     )
     OR EXISTS (
         SELECT 1 FROM service_units
         WHERE canonical_build_id = OLD.canonical_build_id
     )
     OR EXISTS (
         SELECT 1 FROM locality_context
         WHERE canonical_build_id = OLD.canonical_build_id
     )
BEGIN
    SELECT RAISE(ABORT, 'canonical_builds.canonical_build_id is referenced');
END;

CREATE TRIGGER restrict_canonical_builds_primary_key_update
BEFORE UPDATE OF canonical_build_id ON canonical_builds
WHEN NEW.canonical_build_id IS NOT OLD.canonical_build_id
     AND (
         EXISTS (
             SELECT 1 FROM canonical_state
             WHERE active_build_id = OLD.canonical_build_id
         )
         OR EXISTS (
             SELECT 1 FROM administrative_areas
             WHERE canonical_build_id = OLD.canonical_build_id
         )
         OR EXISTS (
             SELECT 1 FROM service_units
             WHERE canonical_build_id = OLD.canonical_build_id
         )
         OR EXISTS (
             SELECT 1 FROM locality_context
             WHERE canonical_build_id = OLD.canonical_build_id
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'canonical_builds.canonical_build_id is referenced');
END;

CREATE TRIGGER guard_canonical_builds_status_transition
BEFORE UPDATE OF status ON canonical_builds
WHEN NEW.status IS NOT OLD.status
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM canonical_state
            WHERE active_build_id = OLD.canonical_build_id
        )
        THEN RAISE(ABORT, 'canonical_builds.status is frozen while active')
    END;
    SELECT CASE
        WHEN OLD.status != 'building'
        THEN RAISE(ABORT, 'canonical_builds.status transition is not allowed')
    END;
END;

-- H1: canonical_state is a singleton and activation is a plain UPDATE of
-- active_build_id, which silently affects zero rows if the row is missing.
-- Seed it here so that never happens.
INSERT INTO canonical_state(state_id, active_build_id)
SELECT 1, NULL
WHERE NOT EXISTS (SELECT 1 FROM canonical_state WHERE state_id = 1);
