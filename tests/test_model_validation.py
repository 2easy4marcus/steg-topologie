import pytest

from app import db, evidence_pipeline
from app.model.config import CONFIG
from app.model.validation import (
    METRIC_DEFINITIONS,
    ValidationReport,
    co_membership_agreement,
    temporal_split,
    validate_cluster_run,
)
from app.model.graph import build_graph_for_build
from tests.test_model_builds import _active_notice
from tests.test_private_candidates import _canonical_geography

SFAX = "جهة صفاقس"


# --- label invariance ------------------------------------------------------


def test_agreement_ignores_cluster_labels():
    left = {"A": 0, "B": 0, "C": 1}
    relabelled = {"A": 9, "B": 9, "C": 4}

    assert co_membership_agreement(left, relabelled) == 1.0


def test_agreement_falls_when_membership_actually_changes():
    left = {"A": 0, "B": 0, "C": 0}
    right = {"A": 0, "B": 0, "C": 1}

    # 3 pairs; AB still together, AC and BC split -> 1/3 agree.
    assert co_membership_agreement(left, right) == pytest.approx(1 / 3)


def test_agreement_uses_only_localities_present_in_both():
    assert co_membership_agreement({"A": 0, "B": 0}, {"A": 1, "B": 1, "Z": 2}) == 1.0


def test_agreement_of_fewer_than_two_shared_localities_is_none():
    assert co_membership_agreement({"A": 0}, {"A": 0}) is None


# --- temporal holdout ------------------------------------------------------


def test_temporal_split_never_puts_one_date_on_both_sides():
    train, holdout = temporal_split(
        ["2026-07-01", "2026-07-01", "2026-07-02", "2026-07-03",
         "2026-07-04", "2026-07-05"]
    )

    assert train & holdout == set()
    assert max(train) < min(holdout)


def test_temporal_split_keeps_the_holdout_non_empty():
    train, holdout = temporal_split(["a", "b"])

    assert train == {"a"}
    assert holdout == {"b"}


def test_temporal_split_of_a_single_date_yields_no_holdout():
    train, holdout = temporal_split(["only"])

    assert train == {"only"}
    assert holdout == set()


# --- report contract -------------------------------------------------------


def _seeded_build(dates=("2026-07-20", "2026-07-21", "2026-07-22")):
    for index, day in enumerate(dates):
        _active_notice(f"ab{index}", [("A", SFAX), ("B", SFAX)], day)
        _active_notice(f"cd{index}", [("C", SFAX), ("D", SFAX)], day)
    return evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:00:00Z"
    )


def test_report_records_every_identity_and_the_seed():
    build_id = _seeded_build()

    report = validate_cluster_run(build_id, algorithm_version="algo-v2")

    assert isinstance(report, ValidationReport)
    assert report.build_id == build_id
    assert report.config_version == CONFIG.version
    assert report.algorithm_version == "algo-v2"
    assert report.validation_version == CONFIG.validation_version
    assert report.random_seed == CONFIG.random_seed
    assert report.bootstrap_runs == CONFIG.bootstrap_runs


def test_report_carries_its_own_metric_definitions():
    report = validate_cluster_run(_seeded_build(), algorithm_version="a")

    # The numbers are meaningless without the rule that produced them, so the
    # report states each one rather than leaving it to external docs.
    assert report.metric_definitions == METRIC_DEFINITIONS
    for key in (
        "membership_agreement",
        "bootstrap_sampling_unit",
        "prediction_rule",
        "recall_denominator",
        "unseen_locality_treatment",
    ):
        assert METRIC_DEFINITIONS[key]


def test_bootstrap_resamples_outage_dates_not_notices():
    assert METRIC_DEFINITIONS["bootstrap_sampling_unit"] == "outage_date"

    report = validate_cluster_run(_seeded_build(), algorithm_version="a")

    assert 0.0 <= report.mean_membership_agreement <= 1.0


def test_validation_is_reproducible_for_a_fixed_seed():
    build_id = _seeded_build()

    first = validate_cluster_run(build_id, algorithm_version="a")
    second = validate_cluster_run(build_id, algorithm_version="a")

    assert first.mean_membership_agreement == second.mean_membership_agreement
    assert first.held_out_edge_recall == second.held_out_edge_recall


def test_unmeasurable_scores_are_none_with_a_stated_reason():
    build_id = _seeded_build()

    report = validate_cluster_run(build_id, algorithm_version="a")

    # No geography is pinned to an evidence build yet, so these baselines
    # cannot be computed. They must report absence, not a fabricated score.
    assert report.geography_baseline is None
    assert report.service_unit_baseline is None
    assert "geography" in report.unmeasured_reasons


def test_too_few_holdout_dates_reports_none_recall():
    build_id = _seeded_build(dates=("2026-07-20",))

    report = validate_cluster_run(build_id, algorithm_version="a")

    assert report.held_out_edge_recall is None
    assert "held_out_edge_recall" in report.unmeasured_reasons


