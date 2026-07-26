# tests/test_steg_scraper.py
from bs4 import BeautifulSoup

from app import steg_scraper


def _cell_body(cell_html: str):
    """Wrap one <td> in the minimal structure _extract_zones expects
    (it looks for the first <table> inside the given element)."""
    return BeautifulSoup(f"<div><table><tr>{cell_html}</tr></table></div>", "html.parser")


def test_extract_zones_finds_header_when_bolded_correctly():
    # The one column that worked correctly in the wild: header is bolded,
    # towns are not.
    body = _cell_body(
        "<td><strong>جهة الوطن القبلي</strong><br>قليبية<br>حمام لغزاز</td>"
    )
    zones, subregions = steg_scraper._extract_zones(body)
    assert subregions == [{"name": "جهة الوطن القبلي", "zones": ["قليبية", "حمام لغزاز"]}]
    assert zones == ["قليبية", "حمام لغزاز"]


def test_extract_zones_finds_header_even_when_a_town_is_bolded_instead():
    # The bug: header is plain text, but the first town happens to be
    # bolded (STEG's inconsistent formatting). Must not treat the bolded
    # town as the header, and must not drop the real header from the cell.
    body = _cell_body(
        "<td>جهة بنزرت<br><strong>سجنان</strong><br>جومين<br>الماتلين</td>"
    )
    zones, subregions = steg_scraper._extract_zones(body)
    assert subregions == [{"name": "جهة بنزرت", "zones": ["سجنان", "جومين", "الماتلين"]}]
    assert zones == ["سجنان", "جومين", "الماتلين"]


def test_extract_zones_headerless_cell_falls_back_to_all_lines_as_zones():
    # No line matches the جهة/ولاية pattern -- preserve today's fallback
    # behavior (no header, everything is a zone).
    body = _cell_body("<td>قليبية<br>حمام لغزاز</td>")
    zones, subregions = steg_scraper._extract_zones(body)
    assert subregions == [{"name": None, "zones": ["قليبية", "حمام لغزاز"]}]
    assert zones == ["قليبية", "حمام لغزاز"]


def test_parse_notice_detail_extracts_title_from_page_title_tag(monkeypatch):
    html = """
    <html><head><title>إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 20/07/2026 | Société Tunisienne de l'Electricité et du Gaz</title></head>
    <body><div class="field-name-body"><div class="field-item">
      خلال الفترة بين 11:00 صباحا على مستوى المناطق التالية:
      <ul><li>Dekka</li></ul>
    </div></div></body></html>
    """
    monkeypatch.setattr(steg_scraper, "fetch", lambda url: BeautifulSoup(html, "html.parser"))

    detail = steg_scraper.parse_notice_detail("http://example.test/notice")

    assert detail["title"] == "إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 20/07/2026"
