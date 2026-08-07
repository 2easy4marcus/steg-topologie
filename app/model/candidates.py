"""Private, experimental asset-candidate generation and ranking (Sfax pilot).

Nothing here is public. The output is a **ranking index**, not a probability:
the components are hand-weighted, none of them is calibrated against a known
outcome, and no public endpoint reads the tables it is stored in.

Order of operations, in the order the amendment requires: geography bounds the
candidate set first, then topology and service features score what is left.
An asset that no locality in the cluster is near is not a low-scoring
candidate, it is not a candidate.

Feature definitions, all in [0, 1] and all measured, never assumed:

- ``outage_fit`` -- share of the cluster's localities within ``radius_km`` of
  the asset. What the asset alone could explain.
- ``topology_consistency`` -- share of the cluster reached by every asset in
  the asset's connected component, i.e. by the whole feeder. Zero when the
  component contains no line at all: an isolated point has no topology, and
  reporting its proximity as topological agreement would invent a signal.
- ``service_prior`` -- purity of the STEG service units of the localities the
  asset covers. An asset straddling two service units explains a shared
  outage less well than one sitting inside a single unit's territory. Zero
  when no covered locality has a measured service unit.
- ``distance_score`` -- ``1 - mean distance / radius`` over the covered
  localities.
- ``temporal_support`` -- the cluster's measured independent outage dates,
  saturating at ``CONFIG.recurrence_saturation_dates``. Cluster-level, so it
  is the same for every candidate in one run; it scales the whole ranking's
  standing rather than reordering it.
- ``completeness`` -- share of the three inputs that were actually present:
  a voltage tag, a line in the component, and a service unit on every covered
  locality. Candidates below ``MIN_CANDIDATE_COMPLETENESS`` are dropped.

Why the weights live here and not in ``app/model/config.py``: ``CONFIG`` is
the *public* evidence model's versioned gate set, and its ``version`` is
stamped onto ``quality_gate_results`` and ``publication_decisions``. Bumping
``evidence-v2.1`` because a private pilot retuned a weight would invalidate
the comparability of unrelated stored public decisions. These constants
therefore identify themselves through their own ``SCORING_VERSION``, which is
stored on every candidate run alongside the build, cluster-run, topology
snapshot and ``CONFIG.version`` identities. Knobs that already exist in
``CONFIG`` (the two-distinct-dates gate, the recurrence saturation point) are
reused from there rather than duplicated.

Rationale for the v1 values:

- The six weights are the plan's, unchanged: outage fit dominates because it
  is the only term measured from outage evidence rather than from geography
  or tagging; completeness is the lightest because it is a data-quality
  penalty, not evidence of anything.
- ``CANDIDATE_RADIUS_KM`` 8.0 -- a medium-voltage distribution feeder's
  practical reach around a Sfax substation. It bounds the candidate set; it
  is not a claim about which asset failed.
- ``MIN_CANDIDATE_COMPLETENESS`` 0.5 -- at least two of the three inputs must
  have been observed before an asset is ranked at all.
- ``PERTURBATION_FACTORS`` 0.8 / 1.2 -- the plan's sensitivity band.
"""

import math
from typing import Literal

from pydantic import BaseModel, Field

from ..topology import osm
from .config import CONFIG

SCORING_VERSION = "sfax-candidate-ranking-v1"

CANDIDATE_RADIUS_KM = 8.0
MIN_CANDIDATE_COMPLETENESS = 0.5

WEIGHTS = {
    "outage_fit": 0.35,
    "topology_consistency": 0.25,
    "service_prior": 0.15,
    "distance_score": 0.10,
    "temporal_support": 0.10,
    "completeness": 0.05,
}

PERTURBATION_FACTORS = (0.8, 1.2)

_EARTH_RADIUS_KM = 6371.0088


class LocalityGeography(BaseModel):
    """One cluster locality's accepted geography for a pinned canonical build.

    ``spatial_confidence`` 0 means the locality's position was not accepted,
    so it cannot bound anything. There is no default: a caller with no
    measurement has none to supply.
    """

    locality: str
    latitude: float
    longitude: float
    service_unit_id: str | None
    spatial_confidence: float = Field(ge=0, le=1)


