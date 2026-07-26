import json

from fastapi.testclient import TestClient

from app import db, main


def test_every_response_has_generated_request_id():
    client = TestClient(main.app)

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_valid_supplied_request_id_is_echoed():
    client = TestClient(main.app)

    response = client.get(
        "/api/status", headers={"X-Request-ID": "request-123"}
    )

    assert response.headers["X-Request-ID"] == "request-123"


def test_ingestion_status_comes_from_persistent_database():
    db.start_ingestion_run(
        "job-1",
        "backfill",
        "2026-07-26T10:00:00Z",
        request_id="request-1",
    )
    db.update_ingestion_run(
        "job-1",
        current_page=4,
        pages_scanned=5,
        notices_imported=12,
    )
    client = TestClient(main.app)

    response = client.get("/api/status/ingestion")

    assert response.status_code == 200
    run = response.json()["backfill"]
    assert run["id"] == "job-1"
    assert run["current_page"] == 4
    assert run["notices_imported"] == 12
    assert "internal_error_detail" not in json.dumps(response.json())


def test_public_status_reports_active_artifact_ids():
    db.create_model_build("build-1", "2026-07-26T10:00:00Z")
    db.complete_model_build("build-1", "2026-07-26T10:01:00Z", 0, 0, 0)
    db.activate_completed_model_build("build-1")
    client = TestClient(main.app)

    response = client.get("/api/status")

    assert response.json()["active_build_id"] == "build-1"
    assert response.json()["active_cluster_run_id"] is None


def test_request_log_contains_metadata_but_not_query_values(capsys):
    client = TestClient(main.app)

    client.get("/api/status?secret-looking=value")

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    record = records[-1]
    assert record["route"] == "/api/status"
    assert "secret-looking" not in json.dumps(record)
    assert "value" not in json.dumps(record)

