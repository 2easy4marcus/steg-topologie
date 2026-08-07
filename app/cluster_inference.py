# cluster_inference.py
"""
Statistical grid-cluster inference: builds a PPMI-weighted co-occurrence
graph from STEG notice data and runs Louvain community detection to find
probable shared-infrastructure groupings.

These are statistical groupings only -- never real physical infrastructure
identities or locations. See docs/superpowers/specs/2026-07-24-grid-cooccurrence-clusters-design.md.
"""

import math
from collections import defaultdict
from datetime import date, datetime, timezone
from uuid import uuid4

import networkx as nx
import community as community_louvain

from . import db
from . import geocoding
from . import model_readiness, observability
from .model.cluster_ids import match_cluster_ids
from .model.config import CONFIG
from .model.graph import build_graph_for_build
from .model.validation import validate_cluster_run

MIN_NOTICES = CONFIG.min_total_notices
MIN_LOCALITIES = CONFIG.min_localities
STABILITY_LOOKBACK_DAYS = 7
ALGORITHM_VERSION = "evidence-weighted-louvain-v2"


def build_ppmi_graph(cooccurrences: list, total_notices: int) -> nx.Graph:
    """Compatibility facade for the V1 unscoped PPMI graph.

    Deliberately NOT a wrapper around build_weighted_graph: legacy rows carry
    only locality_a/locality_b/notice_count, so routing them through V2 would
    mean inventing the confidence components V2 requires. V2 callers use
    model.graph.build_graph_for_build instead.

    cooccurrences: rows with locality_a, locality_b, notice_count.
    Edge weight = positive PMI (negatives clipped to 0, Louvain needs
    non-negative weights). P(a) is the true fraction of all notices that
    mention locality a (db.get_locality_notice_counts()), matching the
    approved spec formula exactly -- NOT approximated from summed edge
    weights, which would inflate the marginal for any hub locality that
    co-occurs with many different partners."""
    notice_counts = db.get_locality_notice_counts()

    G = nx.Graph()
    for row in cooccurrences:
        a, b, count = row["locality_a"], row["locality_b"], row["notice_count"]
        G.add_node(a)
        G.add_node(b)
        p_ab = count / total_notices
        p_a = notice_counts.get(a, 0) / total_notices
        p_b = notice_counts.get(b, 0) / total_notices
        if p_a <= 0 or p_b <= 0 or p_ab <= 0:
            continue
        pmi = math.log(p_ab / (p_a * p_b))
        ppmi = max(0.0, pmi)
        if ppmi > 0:
            G.add_edge(a, b, weight=ppmi)
    return G


def compute_clusters(G: nx.Graph) -> dict:
    """{locality: cluster_id}. Isolated nodes each become a singleton
    cluster with a unique id."""
    if G.number_of_nodes() == 0:
        return {}
    if G.number_of_edges() == 0:
        return {node: i for i, node in enumerate(G.nodes())}
    partition = community_louvain.best_partition(G, weight="weight", random_state=0)
    return partition


def compute_stability(run_date: str, cluster_assignment: dict) -> dict:
    """Jaccard similarity of each locality's cluster co-membership today
    vs. each of the last STABILITY_LOOKBACK_DAYS run_dates, averaged."""
    today_members = defaultdict(set)
    for locality, cid in cluster_assignment.items():
        today_members[cid].add(locality)

    past_dates = db.cluster_run_dates(before=run_date, limit=STABILITY_LOOKBACK_DAYS)

    stability = {}
    for locality, cid in cluster_assignment.items():
        today_set = today_members[cid] - {locality}
        if not past_dates:
            stability[locality] = 0.0
            continue
        scores = []
        for past_date in past_dates:
            past_cid = db.locality_cluster_on(past_date, locality)
            if past_cid is None:
                scores.append(0.0)
                continue
            past_members = db.cluster_members(past_date, past_cid) - {locality}
            union = today_set | past_members
            inter = today_set & past_members
            scores.append(len(inter) / len(union) if union else 0.0)
        stability[locality] = sum(scores) / len(scores)
    return stability


def _memberships(partition: dict) -> dict:
    """{cluster_id: {locality, ...}} from a {locality: cluster_id} partition."""
    memberships: dict = {}
    for locality, cluster_id in partition.items():
        memberships.setdefault(cluster_id, set()).add(locality)
    return memberships


def _lineage_rows(match) -> list:
    """Flatten the matcher's lineage into rows, under the final stable ids."""
    return [
        {
            "cluster_id": match.ids[new_id],
            "previous_cluster_id": row.previous_id,
            "jaccard_similarity": row.similarity,
            "role": row.role,
        }
        for new_id, rows in sorted(match.lineage.items())
        for row in rows
    ]