class CandidateFeatures(BaseModel):
    asset_id: str
    outage_fit: float = Field(ge=0, le=1)
    topology_consistency: float = Field(ge=0, le=1)
    service_prior: float = Field(ge=0, le=1)
    distance_score: float = Field(ge=0, le=1)
    temporal_support: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)


class RankedCandidate(BaseModel):
    asset_id: str
    rank: int = Field(ge=1)
    score: float
    score_kind: Literal["ranking_index"] = "ranking_index"
    components: dict[str, float]


class CandidateRunResult(BaseModel):
    status: Literal["experimental", "insufficient_evidence"]
    candidates: list[RankedCandidate]
    scoring_version: str = SCORING_VERSION


def _haversine_km(lat_a, lon_a, lat_b, lon_b):
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def generate_candidates(
    snapshot,
    *,
    localities,
    independent_dates,
    radius_km=CANDIDATE_RADIUS_KM,
    config=CONFIG,
) -> list:
    """Bound the candidate set by geography, then score what survives.

    Returns features only for assets that a locality with accepted geography
    is actually near, that carry at least one topology or service signal, and
    whose features are complete enough to compare.
    """
    accepted = [row for row in localities if row.spatial_confidence > 0]
    if not accepted:
        return []

    components = osm.connected_components(snapshot)
    component_has_edge = {
        components[edge.edge_id] for edge in snapshot.edges
    }
    positioned = [
        asset
        for asset in snapshot.assets
        if asset.latitude is not None and asset.longitude is not None
    ]
    distances = {
        asset.asset_id: {
            row.locality: _haversine_km(
                asset.latitude, asset.longitude, row.latitude, row.longitude
            )
            for row in accepted
        }
        for asset in positioned
    }
    covered = {
        asset_id: {
            locality
            for locality, distance in rows.items()
            if distance <= radius_km
        }
        for asset_id, rows in distances.items()
    }
    reach: dict = {}
    for asset in positioned:
        reach.setdefault(components[asset.asset_id], set()).update(
            covered[asset.asset_id]
        )

    by_name = {row.locality: row for row in accepted}
    temporal_support = min(
        1.0, independent_dates / config.recurrence_saturation_dates
    )

    rows = []
    for asset in positioned:
        names = covered[asset.asset_id]
        if not names:
            # Geography bounds the set: nothing near, not a candidate.
            continue
        component = components[asset.asset_id]
        connected = component in component_has_edge
        units = [
            by_name[name].service_unit_id
            for name in names
            if by_name[name].service_unit_id
        ]
        service_prior = (
            max(units.count(unit) for unit in set(units)) / len(names)
            if units
            else 0.0
        )
        topology_consistency = (
            len(reach[component]) / len(accepted) if connected else 0.0
        )
        if topology_consistency <= 0 and service_prior <= 0:
            continue
        mean_distance = sum(
            distances[asset.asset_id][name] for name in names
        ) / len(names)
        completeness = (
            (asset.voltage is not None)
            + connected
            + (len(units) == len(names))
        ) / 3
        if completeness < MIN_CANDIDATE_COMPLETENESS:
            continue
        rows.append(
            CandidateFeatures(
                asset_id=asset.asset_id,
                outage_fit=len(names) / len(accepted),
                topology_consistency=topology_consistency,
                service_prior=service_prior,
                distance_score=max(0.0, 1 - mean_distance / radius_km),
                temporal_support=temporal_support,
                completeness=completeness,
            )
        )
    return rows


def _rank(rows, weights):
    """Rank `rows` under `weights`, ties broken by asset ID.

    Ranks are positions in that total order, so tied candidates still get
    distinct, deterministic ranks -- `1, 2` rather than `1, 1`. Two runs over
    the same rows always produce the same assignment.
    """
    scored = sorted(
        (
            (
                sum(
                    getattr(row, key) * weight
                    for key, weight in weights.items()
                ),
                row,
            )
            for row in rows
        ),
        key=lambda pair: (-pair[0], pair[1].asset_id),
    )
    return [
        RankedCandidate(
            asset_id=row.asset_id,
            rank=position,
            score=score,
            components=row.model_dump(exclude={"asset_id"}),
        )
        for position, (score, row) in enumerate(scored, start=1)
    ]


