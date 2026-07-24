# tests/test_cluster_inference.py
from app import db, cluster_inference


def test_ppmi_graph_weights_known_fixture():
    # 10 total notices. A and B always appear together (3 notices each,
    # always paired) -- clearly informative co-occurrence, positive PMI.
    # C and D co-occur once out of 10, and are otherwise rare too --
    # still informative, still positive PMI. A never co-occurs with C or D.
    db.increment_locality_notice_count("A")
    db.increment_locality_notice_count("A")
    db.increment_locality_notice_count("A")
    db.increment_locality_notice_count("B")
    db.increment_locality_notice_count("B")
    db.increment_locality_notice_count("B")
    db.increment_locality_notice_count("C")
    db.increment_locality_notice_count("D")

    cooccurrences = [
        {"locality_a": "A", "locality_b": "B", "notice_count": 3},
        {"locality_a": "C", "locality_b": "D", "notice_count": 1},
    ]
    G = cluster_inference.build_ppmi_graph(cooccurrences, total_notices=10)
    assert G.has_edge("A", "B")
    assert G.has_edge("C", "D")
    assert not G.has_edge("A", "C")
    assert G["A"]["B"]["weight"] > 0
    assert G["C"]["D"]["weight"] > 0


def test_compute_clusters_finds_two_obvious_communities():
    # Seed real per-locality notice counts consistent with the
    # cooccurrence fixture below (A-B=10, B-C=10, X-Y=10, Y-Z=10) so that
    # build_ppmi_graph's db.get_locality_notice_counts() lookup returns
    # non-zero marginals for every locality involved.
    for locality in ("A", "B", "C", "X", "Y", "Z"):
        for _ in range(10):
            db.increment_locality_notice_count(locality)

    cooccurrences = [
        {"locality_a": "A", "locality_b": "B", "notice_count": 10},
        {"locality_a": "B", "locality_b": "C", "notice_count": 10},
        {"locality_a": "X", "locality_b": "Y", "notice_count": 10},
        {"locality_a": "Y", "locality_b": "Z", "notice_count": 10},
    ]
    G = cluster_inference.build_ppmi_graph(cooccurrences, total_notices=20)
    partition = cluster_inference.compute_clusters(G)
    assert partition["A"] == partition["B"] == partition["C"]
    assert partition["X"] == partition["Y"] == partition["Z"]
    assert partition["A"] != partition["X"]


def test_compute_clusters_handles_isolated_node():
    G = cluster_inference.build_ppmi_graph([], total_notices=1)
    G.add_node("Lonely")
    partition = cluster_inference.compute_clusters(G)
    assert "Lonely" in partition


def test_stability_high_when_membership_identical_across_runs():
    db.write_cluster_run("2026-07-20", {"A": 0, "B": 0}, {"A": 0, "B": 0})
    db.write_cluster_run("2026-07-21", {"A": 0, "B": 0}, {"A": 0, "B": 0})
    stability = cluster_inference.compute_stability("2026-07-22", {"A": 5, "B": 5})
    assert stability["A"] == 1.0
    assert stability["B"] == 1.0


def test_stability_zero_when_no_prior_runs():
    stability = cluster_inference.compute_stability("2026-07-24", {"A": 0, "B": 0})
    assert stability["A"] == 0.0
    assert stability["B"] == 0.0


def test_stability_zero_for_locality_new_to_todays_run():
    db.write_cluster_run("2026-07-20", {"A": 0, "B": 0}, {"A": 0, "B": 0})
    stability = cluster_inference.compute_stability("2026-07-21", {"A": 0, "B": 0, "C": 1})
    assert stability["A"] == 1.0
    assert stability["B"] == 1.0
    assert stability["C"] == 0.0  # "C" never appeared in any prior run
