from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app import db, migrations
from app.data.models import (
    DatasetSource,
    PublicationClass,
    QuarantinedRecord,
    SourceArtifact,
)


@pytest.fixture
def source_registry_db(isolated_db, monkeypatch, tmp_path):
    database_path = tmp_path / "source-registry.db"
    monkeypatch.setattr(db, "DB_URL", f"file:{database_path}")
    migrations.apply_all()


def _dataset_source(**overrides):
    values = {
        "source_id": "delegations-v1",
        "title": "Delegations",
        "owner": "INS",
        "publication_class": PublicationClass.PRIVATE_RESEARCH,
        "refresh_policy": "manual",
        "schema_version": "1",
        "acquisition_description": "Downloaded from the publisher.",
    }
    values.update(overrides)
    return DatasetSource(**values)


def _add_source(conn, source_id):
    conn.execute(
        """
        INSERT INTO dataset_sources(
            source_id, title, owner, publication_class, refresh_policy,
            schema_version, acquisition_description
        ) VALUES (?, 'Source', 'Owner', 'private_research', 'manual', '1',
                  'Test source')
        """,
        [source_id],
    )


def _add_artifact(conn, artifact_id, source_id, checksum="a" * 64):
    conn.execute(
        """
        INSERT INTO source_artifacts(
            artifact_id, source_id, relative_path, checksum_sha256,
            byte_size, retrieved_at, media_type, schema_version
        ) VALUES (?, ?, 'file.csv', ?, 1, '2026-07-30T00:00:00Z',
                  'text/csv', '1')
        """,
        [artifact_id, source_id, checksum],
    )


def _add_build(conn, build_id, status="building"):
    conn.execute(
        """
        INSERT INTO canonical_builds(
            canonical_build_id, status, started_at, finished_at
        ) VALUES (?, ?, '2026-07-30T00:00:00Z', ?)
        """,
        [build_id, status, None if status == "building"
         else "2026-07-30T01:00:00Z"],
    )


def _add_area(conn, area_id, build_id, source_id, artifact_id, parent=None):
    conn.execute(
        """
        INSERT INTO administrative_areas(
            area_id, canonical_build_id, area_level, name_ar, parent_area_id,
            geometry_wkt, source_id, artifact_id, source_record_key,
            transformation_version, created_at, confidence
        ) VALUES (?, ?, 'delegation', 'Name', ?, 'POINT (0 0)', ?, ?, 'row-1',
                  'v1', '2026-07-30T00:00:00Z', 1)
        """,
        [area_id, build_id, parent, source_id, artifact_id],
    )


def _add_unit(conn, unit_id, build_id, source_id, artifact_id):
    conn.execute(
        """
        INSERT INTO service_units(
            unit_id, canonical_build_id, unit_type, name, region, governorate,
            coordinate_complete, source_id, artifact_id, source_record_key,
            transformation_version, created_at, confidence
        ) VALUES (?, ?, 'district', 'Unit', 'South', 'Sfax', 0, ?, ?, 'row-1',
                  'v1', '2026-07-30T00:00:00Z', 1)
        """,
        [unit_id, build_id, source_id, artifact_id],
    )


def _add_locality(
    conn, locality, build_id, source_id, artifact_id, area=None, unit=None
):
    conn.execute(
        """
        INSERT INTO locality_context(
            canonical_build_id, locality, context_build_id,
            delegation_area_id, service_unit_id, spatial_confidence,
            source_id, artifact_id, transformation_version, created_at
        ) VALUES (?, ?, 'ctx-1', ?, ?, 1, ?, ?, 'v1',
                  '2026-07-30T00:00:00Z')
        """,
        [build_id, locality, area, unit, source_id, artifact_id],
    )


def test_private_source_may_have_unknown_license():
    source = _dataset_source()

    assert source.license_id is None
    assert source.publication_class == "private_research"
    assert not hasattr(source, "checksum_sha256")
    assert not hasattr(source, "relative_path")


def test_public_source_requires_license():
    with pytest.raises(ValidationError, match="public source requires license_id"):
        _dataset_source(publication_class="public")


@pytest.mark.parametrize("source_id", ["UPPERCASE", "-leading", "has space"])
def test_source_ids_are_strict(source_id):
    with pytest.raises(ValidationError):
        _dataset_source(source_id=source_id)