def rank_candidates(
    rows,
    *,
    independent_dates,
    geography_accepted,
    source_registered,
    config=CONFIG,
) -> CandidateRunResult:
    """Score a bounded candidate set, or refuse to.

    Every gate input is a required keyword argument. A default here would let
    a caller with no measurement inherit a passing one, which is exactly the
    fabricated sufficiency the plan forbids.
    """
    if (
        not rows
        or not geography_accepted
        or not source_registered
        or independent_dates < config.min_edge_distinct_dates
    ):
        return CandidateRunResult(
            status="insufficient_evidence", candidates=[]
        )
    return CandidateRunResult(
        status="experimental", candidates=_rank(rows, WEIGHTS)
    )


def _perturbations(weights):
    """The deterministic perturbation set: baseline, then one weight at a
    time scaled by each factor and the whole vector renormalized to sum 1.

    Fixed and fully enumerated -- sorted weight names, factors in declaration
    order -- so a stored min/max rank means the same thing in every run.
    """
    yield dict(weights)
    for key in sorted(weights):
        for factor in PERTURBATION_FACTORS:
            perturbed = dict(weights)
            perturbed[key] = weights[key] * factor
            total = sum(perturbed.values())
            yield {name: value / total for name, value in perturbed.items()}


def weight_sensitivity(rows, *, weights=WEIGHTS) -> dict:
    """{asset_id: {"min_rank": int, "max_rank": int}} over the perturbations.

    That object is persisted verbatim in `asset_candidate_scores`.
    """
    observed: dict = {}
    for perturbed in _perturbations(weights):
        for candidate in _rank(rows, perturbed):
            observed.setdefault(candidate.asset_id, []).append(candidate.rank)
    return {
        asset_id: {"min_rank": min(ranks), "max_rank": max(ranks)}
        for asset_id, ranks in observed.items()
    }


def run_candidate_pilot(
    run_id,
    *,
    cluster_run_id,
    cluster_id,
    build_id,
    snapshot,
    source_registered,
    created_at,
    radius_km=CANDIDATE_RADIUS_KM,
    config=CONFIG,
) -> CandidateRunResult:
    """Generate, rank and privately persist one cluster's candidate run.

    `source_registered` has no default: it is the operator's statement that
    the topology snapshot came from a registered, checksum-verified artifact
    (see scripts/extract_sfax_topology.py). Everything else is measured from
    the pinned build -- geography through the build's canonical-build pin,
    independent dates from the build's own scoped observations.

    `radius_km` stays overridable because recalibrating it is the pilot's
    purpose, and the value used is stored on the run row: a scoring version
    that does not determine the output is not an identity.
    """
    from .. import db

    members = db.cluster_run_memberships(cluster_run_id).get(cluster_id, set())
    geography = db.build_locality_geography(build_id)
    localities = [
        LocalityGeography(
            locality=name,
            latitude=row["latitude"],
            longitude=row["longitude"],
            service_unit_id=row["service_unit_id"],
            spatial_confidence=row["spatial_confidence"],
        )
        for name, row in sorted(geography.items())
        if name in members
        and row["latitude"] is not None
        and row["longitude"] is not None
    ]
    independent_dates = db.cluster_independent_dates(build_id, members)
    rows = generate_candidates(
        snapshot,
        localities=localities,
        independent_dates=independent_dates,
        radius_km=radius_km,
        config=config,
    )
    result = rank_candidates(
        rows,
        independent_dates=independent_dates,
        geography_accepted=any(
            row.spatial_confidence > 0 for row in localities
        ),
        source_registered=source_registered,
        config=config,
    )
    db.record_candidate_run(
        run_id,
        cluster_run_id=cluster_run_id,
        build_id=build_id,
        source_snapshot_id=snapshot.snapshot_id,
        config_version=config.version,
        scoring_version=SCORING_VERSION,
        radius_km=radius_km,
        status=result.status,
        created_at=created_at,
        completed_at=created_at,
    )
    if result.status == "experimental":
        db.write_candidate_scores(
            run_id, cluster_id, result.candidates, weight_sensitivity(rows)
        )
    return result
