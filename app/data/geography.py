"""Load delegation-level administrative boundaries (GeoJSON only).

Only the GeoJSON delegations artifact is parsed here -- it is the file the
plan designates canonical (272 features, each with a globally-unique
``adm_id``). The sibling TopoJSON artifact (271 objects) is registered in
the manifest for provenance but deliberately not read by this loader; see
docs/data/README.md for the reconciliation note.

``LoadWarning`` is named to avoid colliding with the heavier, DB-backed
``app.data.models.QuarantinedRecord`` contract -- this one is just an
in-memory (record_key, reason_code) pair produced while parsing a file,
before anything is written to the registry.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from shapely import wkt as shapely_wkt
from shapely.geometry import shape


class Delegation(BaseModel):
    delegation_id: str  # normalized adm_id, e.g. "g34d51" -- never deleg_id
    name_ar: str
    name_fr: str | None = None
    governorate_name_ar: str | None = None
    governorate_name_fr: str | None = None
    geometry_wkt: str

    @property
    def geometry(self):
        return shapely_wkt.loads(self.geometry_wkt)


class LoadWarning(BaseModel):
    record_key: str
    reason_code: str


@dataclass
class LoadResult:
    accepted: list[Delegation]
    quarantined: list[LoadWarning]


def load_delegations(path: Path) -> LoadResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    accepted: list[Delegation] = []
    quarantined: list[LoadWarning] = []
    seen_ids: set[str] = set()

    for index, feature in enumerate(payload["features"]):
        props = feature.get("properties") or {}
        adm_id = props.get("adm_id")
        record_key = str(adm_id) if adm_id else f"index:{index}"

        name_ar = props.get("deleg_name")
        if not name_ar or not str(name_ar).strip():
            quarantined.append(
                LoadWarning(record_key=record_key, reason_code="missing_arabic_name")
            )
            continue

        if not adm_id or not str(adm_id).strip():
            quarantined.append(
                LoadWarning(record_key=record_key, reason_code="missing_adm_id")
            )
            continue
        adm_id = str(adm_id)
        if adm_id in seen_ids:
            quarantined.append(
                LoadWarning(record_key=adm_id, reason_code="duplicate_adm_id")
            )
            continue

        geom = shape(feature["geometry"])
        if geom.is_empty or not geom.is_valid:
            quarantined.append(
                LoadWarning(record_key=adm_id, reason_code="invalid_geometry")
            )
            continue

        seen_ids.add(adm_id)
        accepted.append(
            Delegation(
                delegation_id=adm_id,
                name_ar=str(name_ar),
                name_fr=props.get("deleg_na_1"),
                governorate_name_ar=props.get("gov_name_a"),
                governorate_name_fr=props.get("gov_name_f"),
                geometry_wkt=geom.wkt,
            )
        )
    return LoadResult(accepted=accepted, quarantined=quarantined)
