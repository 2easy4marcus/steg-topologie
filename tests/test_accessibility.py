from pathlib import Path

from bs4 import BeautifulSoup
import pytest


def test_arabic_capable_dynamic_fields_use_auto_direction():
    soup = BeautifulSoup(Path("static/model.html").read_text(), "html.parser")

    assert soup.select('[dir="auto"]')


def test_graph_has_text_alternative_reference():
    soup = BeautifulSoup(Path("static/model.html").read_text(), "html.parser")
    graph = soup.select_one("#graph")

    assert graph["aria-describedby"] == "edge-table-description"
    assert soup.select_one("#edge-table-description")


def test_reduced_motion_and_visible_focus_are_defined():
    html = Path("static/model.html").read_text()

    assert "@media (prefers-reduced-motion: reduce)" in html
    assert ":focus-visible" in html


def test_rendered_mobile_page_has_accessible_table_without_page_overflow():
    playwright = pytest.importorskip("playwright.sync_api")
    html = Path("static/model.html").read_text()

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"headless browser unavailable in sandbox: {exc}")
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            java_script_enabled=False,
            reduced_motion="reduce",
        )
        page = context.new_page()
        page.set_content(html)

        assert page.get_by_role("table").count() == 1
        assert page.locator('[aria-live="polite"]').count() >= 1
        dimensions = page.evaluate(
            "() => ({scroll: document.documentElement.scrollWidth, "
            "client: document.documentElement.clientWidth})"
        )
        assert dimensions["scroll"] <= dimensions["client"]
        browser.close()
