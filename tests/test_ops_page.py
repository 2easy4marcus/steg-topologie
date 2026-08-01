from pathlib import Path

from bs4 import BeautifulSoup


OPS_PAGE = Path("static/ops.html")


def test_ops_page_has_accessible_status_regions_and_six_cards():
    soup = BeautifulSoup(OPS_PAGE.read_text(), "html.parser")

    assert soup.html["lang"] == "fr"
    assert soup.find("main")
    assert soup.find(attrs={"aria-live": "polite"})
    assert len(soup.select("[data-status-card]")) == 6
    headings = {node.get_text(" ", strip=True) for node in soup.select("h2")}
    assert {
        "Dernière collecte",
        "Backfill en cours",
        "Build de preuves actif",
        "Dernier clustering",
        "Préparation du modèle",
        "Santé opérationnelle",
    } <= headings


def test_ops_page_uses_only_public_status_endpoints_and_adaptive_polling():
    html = OPS_PAGE.read_text()

    assert 'fetch("/api/status")' in html
    assert 'fetch("/api/status/ingestion")' in html
    # The only protected endpoint the page may reference is the operator
    # diagnostics summary, and only guarded by a session-held secret sent as a
    # header -- never baked into the page.
    assert "/api/internal/" not in html.replace(
        "/api/internal/ops/summary", ""
    )
    assert "sessionStorage" in html
    assert "OPS_SECRET" not in html
    assert "X-Ops-Secret" not in html
    assert "5000" in html
    assert "60000" in html


def test_ops_page_supports_loading_failure_stale_and_request_id_states():
    html = OPS_PAGE.read_text()

    assert 'data-state="loading"' in html
    assert "failed" in html
    assert "stale" in html
    assert "request_id" in html
    assert "dir=\"auto\"" in html


def test_all_public_pages_link_to_tracker_model_and_pipeline_status():
    for page in ("static/index.html", "static/model.html", "static/ops.html"):
        soup = BeautifulSoup(Path(page).read_text(), "html.parser")
        targets = {link.get("href") for link in soup.select("nav a")}
        assert {"/", "/model.html", "/ops.html"} <= targets


def test_ops_page_has_mobile_focus_and_reduced_motion_support():
    html = OPS_PAGE.read_text()

    assert "@media (max-width:" in html
    assert ":focus-visible" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
