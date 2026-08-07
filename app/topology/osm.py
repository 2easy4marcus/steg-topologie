"""Bounded OpenStreetMap power-topology extraction (private Sfax pilot).

Two layers, deliberately separated:

- ``read_osm_elements`` is the only osmium-dependent code. It streams a .pbf
  (or any format osmium reads) and yields plain dicts shaped like Overpass
  JSON. The 84MB Sfax extract is gitignored and absent in CI, so nothing
  below this line may be needed to test the topology logic.
- ``build_snapshot`` is pure: element dicts in, ``TopologySnapshot`` out. A
  JSON fixture drives it.

Three things the naive "keep power-tagged nodes only" reading gets wrong, and
which this module therefore does not do:

- **Topology-node coordinates are retained.** A power way's node references
  point mostly at untagged nodes. Dropping them leaves the references
  dangling, makes a way-based substation's centroid uncomputable, and hides
  the shared node that connects a substation polygon to a line.
- **Way node references are retained in order.** They are the join key that
  puts a way-based substation and a line in the same connected component, and
  order is the line's geometry.
- **Relations are handled, and unsupported ones are quarantined explicitly.**
  A power ``type=site`` or ``type=multipolygon`` relation groups assets that
  belong to one installation. Anything else power-tagged (a ``type=route``
  power line relation, or a relation whose member is another relation) is
  recorded with a reason code and counted, not silently skipped: a silent
  skip is indistinguishable from "there was nothing there".

An element that lies wholly outside the bounding box is dropped rather than
quarantined -- being out of area is the extract working, not a data defect.
"""

import networkx as nx
from pydantic import BaseModel, Field

# Sfax governorate plus the Kerkennah islands, generously rounded. This is the
# whole geographic scope of the pilot; nothing outside it is ever written.
SFAX_KERKENNAH_BBOX_VALUES = (34.20, 9.95, 35.25, 11.40)

# Nodes tagged with one of these are assets in their own right.
ASSET_NODE_POWER = frozenset({"substation", "transformer", "pole", "tower"})
# Ways tagged with one of these are conductors between assets.
LINE_POWER = frozenset({"line", "minor_line", "cable"})
# Ways tagged with one of these are an asset's footprint polygon.
AREA_ASSET_POWER = frozenset({"substation", "transformer"})
# Relation structures whose membership we know how to interpret.
SUPPORTED_RELATION_TYPES = frozenset({"site", "multipolygon"})

_MEMBER_TYPES = {"n": "node", "w": "way", "r": "relation"}


class BoundingBox(BaseModel):
    min_latitude: float
    min_longitude: float
    max_latitude: float
    max_longitude: float

    def contains(self, latitude: float, longitude: float) -> bool:
        return (
            self.min_latitude <= latitude <= self.max_latitude
            and self.min_longitude <= longitude <= self.max_longitude
        )


SFAX_KERKENNAH_BBOX = BoundingBox(
    min_latitude=SFAX_KERKENNAH_BBOX_VALUES[0],
    min_longitude=SFAX_KERKENNAH_BBOX_VALUES[1],
    max_latitude=SFAX_KERKENNAH_BBOX_VALUES[2],
    max_longitude=SFAX_KERKENNAH_BBOX_VALUES[3],
)


class TopologyNode(BaseModel):
    """A retained coordinate. Most carry no power tag of their own."""

    node_id: int
    latitude: float
    longitude: float


class GridAsset(BaseModel):
    asset_id: str
    asset_type: str
    latitude: float | None
    longitude: float | None
    voltage: str | None = None
    # Empty only for an asset whose geometry could not be resolved.
    node_refs: list[int] = Field(default_factory=list)
    source_snapshot_id: str


class GridEdge(BaseModel):
    edge_id: str
    power_type: str
    node_refs: list[int]
    voltage: str | None = None
    source_snapshot_id: str


class GridRelation(BaseModel):
    relation_id: str
    relation_type: str
    member_ids: list[str]


class QuarantinedRelation(BaseModel):
    relation_id: str
    reason_code: str


class TopologySnapshot(BaseModel):
    snapshot_id: str
    nodes: list[TopologyNode] = Field(default_factory=list)
    assets: list[GridAsset] = Field(default_factory=list)
    edges: list[GridEdge] = Field(default_factory=list)
    relations: list[GridRelation] = Field(default_factory=list)
    quarantined_relations: list[QuarantinedRelation] = Field(
        default_factory=list
    )


def _is_power_relation(tags: dict) -> bool:
    return (
        "power" in tags
        or tags.get("site") == "power"
        or tags.get("route") == "power"
    )


def _retained_nodes(elements, bbox):
    nodes = {}
    for element in elements:
        if element.get("type") != "node":
            continue
        latitude, longitude = element.get("lat"), element.get("lon")
        if latitude is None or longitude is None:
            continue
        if bbox is not None and not bbox.contains(latitude, longitude):
            continue
        node_id = int(element["id"])
        nodes[node_id] = TopologyNode(
            node_id=node_id, latitude=latitude, longitude=longitude
        )
    return nodes


