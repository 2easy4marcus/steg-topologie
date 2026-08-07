import pytest

from app import db
from app.model.cluster_ids import JACCARD_SCALE, match_cluster_ids


def test_split_only_best_child_inherits_previous_id():
    previous = {7: {"A", "B", "C", "D"}}
    current = {0: {"A", "B", "C"}, 1: {"D", "E"}}

    result = match_cluster_ids(previous, current, next_id=8)

    assert result.ids[0] == 7
    assert result.ids[1] == 8
    assert result.lineage[0][0].similarity == 0.75
    assert result.next_id == 9


def test_similarity_below_half_allocates_new_id():
    result = match_cluster_ids(
        {7: {"A", "B", "C"}},
        {0: {"A", "X", "Y"}},
        next_id=8,
    )

    assert result.ids[0] == 8
    # Below the inheritance threshold but still a real relationship, so it is
    # recorded as lineage rather than discarded.
    assert result.lineage[0][0].previous_id == 7
    assert result.lineage[0][0].role == "related"


def test_globally_optimal_matching_beats_greedy_first_match():
    # Greedy over sorted new ids gives cluster 0 its best partner (X, 0.6) and
    # leaves cluster 1 nothing, total 0.6. The optimal one-to-one assignment
    # pairs 0->Y and 1->X for 0.5 + 1.0 = 1.5.
    previous = {
        10: {"a", "b", "c", "d", "e"},          # X
        11: {"p", "q"},                         # Y
    }
    current = {
        0: {"a", "b", "c", "p", "q"},
        1: {"a", "b", "c", "d", "e"},
    }

    result = match_cluster_ids(previous, current, next_id=99)

    assert result.ids[1] == 10
    assert result.ids[0] == 11


def test_matching_is_deterministic_under_exact_ties():
    previous = {1: {"A", "B"}, 2: {"A", "B"}}
    current = {0: {"A", "B"}, 5: {"A", "B"}}

    first = match_cluster_ids(previous, current, next_id=100)
    second = match_cluster_ids(previous, current, next_id=100)

    assert first.ids == second.ids


def test_jaccard_is_scaled_to_integers_for_exact_optimality():
    # networkx documents that float weights can return a slightly suboptimal
    # matching; integer weights are computed exactly.
    result = match_cluster_ids(
        {7: {"A", "B", "C", "D"}},
        {0: {"A", "B", "C"}},
        next_id=8,
    )

    assert result.lineage[0][0].weight == 3 * JACCARD_SCALE // 4
    assert isinstance(result.lineage[0][0].weight, int)


def test_lineage_records_split_and_merge_roles():
    split = match_cluster_ids(
        {7: {"A", "B", "C", "D"}},
        {0: {"A", "B", "C", "D"}, 1: {"A", "B", "C"}},
        next_id=8,
    )
    assert split.lineage[1][0].role == "split"

    merge = match_cluster_ids(
        {7: {"A", "B"}, 8: {"C", "D"}},
        {0: {"A", "B", "C", "D"}},
        next_id=9,
    )
    assert {row.role for row in merge.lineage[0]} == {"inherited", "merged"}


def test_no_previous_run_allocates_every_id_in_order():
    result = match_cluster_ids({}, {5: {"A"}, 2: {"B"}}, next_id=40)

    assert result.ids == {2: 40, 5: 41}
    assert result.next_id == 42


# --- persistent allocator --------------------------------------------------


def test_allocator_never_reuses_ids_across_reservations():
    first = db.reserve_cluster_ids(3)
    second = db.reserve_cluster_ids(2)

    assert first == 0
    assert second == 3


def test_allocator_survives_deleting_every_cluster_row():
    db.reserve_cluster_ids(5)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM cluster_members")

    # A MAX(cluster_id) reconstruction would restart at 0 and reissue ids
    # that retention has already handed out.
    assert db.reserve_cluster_ids(1) == 5


def test_reserving_nothing_does_not_advance_the_allocator():
    db.reserve_cluster_ids(2)

    assert db.reserve_cluster_ids(0) == 2
    assert db.reserve_cluster_ids(1) == 2


# --- lineage persistence ---------------------------------------------------


def _run(run_id, build_id="build-1"):
    db.create_model_build(build_id, "2026-07-26T10:00:00Z")
    db.create_cluster_run(
        run_id, build_id, "algo-v2", "2026-07-26T10:02:00Z"
    )
    return run_id


def test_lineage_rows_round_trip():
    _run("run-2")
    db.write_cluster_lineage(
        "run-2",
        "run-1",
        [
            {
                "cluster_id": 0,
                "previous_cluster_id": 7,
                "jaccard_similarity": 0.75,
                "role": "inherited",
            }
        ],
    )

    rows = db.cluster_lineage("run-2")

    assert rows[0]["previous_cluster_id"] == 7
    assert rows[0]["role"] == "inherited"
    assert rows[0]["previous_run_id"] == "run-1"


def test_lineage_rejects_similarity_outside_zero_to_one():
    _run("run-2")

    with pytest.raises(Exception):
        db.write_cluster_lineage(
            "run-2",
            "run-1",
            [
                {
                    "cluster_id": 0,
                    "previous_cluster_id": 7,
                    "jaccard_similarity": 1.5,
                    "role": "inherited",
                }
            ],
        )


def test_lineage_rejects_unknown_role():
    _run("run-2")

    with pytest.raises(Exception):
        db.write_cluster_lineage(
            "run-2",
            "run-1",
            [
                {
                    "cluster_id": 0,
                    "previous_cluster_id": 7,
                    "jaccard_similarity": 0.5,
                    "role": "reticulated",
                }
            ],
        )


def test_lineage_requires_an_existing_run():
    with pytest.raises(Exception):
        db.write_cluster_lineage(
            "ghost",
            "run-1",
            [
                {
                    "cluster_id": 0,
                    "previous_cluster_id": 7,
                    "jaccard_similarity": 0.5,
                    "role": "inherited",
                }
            ],
        )


def test_cluster_run_memberships_reads_a_run_by_id():
    _run("run-1")
    db.write_cluster_members(
        "run-1", {"A": 3, "B": 3, "C": 9}, {"A": 0.0, "B": 0.0, "C": 0.0}
    )

    assert db.cluster_run_memberships("run-1") == {3: {"A", "B"}, 9: {"C"}}
