"""run_import's build-activation safety (scripts/import_canonical_data.py).

run_import is the plan's central safety guarantee: rows are staged under a new
build's canonical_build_id while it is 'building', the build is marked
'completed', and only then does one guarded UPDATE flip
canonical_state.active_build_id. If anything fails mid-staging, only the new
build is marked 'failed' and canonical_state is left untouched -- the
last-known-good active build survives.

These run the real importer against the migrated temp DB the autouse
isolated_db fixture builds (db.init_db() runs the real migration path,
creating the canonical tables), staging real rows from real fixture files.
"""

import shutil

import yaml

from app import db
from scripts.import_canonical_data import run_import

FIXTURES = "tests/fixtures/data"


def _write_manifest(root, delegations_name, steg_name):
    """Write a manifest referencing two files already sitting under `root`,
    with checksums/sizes computed from those files so verify_artifact passes.
    """
    from app.data import registry

    def _artifact(artifact_id, source_id, relative_path, media_type):
        path = root / relative_path
        return {
            "artifact_id": artifact_id,
            "source_id": source_id,
            "relative_path": relative_path,
            "checksum_sha256": registry.sha256_file(path),
            "byte_size": path.stat().st_size,
            "retrieved_at": "2026-07-30T00:00:00Z",
            "registered_at": "2026-07-30T00:00:00Z",
            "media_type": media_type,
            "schema_version": "1",
        }

    def _source(source_id, title):
        return {
            "source_id": source_id,
            "title": title,
            "owner": "Test",
            "publication_class": "private_research",
            "refresh_policy": "manual",
            "schema_version": "1",
            "acquisition_description": "Test fixture.",
        }

    manifest = {
        "sources": [
            _source("tunisia-delegations", "Delegations"),
            _source("steg-service-units", "STEG units"),
        ],
        "artifacts": [
            _artifact(
                "tunisia-delegations-geojson", "tunisia-delegations",
                delegations_name, "application/geo+json",
            ),
            _artifact(
                "steg-service-units-csv", "steg-service-units",
                steg_name, "text/csv",
            ),
        ],
    }
    manifest_path = root / "sources.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return manifest_path


def _seed_root(tmp_path, delegations_src):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(delegations_src, root / "delegations.geojson")
    shutil.copy(f"{FIXTURES}/steg_units.csv", root / "steg_units.csv")
    manifest = _write_manifest(root, "delegations.geojson", "steg_units.csv")
    return manifest, root


def test_successful_apply_activates_new_build(tmp_path):
    # A locality inside the Sfax delegation polygon so the spatial join really
    # runs (point-in-polygon -> confidence 1.0), not just the empty-loop path.
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO localities(name, lat, lng, governorate) "
            "VALUES ('Sfax Centre', 34.74, 10.77, 'Sfax')"
        )

    manifest, root = _seed_root(tmp_path, f"{FIXTURES}/delegations.geojson")
    build_id = run_import(manifest, root)

    with db.get_conn() as conn:
        active = conn.execute(
            "SELECT active_build_id FROM canonical_state WHERE state_id = 1"
        ).fetchone()["active_build_id"]
        status = conn.execute(
            "SELECT status FROM canonical_builds WHERE canonical_build_id = ?",
            [build_id],
        ).fetchone()["status"]
        areas = conn.execute(
            "SELECT COUNT(*) c FROM administrative_areas "
            "WHERE canonical_build_id = ?",
            [build_id],
        ).fetchone()["c"]
        units = conn.execute(
            "SELECT COUNT(*) c FROM service_units WHERE canonical_build_id = ?",
            [build_id],
        ).fetchone()["c"]
        ctx = conn.execute(
            "SELECT delegation_area_id, spatial_confidence FROM locality_context "
            "WHERE canonical_build_id = ? AND locality = 'Sfax Centre'",
            [build_id],
        ).fetchone()

    assert active == build_id
    assert status == "completed"
    assert areas == 2  # Sfax + Kebili delegations
    assert units == 2  # Kairouan (complete) + Kerkennah (incomplete)
    assert ctx["delegation_area_id"] is not None
    assert ctx["spatial_confidence"] == 1.0


def test_mid_staging_failure_preserves_last_known_good(tmp_path):
    # Establish a previous last-known-good active build.
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO canonical_builds(canonical_build_id, status, "
            "started_at, finished_at) VALUES "
            "('cb-previous', 'completed', '2026-07-30T00:00:00Z', "
            "'2026-07-30T01:00:00Z')"
        )
        conn.execute(
            "UPDATE canonical_state SET active_build_id = 'cb-previous' "
            "WHERE state_id = 1"
        )

    # A delegations file whose only feature is quarantined (missing Arabic
    # name) -> zero accepted delegations -> run_import stages zero areas and
    # raises the empty-build guard mid-staging, exactly the report's forced
    # failure.
    empty_delegations = tmp_path / "empty_delegations.geojson"
    empty_delegations.write_text(
        '{"type": "FeatureCollection", "features": [{"type": "Feature", '
        '"properties": {"deleg_name": "", "adm_id": "g1d1", '
        '"gov_name_f": "Sfax"}, "geometry": {"type": "Polygon", '
        '"coordinates": [[[0,0],[1,0],[1,1],[0,1],[0,0]]]}}]}',
        encoding="utf-8",
    )
    manifest, root = _seed_root(tmp_path, empty_delegations)

    import pytest

    with pytest.raises(ValueError, match="staged build is empty"):
        run_import(manifest, root)

    with db.get_conn() as conn:
        active = conn.execute(
            "SELECT active_build_id FROM canonical_state WHERE state_id = 1"
        ).fetchone()["active_build_id"]
        failed = conn.execute(
            "SELECT canonical_build_id, failure_reason FROM canonical_builds "
            "WHERE status = 'failed'"
        ).fetchall()

    # Last-known-good is untouched...
    assert active == "cb-previous"
    # ...and the new build landed as 'failed' with a populated reason.
    assert len(failed) == 1
    assert failed[0]["canonical_build_id"] != "cb-previous"
    assert failed[0]["failure_reason"]