def test_artifact_validates_checksum_and_nonnegative_size():
    artifact = SourceArtifact(
        artifact_id="delegations-20260730",
        source_id="delegations-v1",
        relative_path="delegations/delegations.geojson",
        checksum_sha256="a" * 64,
        byte_size=0,
        retrieved_at="2026-07-30T00:00:00Z",
        media_type="application/geo+json",
        schema_version="1",
        license_id="CC-BY-4.0",
    )
    assert artifact.byte_size == 0
    assert artifact.checksum_sha256 == "a" * 64
    assert artifact.license_id == "CC-BY-4.0"

    with pytest.raises(ValidationError):
        SourceArtifact(
            **{
                **artifact.model_dump(),
                "checksum_sha256": "not-a-checksum",
            }
        )
    with pytest.raises(ValidationError):
        SourceArtifact(**{**artifact.model_dump(), "byte_size": -1})


def test_artifact_contract_is_frozen_and_forbids_extra_fields():
    artifact = SourceArtifact(
        artifact_id="delegations-20260730",
        source_id="delegations-v1",
        relative_path="delegations.geojson",
        checksum_sha256="a" * 64,
        byte_size=1,
        retrieved_at="2026-07-30T00:00:00Z",
        media_type="application/geo+json",
        schema_version="1",
    )

    with pytest.raises(ValidationError):
        artifact.byte_size = 2
    with pytest.raises(ValidationError):
        SourceArtifact(**artifact.model_dump(), unexpected="forbidden")


def test_contracts_require_aware_timestamps_and_normalize_license():
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceArtifact(
            artifact_id="delegations-20260730",
            source_id="delegations-v1",
            relative_path="delegations.geojson",
            checksum_sha256="a" * 64,
            byte_size=1,
            retrieved_at="2026-07-30T00:00:00",
            media_type="application/geo+json",
            schema_version="1",
        )
    with pytest.raises(ValidationError, match="public source requires license_id"):
        _dataset_source(publication_class="public", license_id="   ")
    with pytest.raises(ValidationError):
        _dataset_source(unexpected="forbidden")


def test_quarantined_record_matches_database_contract():
    record = QuarantinedRecord(
        quarantine_id="quarantine-1",
        source_id="delegations-v1",
        artifact_id="delegations-20260730",
        record_key="feature-17",
        reason_code="invalid_geometry",
        safe_detail="Geometry is self-intersecting.",
        quarantined_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    assert set(record.model_dump()) == {
        "quarantine_id",
        "source_id",
        "artifact_id",
        "record_key",
        "reason_code",
        "safe_detail",
        "quarantined_at",
    }


def test_expected_source_and_canonical_schema(source_registry_db):
    expected_tables = {
        "canonical_builds",
        "canonical_state",
        "dataset_sources",
        "source_artifacts",
        "quarantine_records",
        "administrative_areas",
        "service_units",
        "locality_context",
    }
    with db.get_conn() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        dataset_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(dataset_sources)"
            ).fetchall()
        }
        artifact_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(source_artifacts)"
            ).fetchall()
        }
        locality_pk = [
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(locality_context)"
            ).fetchall()
            if row["pk"]
        ]

    assert expected_tables <= tables
    assert {"checksum_sha256", "relative_path"}.isdisjoint(dataset_columns)
    assert {
        "relative_path",
        "checksum_sha256",
        "retrieved_at",
        "media_type",
        "license_id",
    } <= artifact_columns
    assert locality_pk == ["canonical_build_id", "locality"]


