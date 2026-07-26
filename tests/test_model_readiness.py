from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import main, model_readiness


def _passing_metrics():
    return {
        "valid_notices": 30,
        "distinct_outage_dates": 15,
        "unique_localities": 10,
        "repeated_pairs": 20,
        "active_ok_ratio": 0.80,
        "largest_notice_pair_share": 0.20,
    }


def test_exact_model_quality_thresholds_pass(monkeypatch):
    monkeypatch.setattr(
        model_readiness.db,
        "model_readiness_metrics",
        lambda build_id: _passing_metrics(),
    )
    monkeypatch.setattr(
        model_readiness.db,
        "operational_health_metrics",
        lambda cutoff: {
            "recent_parse_success_ratio": 0.80,
            "last_successful_scrape_at": "2026-07-26T10:00:00+00:00",
        },
    )

    result = model_readiness.evaluate(
        now=datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
        build_id="build-1",
    )

    assert result.model_quality.ready is True
    assert result.operational_health.ready is True


def test_each_model_quality_signal_can_block(monkeypatch):
    for key in _passing_metrics():
        metrics = _passing_metrics()
        metrics[key] = (
            metrics[key] + 0.01
            if key == "largest_notice_pair_share"
            else metrics[key] - 0.01
        )
        monkeypatch.setattr(
            model_readiness.db,
            "model_readiness_metrics",
            lambda build_id, metrics=metrics: metrics,
        )
        monkeypatch.setattr(
            model_readiness.db,
            "operational_health_metrics",
            lambda cutoff: {
                "recent_parse_success_ratio": 1.0,
                "last_successful_scrape_at": "2026-07-26T10:00:00+00:00",
            },
        )

        result = model_readiness.evaluate(
            now=datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
            build_id="build-1",
        )

        assert result.model_quality.ready is False, key


def test_failed_recent_parses_remain_in_operational_health(monkeypatch):
    monkeypatch.setattr(
        model_readiness.db,
        "model_readiness_metrics",
        lambda build_id: _passing_metrics(),
    )
    monkeypatch.setattr(
        model_readiness.db,
        "operational_health_metrics",
        lambda cutoff: {
            "recent_parse_success_ratio": 0.79,
            "last_successful_scrape_at": "2026-07-26T10:00:00+00:00",
        },
    )

    result = model_readiness.evaluate(
        now=datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
        build_id="build-1",
    )

    assert result.model_quality.ready is True
    assert result.operational_health.ready is False


def test_model_readiness_endpoint_returns_both_sections(monkeypatch):
    report = model_readiness.empty_readiness()
    monkeypatch.setattr(
        main.model_readiness,
        "evaluate",
        lambda: report,
    )
    client = TestClient(main.app)

    response = client.get("/api/model-readiness")

    assert response.status_code == 200
    assert set(response.json()) >= {
        "build_id",
        "model_quality",
        "operational_health",
    }
