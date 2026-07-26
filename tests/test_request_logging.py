import json

from fastapi.testclient import TestClient

from app import main


def _last_record(capsys):
    lines = capsys.readouterr().out.splitlines()
    return json.loads([line for line in lines if line.startswith("{")][-1])


def test_request_log_uses_route_template_and_generated_id(capsys):
    client = TestClient(main.app)

    response = client.get("/api/status")
    record = _last_record(capsys)

    assert record["request_id"] == response.headers["X-Request-ID"]
    assert record["route"] == "/api/status"
    assert record["method"] == "GET"
    assert record["status"] == 200
    assert record["duration_ms"] >= 0


def test_request_log_handles_404_without_query_values(capsys):
    client = TestClient(main.app)

    client.get("/missing?token=secret-value")
    record = _last_record(capsys)

    assert record["status"] == 404
    assert "token" not in json.dumps(record)
    assert "secret-value" not in json.dumps(record)


def test_request_log_includes_optional_operation_correlation(
    monkeypatch, capsys
):
    client = TestClient(main.app)
    monkeypatch.setattr(
        main.cluster_inference,
        "run_recluster",
        lambda: {
            "status": "ok",
            "run_date": "2026-07-26",
            "cluster_run_id": "run-1",
            "build_id": "build-1",
            "algorithm_version": "ppmi-louvain-v1",
            "localities_clustered": 2,
            "cluster_count": 1,
        },
    )
    monkeypatch.setattr(main, "CRON_SECRET", "test-secret")

    client.post(
        "/api/internal/recluster",
        headers={"X-Cron-Secret": "test-secret"},
    )
    record = _last_record(capsys)

    assert record["build_id"] == "build-1"
    assert record["cluster_run_id"] == "run-1"
    assert "X-Cron-Secret" not in json.dumps(record)

