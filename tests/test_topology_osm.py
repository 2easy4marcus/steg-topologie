"""Bounded OSM power-topology extraction (app/topology/osm.py).

The 84MB Sfax .pbf is gitignored and absent in CI, so everything here drives
the pure snapshot builder from tests/fixtures/data/osm_assets.json. The one
osmium-dependent function is covered separately against a tiny .osm XML file
written at test time -- osmium reads XML with the same reader it uses for
.pbf, so the adapter is exercised without shipping a binary fixture.
"""

import hashlib
import json
from pathlib import Path

import pytest

from app.topology import osm
from scripts import extract_sfax_topology

FIXTURE = Path(__file__).parent / "fixtures" / "data" / "osm_assets.json"
SNAPSHOT_ID = "sfax-2026-07-30"


def _snapshot(**kwargs):
    elements = json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"]
    return osm.build_snapshot(elements, snapshot_id=SNAPSHOT_ID, **kwargs)


def _by_id(snapshot):
    return {asset.asset_id: asset for asset in snapshot.assets} | {
        edge.edge_id: edge for edge in snapshot.edges
    }


def test_line_preserves_ordered_node_refs_voltage_and_snapshot_id():
    snapshot = _snapshot()

    line = _by_id(snapshot)["way/10"]

    assert line.power_type == "line"
    assert line.node_refs == [1, 2, 3]
    assert line.voltage == "90000"
    assert line.source_snapshot_id == SNAPSHOT_ID


def test_topology_node_coordinates_are_retained_even_when_untagged():
    # Nodes 2-5 carry no power tag. Dropping them would leave way/10's node
    # refs pointing at nothing and make way/20's centroid uncomputable.
    snapshot = _snapshot()

    coordinates = {node.node_id: node for node in snapshot.nodes}

    assert {2, 3, 4, 5} <= set(coordinates)
    assert coordinates[3].latitude == 34.7420
    assert coordinates[3].longitude == 10.7620


def test_way_based_substation_gets_a_centroid_from_retained_nodes():
    snapshot = _snapshot()

    substation = _by_id(snapshot)["way/20"]

    assert substation.asset_type == "substation"
    assert substation.node_refs == [3, 4, 5]
    assert round(substation.latitude, 6) == 34.7430
    assert round(substation.longitude, 6) == 10.7630
    assert substation.source_snapshot_id == SNAPSHOT_ID


def test_way_based_substation_shares_a_component_with_the_line():
    # The whole point of retaining way node references: way/20 touches node 3,
    # so does way/10, so does node/1's substation. One feeder, one component.
    snapshot = _snapshot()

    components = osm.connected_components(snapshot)

    assert components["way/20"] == components["way/10"] == components["node/1"]
    assert components["node/7"] != components["way/10"]


def test_supported_relation_membership_is_retained():
    snapshot = _snapshot()

    relation = {r.relation_id: r for r in snapshot.relations}["relation/100"]

    assert relation.relation_type == "site"
    assert relation.member_ids == ["way/20", "node/1"]


def test_unsupported_relation_structures_are_quarantined_and_countable():
    snapshot = _snapshot()

    quarantined = {
        row.relation_id: row.reason_code
        for row in snapshot.quarantined_relations
    }

    assert quarantined == {
        "relation/200": "unsupported_relation_type",
        "relation/300": "nested_relation_member",
    }
    assert {r.relation_id for r in snapshot.relations}.isdisjoint(quarantined)


def test_extract_is_bounded_to_the_sfax_kerkennah_box():
    snapshot = _snapshot()

    identifiers = set(_by_id(snapshot))

    # node/900 (Tunis) and way/30, whose only node is node/900, are outside.
    assert "node/900" not in identifiers
    assert "way/30" not in identifiers
    assert 900 not in {node.node_id for node in snapshot.nodes}


def test_bbox_of_none_keeps_everything_for_a_wider_extract():
    snapshot = _snapshot(bbox=None)

    assert "node/900" in _by_id(snapshot)


def test_pbf_reader_emits_the_elements_the_pure_builder_expects(tmp_path):
    # osmium's XML reader is the same reader the .pbf path uses, so this
    # covers the adapter without a binary fixture.
    source = tmp_path / "tiny.osm"
    source.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osm version="0.6">\n'
        '  <node id="1" lat="34.74" lon="10.76" version="1">\n'
        '    <tag k="power" v="substation"/>\n'
        '  </node>\n'
        '  <node id="2" lat="34.741" lon="10.761" version="1"/>\n'
        '  <way id="10" version="1">\n'
        '    <nd ref="1"/>\n'
        '    <nd ref="2"/>\n'
        '    <tag k="power" v="line"/>\n'
        '    <tag k="voltage" v="30000"/>\n'
        '  </way>\n'
        '</osm>\n',
        encoding="utf-8",
    )

    snapshot = osm.load_topology(source, snapshot_id=SNAPSHOT_ID)

    line = {edge.edge_id: edge for edge in snapshot.edges}["way/10"]
    assert line.node_refs == [1, 2]
    assert line.voltage == "30000"
    assert {node.node_id for node in snapshot.nodes} == {1, 2}
    assert [asset.asset_id for asset in snapshot.assets] == ["node/1"]


def _manifest(tmp_path, checksum):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - source_id: osm-tunisia\n"
        "    title: OSM Tunisia extract\n"
        "    owner: OpenStreetMap contributors\n"
        "    publication_class: private_research\n"
        "    refresh_policy: manual\n"
        "    schema_version: '1'\n"
        "    acquisition_description: Downloaded from Geofabrik.\n"
        "artifacts:\n"
        "  - artifact_id: osm-tunisia-pbf\n"
        "    source_id: osm-tunisia\n"
        "    relative_path: tunisia.osm.pbf\n"
        f"    checksum_sha256: '{checksum}'\n"
        "    byte_size: 1\n"
        "    retrieved_at: '2026-07-30T00:00:00Z'\n"
        "    registered_at: '2026-07-30T00:00:00Z'\n"
        "    media_type: application/x-protobuf\n"
        "    schema_version: '1'\n",
        encoding="utf-8",
    )
    return path


def test_extraction_refuses_a_pbf_whose_checksum_is_not_registered(tmp_path):
    source = tmp_path / "tunisia.osm.pbf"
    source.write_bytes(b"not the registered bytes")

    with pytest.raises(SystemExit, match="unregistered_source"):
        extract_sfax_topology.registered_artifact_id(
            source, _manifest(tmp_path, "f" * 64)
        )


def test_extraction_accepts_the_registered_checksum(tmp_path):
    source = tmp_path / "tunisia.osm.pbf"
    source.write_bytes(b"the registered bytes")
    manifest = _manifest(
        tmp_path, hashlib.sha256(b"the registered bytes").hexdigest()
    )

    assert (
        extract_sfax_topology.registered_artifact_id(source, manifest)
        == "osm-tunisia-pbf"
    )
