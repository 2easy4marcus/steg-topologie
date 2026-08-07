"""Private Sfax/Kerkennah candidate pilot: geography pin, generation,
ranking, sensitivity, and private persistence.

Nothing here is public. `tests/test_openapi_boundaries.py` already asserts the
public schema contains neither "candidate" nor "asset_id", which is the
privacy gate for this task; it is not duplicated here.

The canonical-build pin tests live in this file because migration 0004 is
what adds `model_builds.canonical_build_id` -- the deterministic
locality -> service-unit lookup that geography-bounded candidate generation
and the graph's geographic bonus both depend on.
"""

import pytest

from app import db, evidence_pipeline
from app.model import graph
from app.model.candidates import (
    CANDIDATE_RADIUS_KM,
    SCORING_VERSION,
    CandidateFeatures,
    LocalityGeography,
    generate_candidates,
    rank_candidates,
    run_candidate_pilot,
    weight_sensitivity,
)
from app.model.config import CONFIG
from app.topology import osm
from tests.test_model_builds import _active_notice

SFAX = "جهة صفاقس"
SNAPSHOT_ID = "sfax-2026-07-30"

# A point in central Sfax, and one ~55km south-west of it.
NEAR = (34.7400, 10.7600)
FAR = (34.3000, 10.2000)


# ---------------------------------------------------------------------------
# Ranking (the plan's Step 1 tests, with the fabricating defaults removed).


def test_ranking_exposes_components_and_is_not_probability():
    result = rank_candidates(
        [
            CandidateFeatures(
                asset_id="substation-a",
                outage_fit=0.9,
                topology_consistency=0.8,
                service_prior=0.7,
                distance_score=0.8,
                temporal_support=0.7,
                completeness=0.9,
            ),
            CandidateFeatures(
                asset_id="substation-b",
                outage_fit=0.5,
                topology_consistency=0.4,
                service_prior=0.5,
                distance_score=0.6,
                temporal_support=0.4,
                completeness=0.8,
            ),
        ],
        independent_dates=2,
        geography_accepted=True,
        source_registered=True,
    )
    first, second = result.candidates
    assert first.asset_id == "substation-a"
    assert first.score > second.score
    assert first.score_kind == "ranking_index"
    assert first.components["outage_fit"] == 0.9
    assert result.scoring_version == SCORING_VERSION
    assert (first.rank, second.rank) == (1, 2)


def test_insufficient_independent_dates_returns_no_ranking():
    result = rank_candidates(
        [],
        independent_dates=1,
        geography_accepted=True,
        source_registered=True,
    )
    assert result.status == "insufficient_evidence"


# ---------------------------------------------------------------------------
# Gates. None of these inputs has a default, so no call can fabricate them.


def _features(asset_id="a", **overrides):
    values = {
        "outage_fit": 0.5,
        "topology_consistency": 0.5,
        "service_prior": 0.5,
        "distance_score": 0.5,
        "temporal_support": 0.5,
        "completeness": 1.0,
    }
    values.update(overrides)
    return CandidateFeatures(asset_id=asset_id, **values)


def test_every_evidence_gate_is_required_and_none_may_be_defaulted():
    with pytest.raises(TypeError):
        rank_candidates([_features()])


@pytest.mark.parametrize(
    "gates",
    [
        {"independent_dates": CONFIG.min_edge_distinct_dates - 1},
        {"geography_accepted": False},
        {"source_registered": False},
    ],
)
def test_a_failed_gate_yields_insufficient_evidence(gates):
    kwargs = {
        "independent_dates": 2,
        "geography_accepted": True,
        "source_registered": True,
    } | gates

    result = rank_candidates([_features()], **kwargs)

    assert result.status == "insufficient_evidence"
    assert result.candidates == []


# ---------------------------------------------------------------------------
# Generation. Geography bounds the candidate set before anything is scored.


def _asset(asset_id, point, *, refs, voltage="30000", asset_type="substation"):
    return osm.GridAsset(
        asset_id=asset_id,
        asset_type=asset_type,
        latitude=point[0],
        longitude=point[1],
        voltage=voltage,
        node_refs=refs,
        source_snapshot_id=SNAPSHOT_ID,
    )


def _line(edge_id, refs):
    return osm.GridEdge(
        edge_id=edge_id,
        power_type="line",
        node_refs=refs,
        voltage="30000",
        source_snapshot_id=SNAPSHOT_ID,
    )


