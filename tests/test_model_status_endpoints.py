# tests/test_model_status_endpoints.py
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_model_status_empty_db():
    resp = client.get("/api/model-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["notices_so_far"] == 0
    assert body["notices_needed"] == 30
    assert body["localities_so_far"] == 0
    assert body["localities_needed"] == 10
    assert body["data_floor_met"] is False
    assert body["cluster_count"] == 0
    assert body["average_stability"] == 0.0
    assert body["last_run_date"] is None
    assert body["days_of_history"] == 0


def test_model_status_after_cluster_run():
    db.upsert_locality("Dekka")
    db.upsert_locality("Tozeur")
    db.write_cluster_run("2026-07-24", {"Dekka": 0, "Tozeur": 0}, {"Dekka": 0.5, "Tozeur": 1.0})
    resp = client.get("/api/model-status")
    body = resp.json()
    assert body["cluster_count"] == 1
    assert body["average_stability"] == 0.75
    assert body["last_run_date"] == "2026-07-24"
    assert body["days_of_history"] == 1


def test_cooccurrences_empty():
    resp = client.get("/api/cooccurrences")
    assert resp.status_code == 200
    assert resp.json() == {"edges": []}


def test_cooccurrences_returns_edges():
    db.increment_cooccurrence("Dekka", "Tozeur")
    db.increment_cooccurrence("Dekka", "Tozeur")
    resp = client.get("/api/cooccurrences")
    body = resp.json()
    assert body["edges"] == [{"locality_a": "Dekka", "locality_b": "Tozeur", "notice_count": 2}]
