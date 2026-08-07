from fastapi.testclient import TestClient

from app import db, main
from app.model.config import CONFIG
from app.model.validation import ValidationReport


def _validation(build_id, algorithm_version):
    return ValidationReport(
        build_id=build_id,
        config_version=CONFIG.version,
        algorithm_version=algorithm_version,
        validation_version=CONFIG.validation_version,
        random_seed=CONFIG.random_seed,
        bootstrap_runs=0,
        mean_membership_agreement=1.0,
    )


def _active_build_and_cluster():
    db.create_model_build("build-1", "2026-07-26T10:00:00Z")
    db.complete_model_build("build-1", "2026-07-26T10:01:00Z", 1, 1, 0)
    db.activate_completed_model_build("build-1")
    db.upsert_locality("Dekka", lat=34.1, lng=9.2)
    db.create_cluster_run(
        "run-1", "build-1", "ppmi-louvain-v1", "2026-07-26T10:02:00Z"
    )
    db.write_cluster_members("run-1", {"Dekka": 0}, {"Dekka": 0.8})
    db.complete_cluster_run("run-1", "2026-07-26T10:03:00Z", 1, 1)
    # Activation now also requires a completed validation run and a stored
    # published decision, so a fixture that activates directly has to supply
    # both rather than skip straight to activation.
    db.record_validation_run(
        "run-1",
        _validation("build-1", "ppmi-louvain-v1"),
        status="completed",
        evaluated_at="2026-07-26T10:03:30Z",
    )
    db.record_publication_decision(
        "cluster_run",
        "run-1",
        build_id="build-1",
        decision="published",
        config_version=CONFIG.version,
        decided_at="2026-07-26T10:03:40Z",
    )
    db.activate_completed_cluster_run("run-1")


def test_clusters_response_identifies_evidence_build():
    _active_build_and_cluster()
    client = TestClient(main.app)

    response = client.get("/api/clusters")

    body = response.json()
    assert body["cluster_run_id"] == "run-1"
    assert body["build_id"] == "build-1"
    assert body["active_build_id"] == "build-1"
    assert body["algorithm_version"] == "ppmi-louvain-v1"
    assert body["is_current"] is True
    assert body["clusters"][0]["locality"] == "Dekka"


def test_cluster_becomes_stale_when_evidence_build_changes():
    _active_build_and_cluster()
    db.create_model_build("build-2", "2026-07-26T11:00:00Z")
    db.complete_model_build("build-2", "2026-07-26T11:01:00Z", 1, 1, 0)
    db.activate_completed_model_build("build-2")
    client = TestClient(main.app)

    body = client.get("/api/clusters").json()

    assert body["build_id"] == "build-1"
    assert body["active_build_id"] == "build-2"
    assert body["is_current"] is False

