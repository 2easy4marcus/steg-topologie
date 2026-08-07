# tests/test_cluster_inference.py
from app import db, cluster_inference, evidence_pipeline, model_readiness
from tests.test_model_builds import _active_notice


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


def _readiness(quality_ready=True, operational_ready=True):
    return model_readiness.ReadinessReport(
        build_id="build-1",
        model_quality=model_readiness.ReadinessSection(
            ready=quality_ready, signals=[]
        ),
        operational_health=model_readiness.ReadinessSection(
            ready=operational_ready, signals=[]
        ),
    )


def test_recluster_refuses_run_when_model_quality_fails(monkeypatch):
    db.create_model_build("build-1", "2026-07-26T10:00:00Z")
    db.complete_model_build("build-1", "2026-07-26T10:01:00Z", 30, 10, 20)
    db.activate_completed_model_build("build-1")
    monkeypatch.setattr(
        cluster_inference.model_readiness,
        "evaluate",
        lambda build_id=None: _readiness(quality_ready=False),
    )

    result = cluster_inference.run_recluster()

    assert result["status"] == "insufficient_data"
    assert db.active_cluster_run_id() is None


def test_recluster_persists_source_build_and_algorithm(monkeypatch):
    # Build through the real pipeline: V2 reads the pinned snapshot
    # (build_notice_parses / build_locality_observations /
    # build_pair_observations), so hand-written build_cooccurrences rows are
    # no longer a sufficient fixture.
    subregion = "جهة صفاقس"
    _active_notice("n1", [("A", subregion), ("B", subregion)], "2026-07-20")
    _active_notice("n2", [("A", subregion), ("B", subregion)], "2026-07-21")
    _active_notice("n3", [("C", subregion), ("D", subregion)], "2026-07-22")
    _active_notice("n4", [("C", subregion), ("D", subregion)], "2026-07-23")
    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-26T10:00:00Z"
    )
    for name in ("A", "B", "C", "D"):
        db.upsert_locality(name, lat=1.0, lng=1.0)

    monkeypatch.setattr(
        cluster_inference.model_readiness,
        "evaluate",
        lambda build_id=None: _readiness(),
    )
    monkeypatch.setattr(
        cluster_inference.geocoding, "geocode_all_pending", lambda: None
    )

    result = cluster_inference.run_recluster()
    active = db.active_cluster_run()

    assert result["status"] == "ok"
    assert active["build_id"] == build_id
    assert active["algorithm_version"] == "evidence-weighted-louvain-v2"
    assert result["localities_clustered"] == 4

    repeated = cluster_inference.run_recluster()
    assert repeated["status"] == "already_done"
    assert repeated["cluster_run_id"] == active["run_id"]
