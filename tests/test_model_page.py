from pathlib import Path

from bs4 import BeautifulSoup


MODEL_PAGE = Path("static/model.html")


def test_model_page_has_readiness_metadata_and_accessible_edge_table():
    soup = BeautifulSoup(MODEL_PAGE.read_text(), "html.parser")

    assert soup.select_one("#model-readiness")
    assert soup.select_one("#model-metadata")
    table = soup.select_one("#edge-table")
    assert table is not None
    headings = [th.get_text(" ", strip=True) for th in table.select("th")]
    assert headings == [
        "Localité A",
        "Localité B",
        "Avis",
        "Dates distinctes",
        "Première observation",
        "Dernière observation",
        "Cluster",
        "Preuves",
    ]


def test_model_page_has_shared_filter_and_evidence_panel():
    html = MODEL_PAGE.read_text()
    soup = BeautifulSoup(html, "html.parser")

    assert soup.select_one("#edge-filter")
    assert soup.select_one("#evidence-panel")
    assert "/api/edge-evidence" in html
    assert "applyEdgeFilter" in html


def test_model_page_contains_required_disclaimer_and_live_region():
    soup = BeautifulSoup(MODEL_PAGE.read_text(), "html.parser")

    assert "not a confirmed transformer" in soup.get_text(" ", strip=True)
    assert soup.select_one('[aria-live="polite"]')

