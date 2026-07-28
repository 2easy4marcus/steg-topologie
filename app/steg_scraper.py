"""
Parsing logic for STEG's official electricity-outage notices (steg.com.tn).

This is the pure scraping/parsing half -- see import_official.py for the
piece that upserts results into the SQLite database used by the website.

STEG has no public API. Outage notices ("إشعار بانقطاع الكهرباء") are
published as regular Drupal news items on the homepage's "à la une" section.
Two layouts have been observed for the impacted-zones list:
  1. A plain <ul><li> list (single-region notices).
  2. A <table> with one column per sub-region, each cell holding a bold
     sub-region header followed by dash-prefixed localities (multi-region
     notices, e.g. "جهة الشمال" covering several governorates at once).
Both are handled below.
"""

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.steg.com.tn"
HOMEPAGE_URL = f"{BASE_URL}/fr"
PARSER_VERSION = "2"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# STEG's site is a small, occasionally slow/flaky government site -- reads
# timing out isn't unusual, especially during high-traffic periods (e.g. when
# there actually are outages being announced). Retry with backoff rather than
# failing on the first hiccup.
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 45
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3

NOTICE_TITLE_MARKER = "إشعار بانقطاع الكهرباء"

TITLE_RE = re.compile(
    r"إشعار بانقطاع الكهرباء\s*-\s*(?P<region>.+?)\s*-\s*(?P<time>\d{1,2}:\d{2})\s+(?P<date>\d{2}/\d{2}/\d{4})"
)

SUBREGION_HEADER_RE = re.compile(r"^(?:جهة|ولاية)\s+\S")


class FetchError(RuntimeError):
    """Raised when a URL can't be fetched after all retries are exhausted."""


def slugify_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def fetch(url: str) -> BeautifulSoup:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=HEADERS, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            )
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"  ! {url} failed ({exc.__class__.__name__}), "
                      f"retrying in {wait}s ({attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
    raise FetchError(
        f"Could not reach {url} after {MAX_RETRIES} attempts. "
        f"STEG's site may be temporarily slow/down, or a network in between "
        f"(VPN/proxy/firewall) is interfering. Last error: {last_exc}"
    ) from last_exc


def list_homepage_notices():
    """Return [{title, url}] for outage notices currently on the homepage."""
    soup = fetch(HOMEPAGE_URL)
    notices = []
    for panel in soup.select(".panel"):
        toggle = panel.select_one("a.accordion-toggle")
        if not toggle:
            continue
        title = toggle.get_text(strip=True)
        if NOTICE_TITLE_MARKER not in title:
            continue
        link = panel.select_one('a[href*="/fr/news/"]')
        if not link or not link.get("href"):
            continue
        notices.append({"title": title, "url": urljoin(BASE_URL, link["href"])})
    return notices


def _clean_zone_line(line: str) -> str:
    return re.sub(r"^[\s\-–—ـ]+", "", line).strip()


def _extract_zones(body) -> tuple:
    subregions = []
    table = body.select_one("table")
    if table:
        for cell in table.select("td"):
            lines = [
                _clean_zone_line(line)
                for line in cell.get_text("\n", strip=True).split("\n")
            ]
            lines = [l for l in lines if l]
            header = None
            if lines and SUBREGION_HEADER_RE.match(lines[0]):
                header = lines[0]
                lines = lines[1:]
            if header or lines:
                subregions.append({"name": header, "zones": lines})
        flat = [z for sub in subregions for z in sub["zones"]]
        if flat:
            return flat, subregions

    zones = [li.get_text(strip=True) for li in body.select("li") if li.get_text(strip=True)]
    return zones, subregions


def _page_title(soup) -> str:
    """The node's own title, as rendered in <title>. Drupal 7 (confirmed
    live against this site) renders content pages as
    "{node title} | {site name}" -- split off the site-name suffix."""
    tag = soup.select_one("title")
    if not tag:
        return ""
    return tag.get_text(strip=True).split(" | ")[0].strip()


def parse_notice_detail(url: str) -> dict:
    soup = fetch(url)
    title = _page_title(soup)
    body = soup.select_one(".field-name-body .field-item")
    if not body:
        return {
            "title": title,
            "raw_text": None,
            "raw_html": str(soup),
            "zones": [],
            "subregions": [],
            "time_window_sentence": None,
        }

    zones, subregions = _extract_zones(body)
    raw_text = body.get_text("\n", strip=True)

    time_window_sentence = None
    m = re.search(r"خلال\s+(.+?)(?:،|,)?\s*على مستوى المناطق التالية", raw_text, re.DOTALL)
    if m:
        time_window_sentence = m.group(1).strip()

    return {
        "title": title,
        "raw_text": raw_text,
        "raw_html": str(soup),
        "zones": zones,
        "subregions": subregions,
        "time_window_sentence": time_window_sentence,
    }


def scrape_current_notices() -> list:
    """Fetch the homepage + every linked notice detail page. Returns full records."""
    results = []
    for n in list_homepage_notices():
        m = TITLE_RE.search(n["title"])
        detail = parse_notice_detail(n["url"])
        results.append({
            "id": slugify_id(n["url"]),
            "title": n["title"],
            "url": n["url"],
            "region": m.group("region").strip() if m else None,
            "notice_date": m.group("date") if m else None,
            "notice_time": m.group("time") if m else None,
            "time_window_sentence": detail["time_window_sentence"],
            "zones": detail["zones"],
            "subregions": detail["subregions"],
            "raw_text": detail["raw_text"],
            "raw_html": detail["raw_html"],
        })
    return results