def test_database_checks_reject_invalid_registry_values(source_registry_db):
    with db.get_conn() as conn:
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO dataset_sources(
                    source_id, title, owner, publication_class,
                    refresh_policy, schema_version, acquisition_description
                ) VALUES ('public-no-license', 'Title', 'Owner', 'public',
                          'manual', '1', 'Description')
                """
            )
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO source_artifacts(
                    artifact_id, source_id, relative_path, checksum_sha256,
                    byte_size, retrieved_at, media_type, schema_version
                ) VALUES ('bad-size', 'missing-source', 'file.csv', ?, -1,
                          '2026-07-30T00:00:00Z', 'text/csv', '1')
                """,
                ["a" * 64],
            )
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO service_units(
                    unit_id, unit_type, name, region, governorate,
                    coordinate_complete, source_id, artifact_id,
                    source_record_key, transformation_version, created_at,
                    confidence
                ) VALUES ('unit-1', 'district', 'Unit', 'South', 'Sfax', 2,
                          'source', 'artifact', 'row-1', 'v1',
                          '2026-07-30T00:00:00Z', 1.2)
                """
            )


def test_reference_guard_triggers_are_installed(source_registry_db):
    with db.get_conn() as conn:
        triggers = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    assert {
        "guard_source_artifacts_insert_references",
        "guard_source_artifacts_update_references",
        "guard_quarantine_records_insert_references",
        "guard_quarantine_records_update_references",
        "guard_administrative_areas_insert_references",
        "guard_administrative_areas_update_references",
        "guard_service_units_insert_references",
        "guard_service_units_update_references",
        "guard_locality_context_insert_references",
        "guard_locality_context_update_references",
        "guard_canonical_state_insert_reference",
        "guard_canonical_state_update_reference",
        "restrict_dataset_sources_delete",
        "restrict_dataset_sources_primary_key_update",
        "immutable_source_artifacts_delete",
        "immutable_source_artifacts_update",
        "restrict_administrative_areas_delete",
        "restrict_administrative_areas_primary_key_update",
        "restrict_service_units_delete",
        "restrict_service_units_primary_key_update",
        "restrict_canonical_builds_delete",
        "restrict_canonical_builds_primary_key_update",
    } <= triggers


def test_trigger_rejects_artifact_insert_for_missing_source(
    source_registry_db,
):
    with db.get_conn() as conn:
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()
        assert enabled["foreign_keys"] == 0
        with pytest.raises(
            Exception, match="source_artifacts.source_id missing"
        ):
            conn.execute(
                """
                INSERT INTO source_artifacts(
                    artifact_id, source_id, relative_path, checksum_sha256,
                    byte_size, retrieved_at, media_type, schema_version
                ) VALUES ('orphan', 'missing-source', 'file.csv', ?, 1,
                          '2026-07-30T00:00:00Z', 'text/csv', '1')
                """,
                ["a" * 64],
            )


def test_trigger_rejects_artifact_update_to_missing_source(
    source_registry_db,
):
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dataset_sources(
                source_id, title, owner, publication_class, refresh_policy,
                schema_version, acquisition_description
            ) VALUES (
                'source-one', 'Source', 'Owner', 'private_research', 'manual',
                '1', 'Test source'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO source_artifacts(
                artifact_id, source_id, relative_path, checksum_sha256,
                byte_size, retrieved_at, media_type, schema_version
            ) VALUES (
                'artifact-one', 'source-one', 'file.csv', ?, 1,
                '2026-07-30T00:00:00Z', 'text/csv', '1'
            )
            """,
            ["a" * 64],
        )

        with pytest.raises(
            Exception, match="source_artifacts.source_id missing"
        ):
            conn.execute(
                """
                UPDATE source_artifacts
                SET source_id = 'missing-source'
                WHERE artifact_id = 'artifact-one'
                """
            )