def run_recluster() -> dict:
    """Full daily job: check data floor, geocode pending localities, build
    the PPMI graph, cluster, score stability, persist. Returns a summary
    dict mirroring the /api/internal/recluster response."""
    run_date = date.today().isoformat()
    build_id = db.active_build_id()
    if not build_id:
        return {
            "status": "insufficient_data",
            "run_date": run_date,
            "readiness": model_readiness.evaluate().model_dump(),
        }
    existing = db.completed_cluster_run_for(
        run_date, build_id, ALGORITHM_VERSION
    )
    if existing:
        return {
            "status": "already_done",
            "run_date": run_date,
            "cluster_run_id": existing["run_id"],
            "build_id": build_id,
            "algorithm_version": ALGORITHM_VERSION,
        }

    readiness = model_readiness.evaluate(build_id=build_id)
    if not readiness.model_quality.ready:
        return {
            "status": "insufficient_data",
            "run_date": run_date,
            "readiness": readiness.model_dump(),
        }
    if not readiness.operational_health.ready:
        return {
            "status": "operationally_stale",
            "run_date": run_date,
            "readiness": readiness.model_dump(),
        }

    geocoding.geocode_all_pending()

    # Marginals and N come from this build's pinned snapshot, not from the
    # global notice/locality counters, so the graph cannot mix one build's
    # pairs with another build's totals.
    G = build_graph_for_build(build_id)

    raw_partition = compute_clusters(G)

    # Louvain's community integers are arbitrary per run, so a cluster's
    # identity is re-established by matching this run's memberships against
    # the previous active run's. Ids for unmatched clusters come from the
    # persistent allocator; len(current) is the safe upper bound and any
    # reservation left unused is burned rather than returned.
    previous_run_id = db.active_cluster_run_id()
    previous = (
        db.cluster_run_memberships(previous_run_id) if previous_run_id else {}
    )
    current = _memberships(raw_partition)
    match = match_cluster_ids(
        previous, current, next_id=db.reserve_cluster_ids(len(current))
    )
    partition = {
        locality: match.ids[raw_id] for locality, raw_id in raw_partition.items()
    }
    stability = compute_stability(run_date, partition)

    run_id = uuid4().hex
    db.create_cluster_run(
        run_id,
        build_id,
        ALGORITHM_VERSION,
        datetime.now(timezone.utc).isoformat(),
        config_version=CONFIG.version,
    )
    observability.record_job_event(
        run_id,
        "cluster_started",
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )
    db.write_cluster_members(run_id, partition, stability)
    db.complete_cluster_run(
        run_id,
        datetime.now(timezone.utc).isoformat(),
        len(set(partition.values())),
        len(partition),
    )

    lineage_rows = _lineage_rows(match)
    if previous_run_id:
        db.write_cluster_lineage(run_id, previous_run_id, lineage_rows)

    # Scored against the partition this run actually published, not against a
    # re-derivation of it, so the numbers an operator reads describe the model
    # that is about to be served.
    report = validate_cluster_run(
        build_id, algorithm_version=ALGORITHM_VERSION, partition=partition
    )
    db.record_validation_run(
        run_id,
        report,
        status="completed",
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        split_count=sum(1 for row in lineage_rows if row["role"] == "split"),
        merge_count=sum(1 for row in lineage_rows if row["role"] == "merged"),
    )
    observability.record_job_event(
        run_id,
        "cluster_validated",
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )

    # Activation reads this decision, so it has to be stored first. Readiness
    # was already checked above; a run that got this far has a completed
    # validation run for the active build.
    db.record_publication_decision(
        "cluster_run",
        run_id,
        build_id=build_id,
        decision="published",
        config_version=CONFIG.version,
        decided_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        db.activate_completed_cluster_run(run_id)
    except ValueError:
        # A refused activation must not be invisible: the completed run stays
        # in the table and the previous run stays active, so without a
        # breadcrumb the only symptom is a map that quietly stops advancing.
        observability.record_job_event(
            run_id,
            "cluster_activation_refused",
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        raise
    observability.record_job_event(
        run_id,
        "cluster_activated",
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )
    # Preserve legacy stability history while cluster consumers migrate. This
    # must carry the SAME remapped ids: /api/model-status derives its cluster
    # count from the legacy table, and remapping only one of the two writes
    # would make the public count disagree with the map.
    db.write_cluster_run(run_date, partition, stability)

    return {
        "status": "ok",
        "run_date": run_date,
        "cluster_run_id": run_id,
        "build_id": build_id,
        "algorithm_version": ALGORITHM_VERSION,
        "localities_clustered": len(partition),
        "cluster_count": len(set(partition.values())),
    }
