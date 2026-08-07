import pytest

from app import db, evidence_pipeline, model_readiness
from app.model.config import CONFIG
from tests.test_model_builds import _active_notice

SFAX = "جهة صفاقس"
KERKENNAH = "جهة قرقنة"


def _build(created_at="2026-07-30T00:00:00Z"):
    return evidence_pipeline.build_model_evidence(created_at=created_at)


def _pairs(build_id):
    return {
        (row["locality_a"], row["locality_b"]): row
        for row in db.build_scoped_cooccurrences(build_id)
    }


def test_different_named_subregions_do_not_get_full_strength_edge():
    _active_notice(
        "n1",
        [("A", SFAX), ("B", SFAX), ("C", KERKENNAH)],
        "2026-07-20",
    )

    rows = _pairs(_build())

    assert rows[("A", "B")]["scope_kind"] == "subregion"
    assert rows[("A", "B")]["scope_confidence"] == 1.0
    assert ("A", "C") not in rows


def test_headerless_notice_uses_low_confidence_fallback():
    _active_notice("n1", [("A", None), ("B", None)], "2026-07-20")

    row = db.build_scoped_cooccurrences(_build())[0]

    assert row["scope_kind"] == "notice_fallback"
    assert row["scope_confidence"] == 0.35


def test_duplicate_subregion_headings_remain_distinct_scopes():
    # Two source cells carrying the identical heading are two scopes, so
    # localities from different cells must not be paired.
    _active_notice(
        "n1",
        [("A", SFAX, 0), ("B", SFAX, 0), ("C", SFAX, 1), ("D", SFAX, 1)],
        "2026-07-20",
    )

    rows = _pairs(_build())

    assert set(rows) == {("A", "B"), ("C", "D")}
    assert rows[("A", "B")]["scope_ordinal"] == 0
    assert rows[("C", "D")]["scope_ordinal"] == 1


def test_unheaded_cell_in_mixed_notice_is_its_own_scope_not_dropped():
    # A cell whose heading failed to parse is still an observed boundary. It
    # must keep producing observations rather than vanishing from the build.
    _active_notice(
        "n1",
        [("A", SFAX, 0), ("B", SFAX, 0), ("C", None, 1), ("D", None, 1)],
        "2026-07-20",
    )

    rows = _pairs(_build())

    assert set(rows) == {("A", "B"), ("C", "D")}
    assert rows[("C", "D")]["scope_kind"] == "subregion"
    assert rows[("C", "D")]["scope_name"] is None


def test_missing_outage_date_yields_no_temporal_confidence_or_dates():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], None)

    build_id = _build()

    assert db.build_scoped_cooccurrences(build_id)[0][
        "temporal_confidence"
    ] is None
    assert db.build_cooccurrences(build_id)[0]["distinct_date_count"] == 0


def test_warning_parses_are_downweighted_not_dropped():
    _active_notice(
        "n1",
        [("A", SFAX), ("B", SFAX)],
        "2026-07-20",
        parse_status="warning",
    )

    row = db.build_scoped_cooccurrences(_build())[0]

    assert row["parse_confidence"] == CONFIG.warning_parse_confidence
    assert row["config_version"] == CONFIG.version


def test_geographic_confidence_is_unmeasured_not_assumed():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")

    assert db.build_scoped_cooccurrences(_build())[0][
        "geographic_confidence"
    ] is None


def test_population_is_idempotent_and_retryable():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    build_id = _build()
    before = db.build_scoped_cooccurrences(build_id)

    db.pin_build_snapshot(build_id)
    db.populate_scoped_observations(build_id, CONFIG)
    db.populate_model_build(build_id)

    assert db.build_scoped_cooccurrences(build_id) == before
    assert db.build_locality_counts(build_id) == {"A": 1, "B": 1}