def _snapshot(assets, edges=()):
    return osm.TopologySnapshot(
        snapshot_id=SNAPSHOT_ID,
        nodes=[],
        assets=list(assets),
        edges=list(edges),
        relations=[],
        quarantined_relations=[],
    )


def _locality(name, point, unit="unit-a", confidence=1.0):
    return LocalityGeography(
        locality=name,
        latitude=point[0],
        longitude=point[1],
        service_unit_id=unit,
        spatial_confidence=confidence,
    )


def test_generation_is_bounded_by_cluster_geography():
    snapshot = _snapshot(
        [_asset("near", NEAR, refs=[1]), _asset("far", FAR, refs=[2])],
        [_line("line-1", [1]), _line("line-2", [2])],
    )

    rows = generate_candidates(
        snapshot,
        localities=[_locality("A", NEAR)],
        independent_dates=3,
    )

    assert [row.asset_id for row in rows] == ["near"]


def test_cluster_without_accepted_geography_generates_nothing():
    snapshot = _snapshot([_asset("near", NEAR, refs=[1])], [_line("l", [1])])

    rows = generate_candidates(
        snapshot,
        localities=[_locality("A", NEAR, confidence=0.0)],
        independent_dates=3,
    )

    assert rows == []


def test_asset_with_no_topology_or_service_signal_is_dropped():
    # An isolated pole beside a locality whose service unit was never
    # measured has nothing to say about the outage.
    snapshot = _snapshot(
        [_asset("orphan", NEAR, refs=[1], asset_type="pole", voltage=None)]
    )

    rows = generate_candidates(
        snapshot,
        localities=[_locality("A", NEAR, unit=None)],
        independent_dates=3,
    )

    assert rows == []


def test_incomplete_features_are_dropped_even_with_a_topology_signal():
    # Connected to a line (topology signal present) but with no voltage tag
    # and no measured service unit: 1 of 3 inputs, below the minimum.
    snapshot = _snapshot(
        [_asset("thin", NEAR, refs=[1], voltage=None)], [_line("l", [1])]
    )

    rows = generate_candidates(
        snapshot,
        localities=[_locality("A", NEAR, unit=None)],
        independent_dates=3,
    )

    assert rows == []


def test_component_reach_separates_topology_from_bare_proximity():
    # Two substations on one line, each next to a different locality. Each
    # covers half the cluster on its own but the shared feeder reaches all
    # of it, so topology_consistency exceeds outage_fit for both.
    other = (34.8400, 10.8600)
    snapshot = _snapshot(
        [_asset("s1", NEAR, refs=[1]), _asset("s2", other, refs=[2])],
        [_line("feeder", [1, 2])],
    )

    rows = {
        row.asset_id: row
        for row in generate_candidates(
            snapshot,
            localities=[_locality("A", NEAR), _locality("B", other)],
            independent_dates=3,
        )
    }

    assert set(rows) == {"s1", "s2"}
    assert rows["s1"].outage_fit == 0.5
    assert rows["s1"].topology_consistency == 1.0
    assert rows["s1"].service_prior == 1.0
    assert 0.0 < rows["s1"].distance_score <= 1.0
    assert rows["s1"].temporal_support == min(
        1.0, 3 / CONFIG.recurrence_saturation_dates
    )


def test_radius_is_the_only_thing_that_bounds_the_set():
    snapshot = _snapshot([_asset("far", FAR, refs=[1])], [_line("l", [1])])

    rows = generate_candidates(
        snapshot,
        localities=[_locality("A", NEAR)],
        independent_dates=3,
        radius_km=CANDIDATE_RADIUS_KM * 20,
    )

    assert [row.asset_id for row in rows] == ["far"]


# ---------------------------------------------------------------------------
# Sensitivity: deterministic perturbations, explicit ranks, asset-ID ties.


def test_sensitivity_persists_the_exact_min_max_rank_shape():
    rows = [_features("a", outage_fit=0.9), _features("b", outage_fit=0.1)]

    sensitivity = weight_sensitivity(rows)

    assert sensitivity == {
        "a": {"min_rank": 1, "max_rank": 1},
        "b": {"min_rank": 2, "max_rank": 2},
    }
    assert all(
        isinstance(value, int)
        for row in sensitivity.values()
        for value in row.values()
    )