def build_snapshot(elements, *, snapshot_id, bbox=SFAX_KERKENNAH_BBOX):
    """Turn a stream of OSM element dicts into one bounded topology snapshot.

    `bbox=None` disables bounding. An element is in the extract when at least
    one of its own nodes is inside the box; its full ordered node reference
    list is kept either way, because a truncated reference list would be a
    silently wrong geometry.
    """
    elements = list(elements)
    nodes = _retained_nodes(elements, bbox)
    assets: list[GridAsset] = []
    edges: list[GridEdge] = []

    for element in elements:
        kind = element.get("type")
        tags = element.get("tags") or {}
        power = tags.get("power")
        if kind == "node":
            node = nodes.get(int(element["id"]))
            if power in ASSET_NODE_POWER and node is not None:
                assets.append(
                    GridAsset(
                        asset_id=f"node/{element['id']}",
                        asset_type=power,
                        latitude=node.latitude,
                        longitude=node.longitude,
                        voltage=tags.get("voltage"),
                        node_refs=[node.node_id],
                        source_snapshot_id=snapshot_id,
                    )
                )
            continue
        if kind != "way":
            continue
        refs = [int(ref) for ref in element.get("nodes") or []]
        inside = [nodes[ref] for ref in refs if ref in nodes]
        if not inside:
            continue
        if power in LINE_POWER:
            edges.append(
                GridEdge(
                    edge_id=f"way/{element['id']}",
                    power_type=power,
                    node_refs=refs,
                    voltage=tags.get("voltage"),
                    source_snapshot_id=snapshot_id,
                )
            )
        elif power in AREA_ASSET_POWER:
            assets.append(
                GridAsset(
                    asset_id=f"way/{element['id']}",
                    asset_type=power,
                    latitude=sum(n.latitude for n in inside) / len(inside),
                    longitude=sum(n.longitude for n in inside) / len(inside),
                    voltage=tags.get("voltage"),
                    node_refs=refs,
                    source_snapshot_id=snapshot_id,
                )
            )

    known = {asset.asset_id for asset in assets} | {
        edge.edge_id for edge in edges
    }
    relations: list[GridRelation] = []
    quarantined: list[QuarantinedRelation] = []
    for element in elements:
        if element.get("type") != "relation":
            continue
        tags = element.get("tags") or {}
        if not _is_power_relation(tags):
            continue
        relation_id = f"relation/{element['id']}"
        members = element.get("members") or []
        if tags.get("type") not in SUPPORTED_RELATION_TYPES:
            quarantined.append(
                QuarantinedRelation(
                    relation_id=relation_id,
                    reason_code="unsupported_relation_type",
                )
            )
            continue
        if any(member.get("type") == "relation" for member in members):
            # Nested relations would need recursive resolution we have no
            # verified example of. Refuse rather than guess at the membership.
            quarantined.append(
                QuarantinedRelation(
                    relation_id=relation_id,
                    reason_code="nested_relation_member",
                )
            )
            continue
        member_ids = [
            f"{member['type']}/{member['ref']}"
            for member in members
            if f"{member['type']}/{member['ref']}" in known
        ]
        if not member_ids:
            continue
        relations.append(
            GridRelation(
                relation_id=relation_id,
                relation_type=tags["type"],
                member_ids=member_ids,
            )
        )

    return TopologySnapshot(
        snapshot_id=snapshot_id,
        nodes=sorted(nodes.values(), key=lambda node: node.node_id),
        assets=assets,
        edges=edges,
        relations=relations,
        quarantined_relations=quarantined,
    )


def connected_components(snapshot: TopologySnapshot) -> dict:
    """{asset_or_edge_id: component_index} for one snapshot.

    Elements are joined through the OSM nodes they share and through the
    supported relations they belong to. Components are numbered by their
    alphabetically smallest member, so the index is stable across runs.
    """
    graph = nx.Graph()
    elements = [(asset.asset_id, asset.node_refs) for asset in snapshot.assets]
    elements += [(edge.edge_id, edge.node_refs) for edge in snapshot.edges]
    element_ids = {element_id for element_id, _ in elements}
    for element_id, refs in elements:
        graph.add_node(element_id)
        for ref in refs:
            graph.add_edge(element_id, ("node", ref))
    for relation in snapshot.relations:
        for member_id in relation.member_ids:
            graph.add_edge(("relation", relation.relation_id), member_id)

    groups = sorted(
        sorted(member for member in component if member in element_ids)
        for component in nx.connected_components(graph)
    )
    return {
        element_id: index
        for index, group in enumerate(groups)
        for element_id in group
    }


def read_osm_elements(path):
    """Stream element dicts from an OSM file. The only osmium-dependent code.

    Only power-related elements are emitted, plus a synthetic node element for
    every coordinate a power way references -- that is how topology-node
    coordinates survive without holding the whole planet's nodes in memory.
    """
    import osmium

    seen_nodes = set()
    for obj in osmium.FileProcessor(str(path)).with_locations():
        tags = dict(obj.tags)
        kind = obj.type_str()
        if kind == "n":
            if tags.get("power") in ASSET_NODE_POWER and obj.location.valid():
                seen_nodes.add(obj.id)
                yield {
                    "type": "node",
                    "id": obj.id,
                    "lat": obj.location.lat,
                    "lon": obj.location.lon,
                    "tags": tags,
                }
        elif kind == "w":
            power = tags.get("power")
            if power not in LINE_POWER and power not in AREA_ASSET_POWER:
                continue
            for node in obj.nodes:
                if node.ref in seen_nodes or not node.location.valid():
                    continue
                seen_nodes.add(node.ref)
                yield {
                    "type": "node",
                    "id": node.ref,
                    "lat": node.location.lat,
                    "lon": node.location.lon,
                }
            yield {
                "type": "way",
                "id": obj.id,
                "nodes": [node.ref for node in obj.nodes],
                "tags": tags,
            }
        elif kind == "r" and _is_power_relation(tags):
            yield {
                "type": "relation",
                "id": obj.id,
                "members": [
                    {
                        "type": _MEMBER_TYPES.get(member.type, member.type),
                        "ref": member.ref,
                        "role": member.role,
                    }
                    for member in obj.members
                ],
                "tags": tags,
            }


def load_topology(path, *, snapshot_id, bbox=SFAX_KERKENNAH_BBOX):
    return build_snapshot(
        read_osm_elements(path), snapshot_id=snapshot_id, bbox=bbox
    )