def test_source_artifacts_are_append_only_even_when_unreferenced(
    source_registry_db,
):
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dataset_sources(
                source_id, title, owner, publication_class, refresh_policy,
                schema_version, acquisition_description
            ) VALUES (
                'source-one', 'Source', 'Owner', 'private_research', 'manual',
                '1', 'Test source'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO source_artifacts(
                artifact_id, source_id, relative_path, checksum_sha256,
                byte_size, retrieved_at, media_type, schema_version
            ) VALUES (
                'artifact-one', 'source-one', 'file.csv', ?, 1,
                '2026-07-30T00:00:00Z', 'text/csv', '1'
            )
            """,
            ["a" * 64],
        )

        with pytest.raises(Exception, match="source_artifacts are immutable"):
            conn.execute(
                """
                UPDATE source_artifacts SET byte_size = 2
                WHERE artifact_id = 'artifact-one'
                """
            )
        with pytest.raises(Exception, match="source_artifacts are immutable"):
            conn.execute(
                "DELETE FROM source_artifacts WHERE artifact_id = 'artifact-one'"
            )


def test_restrict_triggers_reject_referenced_parent_delete_and_pk_update(
    source_registry_db,
):
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 0
        conn.execute(
            """
            INSERT INTO dataset_sources(
                source_id, title, owner, publication_class, refresh_policy,
                schema_version, acquisition_description
            ) VALUES (
                'source-one', 'Source', 'Owner', 'private_research', 'manual',
                '1', 'Test source'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO source_artifacts(
                artifact_id, source_id, relative_path, checksum_sha256,
                byte_size, retrieved_at, media_type, schema_version
            ) VALUES (
                'artifact-one', 'source-one', 'file.csv', ?, 1,
                '2026-07-30T00:00:00Z', 'text/csv', '1'
            )
            """,
            ["a" * 64],
        )
        conn.execute(
            """
            INSERT INTO quarantine_records(
                quarantine_id, source_id, artifact_id, record_key,
                reason_code, safe_detail, quarantined_at
            ) VALUES (
                'quarantine-one', 'source-one', 'artifact-one', 'row-1',
                'invalid', 'Invalid test row', '2026-07-30T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO administrative_areas(
                area_id, area_level, name_ar, geometry_wkt, source_id,
                artifact_id, source_record_key, transformation_version,
                created_at, confidence
            ) VALUES (
                'area-parent', 'governorate', 'Parent', 'POINT (0 0)',
                'source-one', 'artifact-one', 'parent', 'v1',
                '2026-07-30T00:00:00Z', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO administrative_areas(
                area_id, area_level, name_ar, parent_area_id, geometry_wkt,
                source_id, artifact_id, source_record_key,
                transformation_version, created_at, confidence
            ) VALUES (
                'area-child', 'delegation', 'Child', 'area-parent',
                'POINT (0 0)', 'source-one', 'artifact-one', 'child', 'v1',
                '2026-07-30T00:00:00Z', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO service_units(
                unit_id, unit_type, name, region, governorate,
                coordinate_complete, source_id, artifact_id,
                source_record_key, transformation_version, created_at,
                confidence
            ) VALUES (
                'unit-one', 'district', 'Unit', 'South', 'Sfax', 0,
                'source-one', 'artifact-one', 'unit', 'v1',
                '2026-07-30T00:00:00Z', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO locality_context(
                locality, context_build_id, delegation_area_id,
                service_unit_id, spatial_confidence, source_id, artifact_id,
                transformation_version, created_at
            ) VALUES (
                'Locality', 'build-one', 'area-parent', 'unit-one', 1,
                'source-one', 'artifact-one', 'v1',
                '2026-07-30T00:00:00Z'
            )
            """
        )

        guarded_operations = [
            (
                "DELETE FROM dataset_sources WHERE source_id = 'source-one'",
                "dataset_sources.source_id is referenced",
            ),
            (
                """
                UPDATE dataset_sources SET source_id = 'source-renamed'
                WHERE source_id = 'source-one'
                """,
                "dataset_sources.source_id is referenced",
            ),
            (
                "DELETE FROM source_artifacts WHERE artifact_id = 'artifact-one'",
                "source_artifacts.artifact_id is referenced",
            ),
            (
                """
                UPDATE source_artifacts SET artifact_id = 'artifact-renamed'
                WHERE artifact_id = 'artifact-one'
                """,
                "source_artifacts.artifact_id is referenced",
            ),
            (
                "DELETE FROM administrative_areas WHERE area_id = 'area-parent'",
                "administrative_areas.area_id is referenced",
            ),
            (
                """
                UPDATE administrative_areas SET area_id = 'area-renamed'
                WHERE area_id = 'area-parent'
                """,
                "administrative_areas.area_id is referenced",
            ),
            (
                "DELETE FROM service_units WHERE unit_id = 'unit-one'",
                "service_units.unit_id is referenced",
            ),
            (
                """
                UPDATE service_units SET unit_id = 'unit-renamed'
                WHERE unit_id = 'unit-one'
                """,
                "service_units.unit_id is referenced",
            ),
        ]
        for sql, message in guarded_operations:
            with pytest.raises(Exception, match=message.replace(".", r"\.")):
                conn.execute(sql)


def test_restrict_trigger_allows_unreferenced_parent_delete(
    source_registry_db,
):
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 0
        conn.execute(
            """
            INSERT INTO dataset_sources(
                source_id, title, owner, publication_class, refresh_policy,
                schema_version, acquisition_description
            ) VALUES (
                'unused-source', 'Unused', 'Owner', 'private_research',
                'manual', '1', 'Unreferenced test source'
            )
            """
        )

        conn.execute(
            "DELETE FROM dataset_sources WHERE source_id = 'unused-source'"
        )

        assert conn.execute(
            """
            SELECT source_id FROM dataset_sources
            WHERE source_id = 'unused-source'
            """
        ).fetchone() is None
