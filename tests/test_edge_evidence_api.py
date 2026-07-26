from fastapi.testclient import TestClient

from app import db, evidence_pipeline, import_official, main


def _seed_notice(notice_id, date, subregion):
    notice = {
        "id": notice_id,
        "title": f"Notice {notice_id}",
        "url": f"https://www.steg.com.tn/fr/news/{notice_id}",
        "notice_date": date,
        "zones": [],
        "subregions": [
            {"name": subregion, "zones": ["قليبية", "حمام لغزاز"]}
        ],
        "raw_text": "قليبية حمام لغزاز",
        "raw_html": f"<html>{notice_id}</html>",
    }
    return import_official.process_notice(
        notice, "2026-07-26T10:00:00+00:00"
    )


def test_edge_evidence_returns_supporting_notices_newest_first():
    changed = _seed_notice("notice-1", "20/07/2026", "جهة الوطن القبلي")
    changed = _seed_notice("notice-2", "23/07/2026", "جهة الوطن القبلي") or changed
    evidence_pipeline.build_model_evidence(
        created_at="2026-07-26T10:10:00+00:00"
    )
    client = TestClient(main.app)

    response = client.get(
        "/api/edge-evidence",
        params={"locality_a": "حمام لغزاز", "locality_b": "قليبية"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["locality_a"] == "حمام لغزاز"
    assert body["locality_b"] == "قليبية"
    assert body["distinct_notice_count"] == 2
    assert body["distinct_outage_dates"] == 2
    assert [n["notice_id"] for n in body["notices"]] == [
        "notice-2",
        "notice-1",
    ]
    assert "raw_html" not in response.text
    assert body["active_build_id"] == db.active_build_id()


def test_edge_evidence_rejects_self_edge():
    client = TestClient(main.app)

    response = client.get(
        "/api/edge-evidence",
        params={"locality_a": "قليبية", "locality_b": "قليبية"},
    )

    assert response.status_code == 422


def test_edge_evidence_returns_404_when_edge_is_not_active():
    client = TestClient(main.app)

    response = client.get(
        "/api/edge-evidence",
        params={"locality_a": "A", "locality_b": "B"},
    )

    assert response.status_code == 404