def test_report_persists_against_its_run_and_is_readable():
    build_id = _seeded_build()
    db.create_cluster_run("run-1", build_id, "algo-v2", "2026-07-30T00:00:00Z")
    report = validate_cluster_run(build_id, algorithm_version="algo-v2")

    db.record_validation_run(
        "run-1",
        report,
        status="completed",
        evaluated_at="2026-07-30T00:05:00Z",
    )
    stored = db.validation_run("run-1")

    assert stored["status"] == "completed"
    assert stored["build_id"] == build_id
    assert stored["random_seed"] == CONFIG.random_seed
    assert stored["validation_version"] == CONFIG.validation_version


def test_validation_run_rejects_an_unknown_status():
    build_id = _seeded_build()
    db.create_cluster_run("run-1", build_id, "algo-v2", "2026-07-30T00:00:00Z")
    report = validate_cluster_run(build_id, algorithm_version="algo-v2")

    with pytest.raises(Exception):
        db.record_validation_run(
            "run-1",
            report,
            status="probably-fine",
            evaluated_at="2026-07-30T00:05:00Z",
        )


# --- validation scores the graph production serves --------------------------
#
# Validation used to rebuild its own graph from the raw observation rows.
# That parallel builder drifted from production in three ways, so every score
# it reported described a model that was never served. It now goes through
# build_graph_for_build like production does; these lock the three.


def test_marginals_count_localities_seen_only_in_single_locality_scopes():
    # E shares a notice with nobody, so it produces no pair row at all.
    _active_notice("ab0", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    _active_notice("ab1", [("A", SFAX), ("B", SFAX)], "2026-07-21")
    _active_notice("e0", [("E", SFAX)], "2026-07-22")
    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:00:00Z"
    )

    _, marginals, total_notices = db.build_graph_inputs(build_id)

    # Derived from pair rows -- as validation used to do -- E vanishes and N
    # is 2, so every marginal is understated and PPMI comes out systematically
    # higher than the value production actually clustered on.
    assert marginals == db.build_locality_counts(build_id)
    assert marginals["E"] == 1
    assert total_notices == db.model_build_counts(build_id)[0] == 3


def test_restricted_graphs_carry_the_geographic_bonus_production_applies():
    _canonical_geography({"A": "unit-a", "B": "unit-a"})
    build_id = _seeded_build()

    served = build_graph_for_build(build_id)
    # The bootstrap and holdout graphs are the ones validation scores against.
    restricted = build_graph_for_build(
        build_id, dates={"2026-07-20", "2026-07-21"}
    )

    # Hardcoding geographic_confidence=None here is what made validation score
    # a bonus-free graph while production served a bonus-bearing one.
    assert served["A"]["B"]["geographic_bonus"] > 0
    assert restricted["A"]["B"]["geographic_bonus"] > 0


def test_restricting_dates_matches_a_build_that_never_saw_the_others():
    kept = ("2026-07-20", "2026-07-21")
    narrow_build = _seeded_build(dates=kept)
    # The same evidence, plus one more outage day.
    _active_notice("ab2", [("A", SFAX), ("B", SFAX)], "2026-07-22")
    _active_notice("cd2", [("C", SFAX), ("D", SFAX)], "2026-07-22")
    wide_build = evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T01:00:00Z"
    )

    restricted = db.build_graph_inputs(wide_build, dates=set(kept))
    narrow = db.build_graph_inputs(narrow_build)

    # Restriction has to be equivalent to never having had the extra evidence,
    # or a bootstrap replicate is not a resample of the model at all.
    assert restricted[1] == narrow[1]
    assert restricted[2] == narrow[2]
    assert [dict(row) for row in restricted[0]] == [
        dict(row) for row in narrow[0]
    ]


def test_the_report_scores_the_partition_it_is_handed():
    build_id = _seeded_build()

    served = validate_cluster_run(
        build_id, algorithm_version="a", partition={"A": 0, "B": 0, "C": 1, "D": 1}
    )
    wrong = validate_cluster_run(
        build_id, algorithm_version="a", partition={"A": 0, "B": 1, "C": 0, "D": 1}
    )

    # A run's own memberships are what the scores are about; a report that
    # re-derives them describes a model nobody was served.
    assert served.mean_membership_agreement != wrong.mean_membership_agreement


def test_a_pinned_geography_is_not_reported_as_missing():
    _canonical_geography({"A": "unit-a", "B": "unit-a"})
    build_id = _seeded_build()

    report = validate_cluster_run(build_id, algorithm_version="a")

    assert db.build_canonical_build_id(build_id) == "cb-1"
    assert "no canonical geography is pinned" not in report.unmeasured_reasons[
        "geography"
    ]
    # Still unscored -- but for the reason that is actually true.
    assert report.geography_baseline is None