def test_sensitivity_is_deterministic():
    rows = [_features("a", topology_consistency=0.6), _features("b")]

    assert weight_sensitivity(rows) == weight_sensitivity(rows)


def test_sensitivity_reports_a_rank_that_moves_under_reweighting():
    # `a` wins on the heaviest weight, `b` on a light one, tuned so the
    # 0.8/1.2 perturbation of outage_fit is enough to swap them.
    rows = [
        _features("a", outage_fit=0.62, completeness=0.0),
        _features("b", outage_fit=0.50, completeness=1.0),
    ]

    sensitivity = weight_sensitivity(rows)

    assert sensitivity["a"] == {"min_rank": 1, "max_rank": 2}
    assert sensitivity["b"] == {"min_rank": 1, "max_rank": 2}


def test_ties_are_broken_by_asset_id_and_still_get_distinct_ranks():
    rows = [_features("z-asset"), _features("a-asset")]

    result = rank_candidates(
        rows,
        independent_dates=2,
        geography_accepted=True,
        source_registered=True,
    )

    assert [row.asset_id for row in result.candidates] == [
        "a-asset",
        "z-asset",
    ]
    assert [row.rank for row in result.candidates] == [1, 2]
    assert weight_sensitivity(rows) == {
        "a-asset": {"min_rank": 1, "max_rank": 1},
        "z-asset": {"min_rank": 2, "max_rank": 2},
    }


# ---------------------------------------------------------------------------
# Private persistence.


def _seed_cluster_run(run_id="cluster-1", build_id="build-1", localities=()):
    db.create_model_build(build_id, "2026-07-30T00:00:00Z")
    db.complete_model_build(build_id, "2026-07-30T00:01:00Z", 1, 1, 1)
    db.create_cluster_run(run_id, build_id, "1", "2026-07-30T00:02:00Z")
    if localities:
        db.write_cluster_members(
            run_id, {name: 0 for name in localities}, {}
        )
    db.complete_cluster_run(run_id, "2026-07-30T00:03:00Z", 1, len(localities))
    return run_id, build_id


def _run_row(run_id):
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM asset_candidate_runs WHERE run_id = ?", [run_id]
        ).fetchone()


def test_candidate_run_carries_every_identity_it_was_produced_under():
    run_id, build_id = _seed_cluster_run()

    db.record_candidate_run(
        "cand-1",
        cluster_run_id=run_id,
        build_id=build_id,
        source_snapshot_id=SNAPSHOT_ID,
        config_version=CONFIG.version,
        scoring_version=SCORING_VERSION,
        radius_km=CANDIDATE_RADIUS_KM,
        status="experimental",
        created_at="2026-07-30T01:00:00Z",
        completed_at="2026-07-30T01:00:05Z",
    )

    row = _run_row("cand-1")
    assert row["cluster_run_id"] == run_id
    assert row["build_id"] == build_id
    assert row["source_snapshot_id"] == SNAPSHOT_ID
    assert row["config_version"] == CONFIG.version
    assert row["scoring_version"] == SCORING_VERSION
    assert row["radius_km"] == CANDIDATE_RADIUS_KM


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"cluster_run_id": "ghost"}, "unknown cluster run"),
        ({"build_id": "ghost"}, "unknown model build"),
    ],
)
def test_candidate_run_refuses_an_unknown_parent(overrides, message):
    run_id, build_id = _seed_cluster_run()
    kwargs = {
        "cluster_run_id": run_id,
        "build_id": build_id,
        "source_snapshot_id": SNAPSHOT_ID,
        "config_version": CONFIG.version,
        "scoring_version": SCORING_VERSION,
        "radius_km": CANDIDATE_RADIUS_KM,
        "status": "experimental",
        "created_at": "2026-07-30T01:00:00Z",
    } | overrides

    with pytest.raises(Exception, match=message):
        db.record_candidate_run("cand-1", **kwargs)


def test_scores_cannot_be_stored_against_an_insufficient_evidence_run():
    run_id, build_id = _seed_cluster_run()
    db.record_candidate_run(
        "cand-1",
        cluster_run_id=run_id,
        build_id=build_id,
        source_snapshot_id=SNAPSHOT_ID,
        config_version=CONFIG.version,
        scoring_version=SCORING_VERSION,
        radius_km=CANDIDATE_RADIUS_KM,
        status="insufficient_evidence",
        created_at="2026-07-30T01:00:00Z",
    )

    with pytest.raises(Exception, match="not an experimental run"):
        db.write_candidate_scores(
            "cand-1",
            0,
            rank_candidates(
                [_features()],
                independent_dates=2,
                geography_accepted=True,
                source_registered=True,
            ).candidates,
            {"a": {"min_rank": 1, "max_rank": 1}},
        )


