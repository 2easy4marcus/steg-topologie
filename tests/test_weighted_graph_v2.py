import math

import pytest

from app import db, evidence_pipeline
from app.model.config import CONFIG
from app.model.graph import (
    EdgeEvidence,
    InconsistentBuildError,
    build_graph_for_build,
    build_weighted_graph,
)
from tests.test_model_builds import _active_notice

SFAX = "جهة صفاقس"
KERKENNAH = "جهة قرقنة"


def _edge(**overrides):
    values = {
        "locality_a": "A",
        "locality_b": "B",
        "notice_count": 3,
        "distinct_date_count": 3,
        "mean_parse_confidence": 1.0,
        "mean_scope_confidence": 1.0,
        "mean_canonicalization_confidence": 1.0,
        "geographic_confidence": None,
    }
    values.update(overrides)
    return EdgeEvidence(**values)


def _graph(edges, **overrides):
    values = {"total_notices": 10, "locality_counts": {"A": 4, "B": 5}}
    values.update(overrides)
    return build_weighted_graph(edges, config=CONFIG, **values)


def test_ppmi_matches_hand_calculation():
    graph = _graph([_edge()])

    # ln((3/10) / ((4/10)*(5/10))) = ln(1.5)
    assert graph["A"]["B"]["ppmi"] == pytest.approx(math.log(1.5), rel=1e-9)


def test_weight_is_ppmi_times_reliability_with_no_geographic_bonus():
    # Every measured component is 1.0 and geography is unmeasured, so the
    # weight must be exactly the PPMI -- no invented bonus.
    graph = _graph([_edge()])
    data = graph["A"]["B"]

    assert data["weight"] == pytest.approx(data["ppmi"], rel=1e-9)
    assert data["geographic_bonus"] == 0.0


def test_reliability_components_are_recorded_separately_not_blended():
    graph = _graph(
        [
            _edge(
                mean_parse_confidence=0.7,
                mean_scope_confidence=0.35,
                mean_canonicalization_confidence=0.9,
            )
        ]
    )
    data = graph["A"]["B"]

    assert data["parse_confidence"] == 0.7
    assert data["scope_confidence"] == 0.35
    assert data["canonicalization_confidence"] == 0.9
    assert data["reliability"] == pytest.approx(0.7 * 0.35 * 0.9 * 1.0)
    assert data["weight"] == pytest.approx(data["ppmi"] * data["reliability"])


def test_geographic_agreement_only_scales_an_existing_edge():
    plain = _graph([_edge()])["A"]["B"]
    boosted = _graph([_edge(geographic_confidence=1.0)])["A"]["B"]

    assert boosted["geographic_bonus"] == CONFIG.max_geographic_bonus
    assert boosted["weight"] == pytest.approx(
        plain["weight"] * (1 + CONFIG.max_geographic_bonus)
    )
    assert boosted["ppmi"] == plain["ppmi"]


def test_single_date_pair_is_not_an_edge_but_stays_a_node():
    graph = _graph([_edge(notice_count=3, distinct_date_count=1)])

    assert not graph.has_edge("A", "B")
    assert set(graph.nodes()) == {"A", "B"}
    assert graph.nodes["A"]["gated_pairs"] == 1


def test_minimum_distinct_dates_is_configurable(monkeypatch):
    monkeypatch.setattr(CONFIG, "min_edge_distinct_dates", 1)

    graph = _graph([_edge(distinct_date_count=1)])

    assert graph.has_edge("A", "B")


def test_undated_pair_cannot_become_an_edge():
    # distinct_date_count 0 means no outage date could be parsed at all.
    graph = _graph([_edge(distinct_date_count=0)])

    assert not graph.has_edge("A", "B")


def test_zero_ppmi_pair_keeps_its_nodes():
    # p_ab == p_a * p_b exactly -> PMI 0 -> no edge, but the localities must
    # still be clusterable as singletons.
    graph = _graph(
        [_edge(notice_count=2, distinct_date_count=2)],
        total_notices=10,
        locality_counts={"A": 4, "B": 5},
    )

    assert not graph.has_edge("A", "B")
    assert set(graph.nodes()) == {"A", "B"}


