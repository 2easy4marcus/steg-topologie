"""Stage canonical geography into a new canonical_build, then activate it.

Loads the verified manifest, parses the canonical delegations GeoJSON and
the STEG service-units .xls, reconciles STEG's and the GeoJSON's differing
governorate spellings against app.governorates' 24 real names, spatially
joins every geocoded locality to a delegation polygon and/or nearest
same-governorate service unit, and persists all of it under one
canonical_build_id -- built while `status='building'`, validated, then
flipped to `completed` and activated as the singleton active build. Any
failure marks only the new build `failed`; the previously active build (if
any) is left untouched.

Dry-run by default, matching app/rebuild_evidence.py's convention.

Usage:
    python scripts/import_canonical_data.py            # dry run
    python scripts/import_canonical_data.py --apply     # writes to the DB
"""

import argparse
import hashlib
import math
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/import_canonical_data.py` from the repo
# root without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shapely.geometry import Point  # noqa: E402

from app import db  # noqa: E402
from app.data import geography, registry, steg_units  # noqa: E402
from app.governorates import GOVERNORATE_NAMES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "data" / "sources.yaml"

DELEGATIONS_SOURCE_ID = "tunisia-delegations"
STEG_SOURCE_ID = "steg-service-units"
TRANSFORMATION_VERSION = "1"


# ---------------------------------------------------------------------------
# Governorate-name reconciliation.
#
# delegations.geojson's gov_name_f and tnlistedistrictsteg.xls's Gouvernorat
# column each spell governorate names differently from
# app.governorates.GOVERNORATE_NAMES and from each other (e.g. "El Kef" vs.
# "Le Kef", "Gabes" vs. "Gabès", "Manubah" vs. "Manouba" -- the latter a
# source typo). Both are reconciled through the same accent-stripped,
# alias-corrected key so every raw spelling resolves to exactly one of the
# 24 real governorate names; anything that doesn't resolve is a hard error,
# not a silent drop.

_GOVERNORATE_ALIASES = {
    "el kef": "le kef",
    "sidi bou zid": "sidi bouzid",
    "manubah": "manouba",
}


def _governorate_key(raw: str) -> str:
    stripped = "".join(
        c
        for c in unicodedata.normalize("NFKD", raw.strip().lower())
        if not unicodedata.combining(c)
    )
    stripped = " ".join(stripped.split())
    return _GOVERNORATE_ALIASES.get(stripped, stripped)


_GOVERNORATE_BY_KEY = {_governorate_key(name): name for name in GOVERNORATE_NAMES}


def resolve_governorate(raw: str) -> str:
    key = _governorate_key(raw)
    resolved = _GOVERNORATE_BY_KEY.get(key)
    if resolved is None:
        raise ValueError(f"unresolved_governorate:{raw!r}")
    return resolved


# ---------------------------------------------------------------------------
# Stable IDs. Content-derived (not row-position-derived) so re-running the
# importer against the same source data produces the same IDs.
# NOTE: IDs must satisfy dataset_sources'/administrative_areas' etc.
# ^[a-z0-9][a-z0-9_-]*$ CHECK -- no colons.


def delegation_id(source_id: str, row_delegation_id: str) -> str:
    return f"{source_id}-delegation-{row_delegation_id}".lower()


def service_unit_id(source_id: str, row) -> str:
    key = "|".join([row.region, row.governorate, row.unit_type, row.name])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{source_id}-service-{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_build_id() -> str:
    return f"cb-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# DB writes. Every write goes through app.db.get_conn(), per the rest of the
# codebase's "no separate client, no interactive transactions" convention
# (see the NOTE at the top of app/db.py). Each INSERT is a single guarded
# statement; idempotent registration of sources/artifacts uses the same
# INSERT ... WHERE NOT EXISTS pattern app/db.py already uses elsewhere.


def _register_source(conn, source) -> None:
    conn.execute(
        """
        INSERT INTO dataset_sources(
            source_id, title, owner, source_url, geographic_coverage,
            temporal_coverage, license_id, publication_class, refresh_policy,
            schema_version, acquisition_description
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM dataset_sources WHERE source_id = ?
        )
        """,
        [
            source.source_id, source.title, source.owner, source.source_url,
            source.geographic_coverage, source.temporal_coverage,
            source.license_id, source.publication_class.value,
            source.refresh_policy, source.schema_version,
            source.acquisition_description, source.source_id,
        ],
    )


def _register_artifact(conn, artifact) -> None:
    conn.execute(
        """
        INSERT INTO source_artifacts(
            artifact_id, source_id, relative_path, checksum_sha256,
            byte_size, retrieved_at, registered_at, media_type,
            schema_version, license_id
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM source_artifacts WHERE artifact_id = ?
        )
        """,
        [
            artifact.artifact_id, artifact.source_id, artifact.relative_path,
            artifact.checksum_sha256, artifact.byte_size,
            artifact.retrieved_at.isoformat(), artifact.registered_at.isoformat(),
            artifact.media_type, artifact.schema_version, artifact.license_id,
            artifact.artifact_id,
        ],
    )


def _insert_area(conn, *, area_id, build_id, name_ar, name_fr, geometry_wkt,
                  source_id, artifact_id, source_record_key, created_at) -> None:
    conn.execute(
        """
        INSERT INTO administrative_areas(
            area_id, canonical_build_id, area_level, name_ar, name_fr,
            parent_area_id, geometry_wkt, source_id, artifact_id,
            source_record_key, transformation_version, created_at, confidence
        ) VALUES (?, ?, 'delegation', ?, ?, NULL, ?, ?, ?, ?, ?, ?, 1.0)
        """,
        [
            area_id, build_id, name_ar, name_fr, geometry_wkt, source_id,
            artifact_id, source_record_key, TRANSFORMATION_VERSION, created_at,
        ],
    )


def _insert_unit(conn, *, unit_id, build_id, row, source_id, artifact_id,
                  source_record_key, created_at) -> None:
    conn.execute(
        """
        INSERT INTO service_units(
            unit_id, canonical_build_id, unit_type, name, region,
            governorate, latitude, longitude, coordinate_complete,
            source_id, artifact_id, source_record_key,
            transformation_version, created_at, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0)
        """,
        [
            unit_id, build_id, row.unit_type, row.name, row.region,
            row.governorate, row.latitude, row.longitude,
            1 if row.coordinate_complete else 0, source_id, artifact_id,
            source_record_key, TRANSFORMATION_VERSION, created_at,
        ],
    )


def _insert_locality_context(conn, *, build_id, locality, delegation_area_id,
                               unit_id, spatial_confidence, source_id,
                               artifact_id, created_at) -> None:
    conn.execute(
        """
        INSERT INTO locality_context(
            canonical_build_id, locality, context_build_id,
            delegation_area_id, service_unit_id, spatial_confidence,
            source_id, artifact_id, transformation_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            build_id, locality, build_id, delegation_area_id, unit_id,
            spatial_confidence, source_id, artifact_id,
            TRANSFORMATION_VERSION, created_at,
        ],
    )


def _insert_quarantine(conn, *, quarantine_id, source_id, artifact_id,
                         record_key, reason_code, safe_detail,
                         quarantined_at) -> None:
    conn.execute(
        """
        INSERT INTO quarantine_records(
            quarantine_id, source_id, artifact_id, record_key, reason_code,
            safe_detail, quarantined_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            quarantine_id, source_id, artifact_id, record_key, reason_code,
            safe_detail, quarantined_at,
        ],
    )


def _create_build(conn, build_id, started_at) -> None:
    conn.execute(
        """
        INSERT INTO canonical_builds(canonical_build_id, status, started_at)
        VALUES (?, 'building', ?)
        """,
        [build_id, started_at],
    )


def _fail_build(build_id, reason: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE canonical_builds
            SET status = 'failed', finished_at = ?, failure_reason = ?
            WHERE canonical_build_id = ? AND status = 'building'
            """,
            [_now(), reason[:500], build_id],
        )


def _complete_and_activate_build(build_id) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE canonical_builds
            SET status = 'completed', finished_at = ?
            WHERE canonical_build_id = ? AND status = 'building'
            """,
            [_now(), build_id],
        )
        # Guarded by guard_canonical_state_update_reference: aborts unless
        # build_id refers to a build that is (now) 'completed'. A failed
        # build never reaches this call.
        conn.execute(
            "UPDATE canonical_state SET active_build_id = ? WHERE state_id = 1",
            [build_id],
        )


# ---------------------------------------------------------------------------
# Spatial join: geocoded locality -> covering delegation polygon and/or
# nearest same-governorate complete service unit.
#
# ponytail: Euclidean lat/lng distance, not haversine -- Tunisia is small
# enough (~10 degrees across) that the distortion doesn't change which unit
# is nearest in practice. Upgrade to a proper great-circle distance if this
# ever needs to compare units near opposite ends of the country.


def _nearest_unit(lat: float, lng: float, candidates: list) -> dict | None:
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda u: math.hypot(u["latitude"] - lat, u["longitude"] - lng),
    )


def _spatial_context(lat, lng, governorate, delegation_shapes, units_by_governorate):
    point = Point(lng, lat)
    covering = [
        area_id for area_id, polygon in delegation_shapes if polygon.covers(point)
    ]
    ambiguous = len(covering) > 1
    delegation_area = covering[0] if len(covering) == 1 else None

    nearest = None
    if governorate:
        key = _governorate_key(governorate)
        nearest = _nearest_unit(lat, lng, units_by_governorate.get(key, []))

    if delegation_area is not None:
        confidence = 1.0
    elif nearest is not None:
        confidence = 0.7
    else:
        confidence = 0.0
    return delegation_area, (nearest["unit_id"] if nearest else None), confidence, ambiguous


def run_import(manifest_path: Path, root: Path) -> str:
    manifest = registry.load_manifest(manifest_path)
    sources_by_id = {s.source_id: s for s in manifest.sources}
    delegations_source = sources_by_id[DELEGATIONS_SOURCE_ID]
    steg_source = sources_by_id[STEG_SOURCE_ID]

    delegations_artifact = next(
        a for a in manifest.artifacts
        if a.source_id == DELEGATIONS_SOURCE_ID
        and a.relative_path.endswith(".geojson")
    )
    steg_artifact = next(
        a for a in manifest.artifacts if a.source_id == STEG_SOURCE_ID
    )

    registry.verify_artifact(delegations_artifact, root)
    registry.verify_artifact(steg_artifact, root)

    delegations_result = geography.load_delegations(
        root / delegations_artifact.relative_path
    )
    steg_result = steg_units.load_service_units(root / steg_artifact.relative_path)

    # Fail loudly rather than silently dropping rows whose governorate
    # spelling doesn't resolve -- see the reconciliation note above.
    for row in steg_result.accepted:
        resolve_governorate(row.governorate)
    for delegation in delegations_result.accepted:
        if delegation.governorate_name_fr:
            resolve_governorate(delegation.governorate_name_fr)

    build_id = _new_build_id()
    started_at = _now()
    with db.get_conn() as conn:
        _create_build(conn, build_id, started_at)

    try:
        with db.get_conn() as conn:
            for source in manifest.sources:
                _register_source(conn, source)
            for artifact in manifest.artifacts:
                _register_artifact(conn, artifact)

            quarantine_n = 0

            def _quarantine(source_id, artifact_id, record_key, reason_code, detail):
                nonlocal quarantine_n
                quarantine_n += 1
                _insert_quarantine(
                    conn,
                    quarantine_id=f"{build_id}-q-{quarantine_n}",
                    source_id=source_id,
                    artifact_id=artifact_id,
                    record_key=record_key,
                    reason_code=reason_code,
                    safe_detail=detail,
                    quarantined_at=_now(),
                )

            for warning in delegations_result.quarantined:
                _quarantine(
                    DELEGATIONS_SOURCE_ID, delegations_artifact.artifact_id,
                    warning.record_key, warning.reason_code,
                    f"Delegation record {warning.record_key} rejected: "
                    f"{warning.reason_code}",
                )
            for warning in steg_result.quarantined:
                _quarantine(
                    STEG_SOURCE_ID, steg_artifact.artifact_id,
                    warning.record_key, warning.reason_code,
                    f"Service unit record {warning.record_key} rejected: "
                    f"{warning.reason_code}",
                )

            delegation_shapes = []
            for delegation in delegations_result.accepted:
                area_id = delegation_id(DELEGATIONS_SOURCE_ID, delegation.delegation_id)
                _insert_area(
                    conn,
                    area_id=area_id,
                    build_id=build_id,
                    name_ar=delegation.name_ar,
                    name_fr=delegation.name_fr,
                    geometry_wkt=delegation.geometry_wkt,
                    source_id=DELEGATIONS_SOURCE_ID,
                    artifact_id=delegations_artifact.artifact_id,
                    source_record_key=delegation.delegation_id,
                    created_at=_now(),
                )
                delegation_shapes.append((area_id, delegation.geometry))

            seen_unit_ids: set[str] = set()
            units_by_governorate: dict[str, list] = {}
            for row in steg_result.accepted:
                unit_id = service_unit_id(STEG_SOURCE_ID, row)
                if unit_id in seen_unit_ids:
                    _quarantine(
                        STEG_SOURCE_ID, steg_artifact.artifact_id, row.name,
                        "duplicate_source_key",
                        f"Service unit {row.name!r} hashes to an already-used "
                        "stable ID.",
                    )
                    continue
                seen_unit_ids.add(unit_id)
                _insert_unit(
                    conn,
                    unit_id=unit_id,
                    build_id=build_id,
                    row=row,
                    source_id=STEG_SOURCE_ID,
                    artifact_id=steg_artifact.artifact_id,
                    source_record_key=row.name,
                    created_at=_now(),
                )
                if row.coordinate_complete:
                    key = _governorate_key(row.governorate)
                    units_by_governorate.setdefault(key, []).append(
                        {
                            "unit_id": unit_id,
                            "latitude": row.latitude,
                            "longitude": row.longitude,
                        }
                    )

            localities = conn.execute(
                """
                SELECT name, lat, lng, governorate FROM localities
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                """
            ).fetchall()
            for locality in localities:
                delegation_area, unit_id, confidence, ambiguous = _spatial_context(
                    locality["lat"], locality["lng"], locality["governorate"],
                    delegation_shapes, units_by_governorate,
                )
                if ambiguous:
                    _quarantine(
                        DELEGATIONS_SOURCE_ID, delegations_artifact.artifact_id,
                        locality["name"], "ambiguous_spatial_join",
                        f"Locality {locality['name']!r} falls inside more than "
                        "one delegation polygon.",
                    )
                _insert_locality_context(
                    conn,
                    build_id=build_id,
                    locality=locality["name"],
                    delegation_area_id=delegation_area,
                    unit_id=unit_id,
                    spatial_confidence=confidence,
                    source_id=DELEGATIONS_SOURCE_ID,
                    artifact_id=delegations_artifact.artifact_id,
                    created_at=_now(),
                )

            area_count = conn.execute(
                "SELECT COUNT(*) c FROM administrative_areas "
                "WHERE canonical_build_id = ?",
                [build_id],
            ).fetchone()["c"]
            unit_count = conn.execute(
                "SELECT COUNT(*) c FROM service_units "
                "WHERE canonical_build_id = ?",
                [build_id],
            ).fetchone()["c"]
            if area_count == 0 or unit_count == 0:
                raise ValueError(
                    f"staged build is empty: areas={area_count} units={unit_count}"
                )
    except Exception as exc:
        _fail_build(build_id, str(exc))
        raise

    _complete_and_activate_build(build_id)
    return build_id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if not args.apply:
        manifest = registry.load_manifest(args.manifest)
        sources_by_id = {s.source_id: s for s in manifest.sources}
        delegations_artifact = next(
            a for a in manifest.artifacts
            if a.source_id == DELEGATIONS_SOURCE_ID
            and a.relative_path.endswith(".geojson")
        )
        steg_artifact = next(
            a for a in manifest.artifacts if a.source_id == STEG_SOURCE_ID
        )
        registry.verify_artifact(delegations_artifact, args.root)
        registry.verify_artifact(steg_artifact, args.root)
        delegations_result = geography.load_delegations(
            args.root / delegations_artifact.relative_path
        )
        steg_result = steg_units.load_service_units(
            args.root / steg_artifact.relative_path
        )
        print(
            "DRY-RUN: would stage "
            f"{len(delegations_result.accepted)} delegations "
            f"({len(delegations_result.quarantined)} quarantined) and "
            f"{len(steg_result.accepted)} service units "
            f"({len(steg_result.quarantined)} quarantined)"
        )
        return 0

    build_id = run_import(args.manifest, args.root)
    print(f"Completed canonical build {build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