def _pilot_stack(dates=("2026-07-20", "2026-07-21")):
    """A pinned build, geocoded localities, and a cluster run over them."""
    _canonical_geography({"A": "unit-a", "B": "unit-a"})
    db.upsert_locality("A", NEAR[0], NEAR[1])
    db.upsert_locality("B", NEAR[0] + 0.005, NEAR[1] + 0.005)
    for index, date in enumerate(dates):
        _active_notice(f"n{index}", [("A", SFAX), ("B", SFAX)], date)
    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:20:00Z"
    )
    db.create_cluster_run("cluster-1", build_id, "1", "2026-07-30T00:30:00Z")
    db.write_cluster_members("cluster-1", {"A": 0, "B": 0}, {})
    db.complete_cluster_run("cluster-1", "2026-07-30T00:31:00Z", 1, 2)
    return "cluster-1", build_id


def _pilot_snapshot():
    return _snapshot([_asset("s1", NEAR, refs=[1])], [_line("feeder", [1])])


def test_pilot_persists_a_ranked_run_privately():
    run_id, build_id = _pilot_stack()

    result = run_candidate_pilot(
        "cand-1",
        cluster_run_id=run_id,
        cluster_id=0,
        build_id=build_id,
        snapshot=_pilot_snapshot(),
        source_registered=True,
        created_at="2026-07-30T01:00:00Z",
    )

    assert result.status == "experimental"
    rows = db.candidate_scores("cand-1")
    assert [row["asset_id"] for row in rows] == ["s1"]
    assert rows[0]["rank"] == 1
    assert rows[0]["cluster_id"] == 0
    assert "outage_fit" in rows[0]["component_json"]
    assert rows[0]["sensitivity_json"] == '{"min_rank": 1, "max_rank": 1}'
    assert _run_row("cand-1")["completed_at"] == "2026-07-30T01:00:00Z"
    assert _run_row("cand-1")["source_snapshot_id"] == SNAPSHOT_ID


def test_pilot_stores_the_radius_it_actually_scored_with():
    # The scoring version alone would make an 8km run and a 20km run
    # indistinguishable, and recalibrating the radius is the pilot's purpose.
    run_id, build_id = _pilot_stack()

    run_candidate_pilot(
        "cand-1",
        cluster_run_id=run_id,
        cluster_id=0,
        build_id=build_id,
        snapshot=_pilot_snapshot(),
        source_registered=True,
        created_at="2026-07-30T01:00:00Z",
        radius_km=20.0,
    )

    assert _run_row("cand-1")["radius_km"] == 20.0


def test_pilot_refuses_an_unregistered_topology_source():
    run_id, build_id = _pilot_stack()

    result = run_candidate_pilot(
        "cand-1",
        cluster_run_id=run_id,
        cluster_id=0,
        build_id=build_id,
        snapshot=_pilot_snapshot(),
        source_registered=False,
        created_at="2026-07-30T01:00:00Z",
    )

    assert result.status == "insufficient_evidence"
    assert db.candidate_scores("cand-1") == []
    assert _run_row("cand-1")["status"] == "insufficient_evidence"


def test_pilot_measures_independent_dates_rather_than_assuming_them():
    # One outage date is one event, so the run has nothing to rank.
    run_id, build_id = _pilot_stack(dates=("2026-07-20",))

    result = run_candidate_pilot(
        "cand-1",
        cluster_run_id=run_id,
        cluster_id=0,
        build_id=build_id,
        snapshot=_pilot_snapshot(),
        source_registered=True,
        created_at="2026-07-30T01:00:00Z",
    )

    assert result.status == "insufficient_evidence"
    assert db.candidate_scores("cand-1") == []


# ---------------------------------------------------------------------------
# The canonical-geography pin, and the graph bonus it activates.


