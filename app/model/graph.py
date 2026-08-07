"""Confidence-weighted co-occurrence graph (evidence model V2).

The weight is ordinary scoped PPMI, scaled by an explicit evidence-reliability
regularizer, then optionally scaled again by a bounded geographic agreement
bonus:

    weight = ppmi * reliability * (1 + geographic_bonus)

`reliability` is the product of the separately measured confidence components
(parse, scope, canonicalization, temporal recurrence). It is a regularizer on
an ordinary PPMI value -- the PPMI itself is never described or stored as a
fractional count, and every component stays available on the edge so a reader
can see which one pulled a weight down.

The geographic term never creates an edge. It only scales one that outage
evidence already justified, and only when geography was actually measured;
an unmeasured pair gets a bonus of zero rather than an assumed agreement.
"""

import math

import networkx as nx
from pydantic import BaseModel, Field

from .config import CONFIG


class InconsistentBuildError(ValueError):
    """A build's probability inputs violate `0 < pair_count <= marginal <= N`.

    This is a corrupt snapshot, not a filterable edge: a pair cannot have been
    seen in more notices than one of its own localities was. Raising keeps a
    broken build from silently producing a plausible-looking graph.
    """


class EdgeEvidence(BaseModel):
    locality_a: str
    locality_b: str
    notice_count: int = Field(gt=0)
    distinct_date_count: int = Field(ge=0)
    mean_parse_confidence: float = Field(ge=0, le=1)
    mean_scope_confidence: float = Field(ge=0, le=1)
    mean_canonicalization_confidence: float = Field(default=1.0, ge=0, le=1)
    # None means geography was never joined for this pair. Distinct from 0.0,
    # which would mean "measured, and the localities disagree".
    geographic_confidence: float | None = Field(default=None, ge=0, le=1)


def _marginal(locality_counts: dict, locality: str, total_notices: int) -> int:
    try:
        count = locality_counts[locality]
    except KeyError as exc:
        raise InconsistentBuildError(
            f"missing marginal for {locality!r}"
        ) from exc
    if not 0 < count <= total_notices:
        raise InconsistentBuildError(
            f"marginal for {locality!r} outside (0, {total_notices}]"
        )
    return count


def build_weighted_graph(
    edges: list,
    *,
    total_notices: int,
    locality_counts: dict,
    config=CONFIG,
) -> nx.Graph:
    """Pure math. See `build_graph_for_build` for the build-specific entry."""
    graph = nx.Graph()
    if total_notices <= 0:
        raise InconsistentBuildError("total_notices must be positive")

    for edge in edges:
        # Both localities become nodes even when the pair produces no edge, so
        # a locality gated out of every pair still clusters as a singleton
        # rather than vanishing from the model.
        for locality in (edge.locality_a, edge.locality_b):
            if locality not in graph:
                graph.add_node(locality, gated_pairs=0)

        count_a = _marginal(locality_counts, edge.locality_a, total_notices)
        count_b = _marginal(locality_counts, edge.locality_b, total_notices)
        if not 0 < edge.notice_count <= min(count_a, count_b):
            raise InconsistentBuildError(
                f"pair count {edge.notice_count} for "
                f"({edge.locality_a}, {edge.locality_b}) exceeds a marginal"
            )

        if edge.distinct_date_count < config.min_edge_distinct_dates:
            # One outage date is one event, not a repeated relationship. The
            # observation stays in build_pair_observations for diagnostics;
            # it just cannot carry a clustering edge.
            graph.nodes[edge.locality_a]["gated_pairs"] += 1
            graph.nodes[edge.locality_b]["gated_pairs"] += 1
            continue

        p_ab = edge.notice_count / total_notices
        p_a = count_a / total_notices
        p_b = count_b / total_notices
        ppmi = max(0.0, math.log(p_ab / (p_a * p_b)))
        if ppmi == 0:
            continue

        temporal = min(
            1.0, edge.distinct_date_count / config.recurrence_saturation_dates
        )
        reliability = (
            edge.mean_parse_confidence
            * edge.mean_scope_confidence
            * edge.mean_canonicalization_confidence
            * temporal
        )
        geographic_bonus = config.max_geographic_bonus * (
            edge.geographic_confidence or 0.0
        )

        graph.add_edge(
            edge.locality_a,
            edge.locality_b,
            weight=ppmi * reliability * (1 + geographic_bonus),
            ppmi=ppmi,
            reliability=reliability,
            parse_confidence=edge.mean_parse_confidence,
            scope_confidence=edge.mean_scope_confidence,
            canonicalization_confidence=edge.mean_canonicalization_confidence,
            temporal_support=temporal,
            distinct_date_count=edge.distinct_date_count,
            geographic_bonus=geographic_bonus,
        )
    return graph


def build_graph_for_build(build_id: str, *, config=CONFIG) -> nx.Graph:
    """Build the V2 graph from one evidence build's pinned snapshot.

    Marginals and N come from the build's own rows, never from the global
    locality_notice_counts table, so a graph cannot mix one build's pairs with
    another build's totals.
    """
    from .. import db

    edges = [EdgeEvidence(**row) for row in db.build_edge_evidence(build_id)]
    locality_counts = db.build_locality_counts(build_id)
    total_notices = db.model_build_counts(build_id)[0]

    graph = build_weighted_graph(
        edges,
        total_notices=total_notices,
        locality_counts=locality_counts,
        config=config,
    )
    # Localities with no pair at all still belong to the model.
    for locality in locality_counts:
        if locality not in graph:
            graph.add_node(locality, gated_pairs=0)

    graph.graph.update(
        build_id=build_id,
        config_version=config.version,
        total_notices=total_notices,
    )
    return graph
