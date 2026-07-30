from pathlib import Path

import pytest

from app.data.geography import load_delegations


def test_geojson_loads_named_valid_tunisian_features():
    result = load_delegations(Path("tests/fixtures/data/delegations.geojson"))
    assert result.accepted[0].name_ar == "صفاقس المدينة"
    assert result.accepted[0].delegation_id == "g34d51"
    assert result.accepted[0].geometry.is_valid
    assert result.quarantined == []


def test_delegation_identity_uses_adm_id_not_deleg_id():
    # deleg_id (51.0) is governorate-local; adm_id (g34d51) is the normalized,
    # globally-unique identity the plan requires callers to use instead.
    result = load_delegations(Path("tests/fixtures/data/delegations.geojson"))
    ids = {d.delegation_id for d in result.accepted}
    assert ids == {"g34d51", "g63d54"}


def test_missing_arabic_name_is_quarantined(tmp_path):
    path = tmp_path / "delegations.geojson"
    path.write_text(
        """
        {"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "properties": {"deleg_name": "", "adm_id": "g1d1",
                             "gov_name_f": "Tunis"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0,0],[1,0],[1,1],[0,1],[0,0]]]}}
        ]}
        """,
        encoding="utf-8",
    )
    result = load_delegations(path)
    assert result.accepted == []
    assert [q.reason_code for q in result.quarantined] == ["missing_arabic_name"]


def test_duplicate_adm_id_is_quarantined(tmp_path):
    feature = {
        "type": "Feature",
        "properties": {"deleg_name": "X", "adm_id": "g1d1", "gov_name_f": "Tunis"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        },
    }
    path = tmp_path / "delegations.geojson"
    import json

    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature, feature]}),
        encoding="utf-8",
    )
    result = load_delegations(path)
    assert len(result.accepted) == 1
    assert [q.reason_code for q in result.quarantined] == ["duplicate_adm_id"]


def test_invalid_geometry_is_quarantined(tmp_path):
    # A self-intersecting "bowtie" polygon is invalid per the OGC simple
    # features model that shapely.geometry.shape().is_valid checks.
    feature = {
        "type": "Feature",
        "properties": {"deleg_name": "Y", "adm_id": "g2d2", "gov_name_f": "Tunis"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
        },
    }
    path = tmp_path / "delegations.geojson"
    import json

    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature]}),
        encoding="utf-8",
    )
    result = load_delegations(path)
    assert result.accepted == []
    assert [q.reason_code for q in result.quarantined] == ["invalid_geometry"]
