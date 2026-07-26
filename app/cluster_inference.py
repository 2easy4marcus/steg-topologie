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
from . import model_readiness

MIN_NOTICES = 30
MIN_LOCALITIES = 10
STABILITY_LOOKBACK_DAYS = 7
ALGORITHM_VERSION = "ppmi-louvain-v1"


def build_ppmi_graph(cooccurrences: list, total_notices: int) -> nx.Graph:
    """cooccurrences: rows with locality_a, locality_b, notice_count.
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

    notices_so_far = db.total_notice_count()
    cooccurrences = db.build_cooccurrences(build_id)
    G = build_ppmi_graph(cooccurrences, total_notices=notices_so_far)
    for name in db.build_locality_counts(build_id):
        G.add_node(name)

    partition = compute_clusters(G)
    stability = compute_stability(run_date, partition)
    run_id = uuid4().hex
    db.create_cluster_run(
        run_id,
        build_id,
        ALGORITHM_VERSION,
        datetime.now(timezone.utc).isoformat(),
    )
    db.write_cluster_members(run_id, partition, stability)
    db.complete_cluster_run(
        run_id,
        datetime.now(timezone.utc).isoformat(),
        len(set(partition.values())),
        len(partition),
    )
    db.activate_completed_cluster_run(run_id)
    # Preserve legacy stability history while cluster consumers migrate.
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
