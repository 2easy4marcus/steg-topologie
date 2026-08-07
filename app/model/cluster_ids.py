"""Stable cluster identity across runs.

Louvain hands back arbitrary community integers, so a cluster's identity has
to be re-established every run by matching this run's memberships against the
previous run's. The match is maximum-weight one-to-one over Jaccard overlap:
each surviving cluster inherits at most one predecessor id, and the globally
optimal assignment decides which. A greedy pass over sorted ids can strand a
cluster whose only good partner was already claimed, so it is not used.

Lineage keeps every eligible predecessor relationship, not only the matched
one, because a split or merge is exactly the case where the single inherited
id loses information.
"""

from dataclasses import dataclass

import networkx as nx

# Jaccard is compared as an integer numerator over this denominator.
# networkx documents that float weights may return a slightly suboptimal
# matching through accumulated rounding, while integer weights are exact --
# and "globally optimal" is the property the matching is here to provide.
JACCARD_SCALE = 10**9


@dataclass(frozen=True)
class Lineage:
    previous_id: int
    similarity: float
    weight: int
    role: str


@dataclass(frozen=True)
class MatchResult:
    ids: dict
    lineage: dict
    next_id: int


def _role(new_id, old_id, inherited, eligible_by_new, eligible_by_old):
    if inherited.get(new_id) == old_id:
        return "inherited"
    if old_id not in eligible_by_new.get(new_id, ()):
        return "related"
    # Judged from this row's subject, the new cluster: it absorbed several
    # predecessors (merge) before we ask whether its predecessor also fed
    # other new clusters (split).
    if len(eligible_by_new[new_id]) > 1:
        return "merged"
    if len(eligible_by_old.get(old_id, ())) > 1:
        return "split"
    return "related"


def match_cluster_ids(previous, current, *, next_id, threshold=0.50):
    """Assign each current cluster a stable id.

    `next_id` must come from the persistent allocator (db.reserve_cluster_ids),
    never from MAX(cluster_id) over surviving rows -- retention deletes rows,
    and a maximum recomputed after a delete reissues ids that were already
    used. Reserve `len(current)` ids up front; unused reservations are burned,
    which is what "never reused" requires.
    """
    graph = nx.Graph()
    overlaps = {}
    eligible_by_new = {}
    eligible_by_old = {}

    # Sorted iteration keeps graph insertion order deterministic, which is
    # what makes an exact tie between two matchings resolve the same way on
    # every run. No random perturbation is needed.
    for new_id, new_members in sorted(current.items()):
        overlaps[new_id] = []
        for old_id, old_members in sorted(previous.items()):
            union = new_members | old_members
            if not union:
                continue
            intersection = len(new_members & old_members)
            if not intersection:
                continue
            weight = intersection * JACCARD_SCALE // len(union)
            overlaps[new_id].append((old_id, intersection / len(union), weight))
            if intersection / len(union) >= threshold:
                eligible_by_new.setdefault(new_id, []).append(old_id)
                eligible_by_old.setdefault(old_id, []).append(new_id)
                graph.add_edge(("old", old_id), ("new", new_id), weight=weight)

    inherited = {}
    for left, right in nx.max_weight_matching(
        graph, maxcardinality=False, weight="weight"
    ):
        old_node = left if left[0] == "old" else right
        new_node = right if right[0] == "new" else left
        inherited[new_node[1]] = old_node[1]

    lineage = {}
    for new_id, rows in overlaps.items():
        lineage[new_id] = sorted(
            (
                Lineage(
                    previous_id=old_id,
                    similarity=similarity,
                    weight=weight,
                    role=_role(
                        new_id,
                        old_id,
                        inherited,
                        eligible_by_new,
                        eligible_by_old,
                    ),
                )
                for old_id, similarity, weight in rows
            ),
            key=lambda row: (-row.similarity, row.previous_id),
        )

    ids = dict(inherited)
    for new_id in sorted(current):
        if new_id not in ids:
            ids[new_id] = next_id
            next_id += 1
    return MatchResult(ids, lineage, next_id)