def _canonical_geography(units):
    """One completed, active canonical build with `units` locality -> unit."""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dataset_sources(
                source_id, title, owner, publication_class, refresh_policy,
                schema_version, acquisition_description
            ) VALUES ('src-1', 'Source', 'Owner', 'private_research',
                      'manual', '1', 'Test source')
            """
        )
        conn.execute(
            """
            INSERT INTO source_artifacts(
                artifact_id, source_id, relative_path, checksum_sha256,
                byte_size, retrieved_at, media_type, schema_version
            ) VALUES ('art-1', 'src-1', 'file.csv', ?, 1,
                      '2026-07-30T00:00:00Z', 'text/csv', '1')
            """,
            ["a" * 64],
        )
        conn.execute(
            """
            INSERT INTO canonical_builds(
                canonical_build_id, status, started_at, finished_at
            ) VALUES ('cb-1', 'completed', '2026-07-30T00:00:00Z',
                      '2026-07-30T00:10:00Z')
            """
        )
        for unit_id in sorted({unit for unit in units.values() if unit}):
            conn.execute(
                """
                INSERT INTO service_units(
                    unit_id, canonical_build_id, unit_type, name, region,
                    governorate, coordinate_complete, source_id, artifact_id,
                    source_record_key, transformation_version, created_at,
                    confidence
                ) VALUES (?, 'cb-1', 'district', 'Unit', 'South', 'Sfax', 0,
                          'src-1', 'art-1', ?, 'v1', '2026-07-30T00:00:00Z', 1)
                """,
                [unit_id, unit_id],
            )
        for locality, unit_id in sorted(units.items()):
            conn.execute(
                """
                INSERT INTO locality_context(
                    canonical_build_id, locality, context_build_id,
                    delegation_area_id, service_unit_id, spatial_confidence,
                    source_id, artifact_id, transformation_version, created_at
                ) VALUES ('cb-1', ?, 'ctx-1', NULL, ?, 1, 'src-1', 'art-1',
                          'v1', '2026-07-30T00:00:00Z')
                """,
                [locality, unit_id],
            )
        # The singleton row is seeded by migration 0001; activation is an
        # UPDATE of its pointer.
        conn.execute(
            "UPDATE canonical_state SET active_build_id = 'cb-1' "
            "WHERE state_id = 1"
        )


def _two_date_build():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    _active_notice("n2", [("A", SFAX), ("B", SFAX)], "2026-07-21")
    # A third, unrelated notice so A and B are not in every notice: a pair
    # with marginals equal to N has PPMI 0 and carries no graph edge at all.
    _active_notice("n3", [("C", SFAX), ("D", SFAX)], "2026-07-22")
    return evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:20:00Z"
    )


def _geographic_confidence(build_id):
    return db.build_edge_evidence(build_id)[0]["geographic_confidence"]


def test_build_pins_the_canonical_geography_active_when_it_started():
    _canonical_geography({"A": "unit-a", "B": "unit-a"})

    build_id = _two_date_build()

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT canonical_build_id FROM model_builds WHERE build_id = ?",
            [build_id],
        ).fetchone()
    assert row["canonical_build_id"] == "cb-1"


def test_pinned_shared_service_unit_activates_the_geographic_bonus():
    _canonical_geography({"A": "unit-a", "B": "unit-a"})

    build_id = _two_date_build()

    assert _geographic_confidence(build_id) == 1.0
    edge = graph.build_graph_for_build(build_id)["A"]["B"]
    assert edge["geographic_bonus"] == CONFIG.max_geographic_bonus


def test_pinned_different_service_units_measure_disagreement_not_absence():
    _canonical_geography({"A": "unit-a", "B": "unit-b"})

    build_id = _two_date_build()

    assert _geographic_confidence(build_id) == 0.0
    assert graph.build_graph_for_build(build_id)["A"]["B"][
        "geographic_bonus"
    ] == 0.0


def test_locality_missing_from_the_pinned_geography_stays_unmeasured():
    _canonical_geography({"A": "unit-a"})

    assert _geographic_confidence(_two_date_build()) is None


def test_build_without_a_pin_keeps_geography_unmeasured_and_bonus_zero():
    build_id = _two_date_build()

    assert _geographic_confidence(build_id) is None
    assert graph.build_graph_for_build(build_id)["A"]["B"][
        "geographic_bonus"
    ] == 0.0


def test_build_locality_geography_resolves_only_through_the_pin():
    _canonical_geography({"A": "unit-a", "B": "unit-a"})
    build_id = _two_date_build()

    geography = db.build_locality_geography(build_id)

    assert geography["A"]["service_unit_id"] == "unit-a"
    assert geography["A"]["spatial_confidence"] == 1.0
