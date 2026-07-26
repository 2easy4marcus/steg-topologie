# tests/test_app_internal_endpoints.py
import os
from fastapi.testclient import TestClient

from app import db


def _client(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-secret")
    import importlib
    import app.main as app_module
    importlib.reload(app_module)  # pick up the freshly-set env var
    return TestClient(app_module.app), app_module


def test_internal_scrape_requires_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    resp = client.post("/api/internal/scrape")
    assert resp.status_code == 401


def test_internal_scrape_succeeds_with_correct_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    monkeypatch.setattr(app_module.import_official, "run", lambda verbose=True: 0)
    resp = client.post("/api/internal/scrape", headers={"X-Cron-Secret": "test-secret"})
    assert resp.status_code == 200
    assert resp.json()["notices_processed"] == 0


def test_internal_recluster_requires_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    resp = client.post("/api/internal/recluster")
    assert resp.status_code == 401


def test_internal_recluster_idempotent_same_day(monkeypatch):
    client, app_module = _client(monkeypatch)
    monkeypatch.setattr(
        app_module.cluster_inference, "run_recluster",
        lambda: {"status": "ok", "run_date": "2026-07-24", "localities_clustered": 2, "cluster_count": 1},
    )
    resp = client.post("/api/internal/recluster", headers={"X-Cron-Secret": "test-secret"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_get_clusters_returns_insufficient_data_when_no_runs(monkeypatch):
    client, app_module = _client(monkeypatch)
    resp = client.get("/api/clusters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["insufficient_data"] is True
    assert body["data"] == []


def test_get_clusters_returns_points_when_data_exists(monkeypatch):
    client, app_module = _client(monkeypatch)
    db.upsert_locality("Dekka", lat=34.1, lng=9.2)
    db.write_cluster_run("2026-07-24", {"Dekka": 0}, {"Dekka": 0.8})
    resp = client.get("/api/clusters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["insufficient_data"] is False
    assert body["data"][0]["locality"] == "Dekka"
    assert body["data"][0]["lat"] == 34.1
    assert body["data"][0]["stability"] == 0.8


def test_internal_backfill_requires_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    resp = client.post("/api/internal/backfill")
    assert resp.status_code == 401


def test_internal_backfill_succeeds_with_correct_secret(monkeypatch):
    client, app_module = _client(monkeypatch)
    monkeypatch.setattr(app_module.backfill_official, "crawl_archive", lambda verbose=True: 5)
    resp = client.post("/api/internal/backfill", headers={"X-Cron-Secret": "test-secret"})
    assert resp.status_code == 200
    assert resp.json()["notices_processed"] == 5
