from pathlib import Path


def test_daily_workflow_gates_recluster_on_readiness():
    workflow_path = Path(".github/workflows/scrape.yml")
    raw = workflow_path.read_text()
    assert raw.count('cron: "0 2 * * *"') == 1
    assert raw.count("cron:") == 1
    assert "/api/model-readiness" in raw
    assert "/api/internal/recluster" in raw
    assert raw.index("/api/model-readiness") < raw.index(
        "/api/internal/recluster"
    )
    assert "X-Request-ID" in raw