def test_parser_activation_during_build_cannot_change_population():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    build_id = "pinned-build"
    db.create_model_build(build_id, "2026-07-30T00:00:00Z")
    db.pin_build_snapshot(build_id)

    # A competing parse activates after the snapshot is pinned.
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notice_parses(
                parse_id, snapshot_id, notice_id, title, notice_date_iso,
                parser_version, normalization_version, parse_status,
                parse_warnings, parsed_at
            ) VALUES ('parse-late', 'snapshot-n1', 'n1', 'n1', '2026-07-21',
                      '3', '2', 'ok', '[]', '2026-07-30T00:05:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_localities(
                parse_id, ordinal, raw_name, canonical_name,
                subregion_name, scope_ordinal
            ) VALUES ('parse-late', 0, 'Z', 'Z', ?, 0)
            """,
            [SFAX],
        )
        conn.execute(
            "UPDATE notice_state SET active_parse_id = 'parse-late' "
            "WHERE notice_id = 'n1'"
        )

    db.populate_scoped_observations(build_id, CONFIG)
    db.populate_model_build(build_id)

    assert set(_pairs(build_id)) == {("A", "B")}
    assert db.model_build_counts(build_id) == (1, 2, 1)


def test_readiness_counts_scoped_pairs_not_whole_notice_pairs():
    # Ten localities split across two cells: whole-notice Cartesian pairing
    # would report a much larger influence than the scoped truth.
    left = [(f"L{i}", SFAX, 0) for i in range(5)]
    right = [(f"R{i}", KERKENNAH, 1) for i in range(5)]
    _active_notice("n1", left + right, "2026-07-20")

    build_id = _build()
    metrics = db.model_readiness_metrics(build_id)

    # 2 x C(5,2) scoped pairs, never C(10,2) = 45.
    assert len(db.build_scoped_cooccurrences(build_id)) == 20
    assert metrics["valid_notices"] == 1
    assert metrics["largest_notice_pair_share"] == 1.0


def test_repeated_pairs_require_two_distinct_dates_not_two_notices():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    _active_notice("n2", [("A", SFAX), ("B", SFAX)], "2026-07-20")

    metrics = db.model_readiness_metrics(_build())

    assert metrics["repeated_pairs"] == 0

    _active_notice("n3", [("A", SFAX), ("B", SFAX)], "2026-07-21")

    assert db.model_readiness_metrics(_build())["repeated_pairs"] == 1


def test_quality_gates_and_publication_decision_are_recorded():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")

    build_id = _build()
    gates = {row["gate_key"]: row for row in db.quality_gate_results(build_id)}
    decision = db.publication_decision("evidence_build", build_id)

    assert gates["valid_notices"]["outcome"] == "fail"
    assert gates["valid_notices"]["reason_code"] == "valid_notices_below_minimum"
    assert gates["valid_notices"]["required_value"] == CONFIG.min_valid_notices
    assert all(row["config_version"] == CONFIG.version for row in gates.values())
    assert decision["decision"] == "blocked"
    assert decision["reason_code"] == "model_gates_failed"
    assert decision["build_id"] == build_id
    assert decision["config_version"] == CONFIG.version


def test_publication_states_track_which_section_failed(monkeypatch):
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")

    real_evaluate = model_readiness.evaluate

    def _ready(*, now=None, build_id=None):
        report = real_evaluate(now=now, build_id=build_id)
        report.model_quality.ready = True
        return report

    monkeypatch.setattr(model_readiness, "evaluate", _ready)
    build_id = _build()

    assert db.publication_decision("evidence_build", build_id)[
        "decision"
    ] == "experimental"


def test_out_of_range_confidence_is_rejected_by_the_database():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    build_id = _build()

    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO build_pair_observations(
                    build_id, notice_id, scope_kind, scope_ordinal,
                    locality_a, locality_b, parse_confidence,
                    scope_confidence, canonicalization_confidence,
                    config_version
                ) VALUES (?, 'n1', 'subregion', 9, 'X', 'Y', 1.4, 1.0, 1.0, 'v')
                """,
                [build_id],
            )


def test_reversed_pair_ordering_is_rejected_by_the_database():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    build_id = _build()

    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO build_pair_observations(
                    build_id, notice_id, scope_kind, scope_ordinal,
                    locality_a, locality_b, parse_confidence,
                    scope_confidence, canonicalization_confidence,
                    config_version
                ) VALUES (?, 'n1', 'subregion', 9, 'Y', 'X', 1.0, 1.0, 1.0, 'v')
                """,
                [build_id],
            )


def test_observations_require_a_pinned_notice():
    _active_notice("n1", [("A", SFAX), ("B", SFAX)], "2026-07-20")
    build_id = _build()

    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO build_pair_observations(
                    build_id, notice_id, scope_kind, scope_ordinal,
                    locality_a, locality_b, parse_confidence,
                    scope_confidence, canonicalization_confidence,
                    config_version
                ) VALUES (?, 'ghost', 'subregion', 0, 'X', 'Y',
                          1.0, 1.0, 1.0, 'v')
                """,
                [build_id],
            )


def test_legacy_scope_ordinals_are_inferred_from_stored_headings():
    assert evidence_pipeline.infer_scope_ordinals(
        [SFAX, SFAX, KERKENNAH, None]
    ) == [0, 0, 1, 2]
    assert evidence_pipeline.infer_scope_ordinals([None, None]) == [None, None]