def test_pair_count_above_its_marginal_is_a_corrupt_build():
    with pytest.raises(InconsistentBuildError):
        _graph([_edge(notice_count=5)], locality_counts={"A": 4, "B": 5})


def test_marginal_above_total_notices_is_a_corrupt_build():
    with pytest.raises(InconsistentBuildError):
        _graph([_edge()], total_notices=4, locality_counts={"A": 4, "B": 5})


def test_missing_marginal_is_a_corrupt_build_not_a_key_error():
    with pytest.raises(InconsistentBuildError):
        _graph([_edge()], locality_counts={"A": 4})


def test_zero_pair_count_is_rejected_by_the_model():
    with pytest.raises(Exception):
        _edge(notice_count=0)


# --- build-specific entry point -------------------------------------------


def _seed_two_disjoint_pairs():
    # Two pairs that never mix. A and B co-occur more often than chance
    # predicts, which is what gives them a positive PPMI; a pair present in
    # every notice would carry no information and correctly score 0.
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    _active_notice("n2", [("A", SFAX), ("B", SFAX)], "2026-07-21")
    _active_notice("n3", [("C", SFAX), ("D", SFAX)], "2026-07-22")
    _active_notice("n4", [("C", SFAX), ("D", SFAX)], "2026-07-23")
    return evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:00:00Z"
    )


def test_build_graph_for_build_uses_build_scoped_marginals_and_totals():
    build_id = _seed_two_disjoint_pairs()

    graph = build_graph_for_build(build_id)

    # ln((2/4) / ((2/4)*(2/4))) = ln 2
    assert graph["A"]["B"]["ppmi"] == pytest.approx(math.log(2), rel=1e-9)
    assert graph.graph["build_id"] == build_id
    assert graph.graph["config_version"] == CONFIG.version
    assert graph.graph["total_notices"] == 4
    assert not graph.has_edge("A", "C")


def test_build_graph_for_build_ignores_other_builds():
    build_id = _seed_two_disjoint_pairs()
    _active_notice("n5", [("E", SFAX), ("F", SFAX)], "2026-07-24")
    _active_notice("n6", [("E", SFAX), ("F", SFAX)], "2026-07-25")
    later = evidence_pipeline.build_model_evidence(
        created_at="2026-07-31T00:00:00Z"
    )

    first = build_graph_for_build(build_id)
    second = build_graph_for_build(later)

    assert set(first.nodes()) == {"A", "B", "C", "D"}
    assert set(second.nodes()) == {"A", "B", "C", "D", "E", "F"}
    assert first.graph["total_notices"] == 4
    assert second.graph["total_notices"] == 6


def test_build_edge_evidence_averages_confidence_components():
    build_id = _seed_two_disjoint_pairs()

    rows = {
        (row["locality_a"], row["locality_b"]): row
        for row in db.build_edge_evidence(build_id)
    }

    assert set(rows) == {("A", "B"), ("C", "D")}
    row = rows[("A", "B")]
    assert row["notice_count"] == 2
    assert row["distinct_date_count"] == 2
    assert row["mean_scope_confidence"] == 1.0
    assert row["mean_parse_confidence"] == 1.0
    assert row["geographic_confidence"] is None


def test_edge_evidence_api_lists_only_scoped_supporting_notices():
    # n1 pairs A and B inside one cell. n2 mentions both but in different
    # cells, so it is not evidence for the A-B edge and must not be listed.
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    _active_notice("n2", [("A", SFAX, 0), ("B", KERKENNAH, 1)], "2026-07-21")
    _active_notice("n3", [("A", SFAX), ("B", SFAX)], "2026-07-22")
    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:00:00Z"
    )

    evidence = db.edge_evidence(build_id, "A", "B")

    listed = [notice["notice_id"] for notice in evidence["notices"]]
    assert listed == ["n3", "n1"]
    assert evidence["distinct_notice_count"] == len(listed)
