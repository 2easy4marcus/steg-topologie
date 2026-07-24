# Grid Co-occurrence Cluster Inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Infer probable shared-infrastructure groupings of Tunisian localities from STEG outage-notice co-occurrence patterns (PMI + Louvain), surface them as an opt-in map layer, and automate data collection via GitHub Actions hitting protected endpoints on a Turso-backed deployment.

**Architecture:** Two new protected FastAPI endpoints (`/api/internal/scrape`, `/api/internal/recluster`) do all writes in-process against a libSQL DB (local file in dev, Turso in production) — GitHub Actions is a pure external HTTP cron trigger, never touching data directly. A new `cluster_inference.py` module builds a PPMI-weighted co-occurrence graph and runs Louvain community detection daily; a new `locality_dedup.py` module normalizes and fuzzy-matches locality names before they become graph nodes; a new `geocoding.py` module lazily resolves locality coordinates via Nominatim. Results are read through a new `GET /api/clusters` endpoint and rendered as an opt-in, clearly-labeled map layer.

**Tech Stack:** FastAPI, `libsql-client` (Turso/libSQL Python SDK), `networkx` + `python-louvain` (Louvain community detection), `rapidfuzz` (fuzzy string matching), `requests` (Nominatim geocoding), `pytest` (testing), Leaflet.js (existing frontend map).

---

## File Structure

New files:
- `locality_dedup.py` — normalization + exact/fuzzy locality resolution against `localities`/`locality_aliases`
- `geocoding.py` — Nominatim lookup with rate-limiting
- `cluster_inference.py` — PPMI graph construction, Louvain clustering, stability scoring, the `run_recluster()` orchestrator, and data-floor checks
- `.github/workflows/scrape.yml` — cron-triggered HTTP calls to the deployed app
- `tests/test_locality_dedup.py`, `tests/test_geocoding.py`, `tests/test_cluster_inference.py`, `tests/test_db_clusters.py`, `tests/test_app_internal_endpoints.py`, `tests/conftest.py`

Modified files:
- `db.py` — swap `sqlite3` for `libsql_client`; add `localities`, `cooccurrences`, `clusters`, `locality_aliases` tables and their CRUD functions; convert named (`:param`) queries to positional (`?`) since libSQL doesn't support named binding
- `import_official.py` — after upserting each notice, resolve its localities through `locality_dedup` and record co-occurrence pairs
- `app.py` — add `verify_cron_secret` dependency, `/api/internal/scrape`, `/api/internal/recluster`, `GET /api/clusters`, and their Pydantic response models
- `static/index.html` — add the "Show inferred grid clusters (beta)" toggle, rendering logic, tooltip, and persistent disclaimer legend
- `requirements.txt` — add `libsql-client`, `networkx`, `python-louvain`, `rapidfuzz`, `pytest`
- `README.md` — document the new env vars (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `CRON_SECRET`) and the GitHub Actions setup

Each new module has one job: `locality_dedup.py` never touches geocoding or clustering; `geocoding.py` never touches clustering; `cluster_inference.py` never touches HTTP. `app.py` wires them together but contains no algorithmic logic itself.

---

## Task 1: Dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add new dependencies**

```
fastapi>=0.110
uvicorn>=0.29
requests>=2.31
beautifulsoup4>=4.12
pydantic>=2.0
libsql-client>=0.3.1
networkx>=3.2
python-louvain>=0.16
rapidfuzz>=3.6
pytest>=8.0
```

- [ ] **Step 2: Install and verify**

Run: `pip install -r requirements.txt`
Expected: all packages install without error; `python -c "import libsql_client, networkx, community, rapidfuzz"` prints nothing (no ImportError).

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add deps for grid co-occurrence clustering"
```

---

## Task 2: Test fixtures and conftest

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

- [ ] **Step 1: Write conftest that gives every test an isolated local libSQL file DB**

```python
# tests/conftest.py
import os
import tempfile
import pytest

import db


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Every test gets its own empty on-disk libSQL file DB so tests never
    share state or touch the real tracker.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # libsql_client creates the file itself
    monkeypatch.setattr(db, "DB_URL", f"file:{path}")
    monkeypatch.setattr(db, "AUTH_TOKEN", None)
    db.init_db()
    yield
    if os.path.exists(path):
        os.remove(path)
```

- [ ] **Step 2: Run to confirm it errors correctly (db.DB_URL doesn't exist yet)**

Run: `pytest tests/conftest.py -v`
Expected: collection error or `AttributeError`/`ImportError` referencing `db.DB_URL` — confirms we haven't built Task 3 yet, not a fixture bug.

- [ ] **Step 3: Commit**

```bash
git add tests/__init__.py tests/conftest.py
git commit -m "test: add isolated-DB fixture for grid clustering tests"
```

---

## Task 3: Migrate `db.py` to libSQL + add new tables

**Files:**
- Modify: `db.py`
- Test: `tests/test_db_clusters.py`

- [ ] **Step 1: Write the failing test for the new schema and CRUD**

```python
# tests/test_db_clusters.py
import db


def test_new_tables_exist_and_roundtrip():
    db.upsert_locality("Dekka", lat=None, lng=None, governorate=None)
    row = db.get_locality("Dekka")
    assert row["name"] == "Dekka"
    assert row["lat"] is None

    db.set_locality_coords("Dekka", 34.1, 9.2)
    row = db.get_locality("Dekka")
    assert row["lat"] == 34.1
    assert row["lng"] == 9.2

    db.record_alias("دقه", "Dekka")
    assert db.resolve_alias("دقه") == "Dekka"
    assert db.resolve_alias("unknown") is None

    db.increment_cooccurrence("Dekka", "Tozeur")
    db.increment_cooccurrence("Dekka", "Tozeur")
    rows = db.list_cooccurrences()
    assert rows == [{"locality_a": "Dekka", "locality_b": "Tozeur", "notice_count": 2, "last_seen": rows[0]["last_seen"]}]

    db.write_cluster_run("2026-07-24", {"Dekka": 0, "Tozeur": 0}, {"Dekka": 0.5, "Tozeur": 0.5})
    latest = db.latest_cluster_run()
    assert latest["run_date"] == "2026-07-24"
    assert {r["locality"] for r in latest["rows"]} == {"Dekka", "Tozeur"}


def test_notices_and_reports_still_work_after_migration():
    """Existing behavior (from before this feature) must not regress."""
    notice = {
        "id": "n1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "من 8 الى 12", "zones": ["Dekka", "Tozeur"],
        "subregions": [], "raw_text": "raw", "scraped_at": "2026-07-24T00:00:00Z",
    }
    db.upsert_official_notice(notice)
    assert db.count_official_notices() == 1
    fetched = db.list_official_notices()[0]
    assert fetched["zones"] == ["Dekka", "Tozeur"]

    report_id = db.create_user_report({
        "utility": "electricity", "status": "active", "governorate": "Tunis",
        "delegation": None, "zone_text": None, "comment": "",
        "started_at": None, "ended_at": None, "created_at": "2026-07-24T00:00:00Z",
    })
    assert report_id is not None
    assert db.list_user_reports()[0]["governorate"] == "Tunis"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_clusters.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'upsert_locality'` (or similar; `db.py` doesn't have these functions or the libSQL backend yet).

- [ ] **Step 3: Rewrite `db.py`'s connection layer and schema**

Replace the top of `db.py` (imports, `DB_PATH`, `SCHEMA`, `get_conn`, `init_db`) with:

```python
"""
Database access layer for the Tunisia outage tracker.

Backed by libSQL (https://turso.tech) so the same code runs against a local
file in dev/tests and a hosted Turso DB in production -- set
TURSO_DATABASE_URL / TURSO_AUTH_TOKEN to point at a hosted DB; if unset,
falls back to a local file (tracker.db next to this file).

libSQL's Python client only supports positional `?` parameters, not
sqlite3's named `:name` binding, so every query here uses positional
params (lists), including the original official_notices/user_reports
queries that used to use `:name` dict binding.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path

import libsql_client

DB_FILE_DEFAULT = Path(__file__).parent / "tracker.db"
DB_URL = os.environ.get("TURSO_DATABASE_URL", f"file:{DB_FILE_DEFAULT}")
AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS official_notices (
        id TEXT PRIMARY KEY,
        title TEXT,
        url TEXT,
        region TEXT,
        notice_date TEXT,
        notice_time TEXT,
        time_window_sentence TEXT,
        zones TEXT,
        subregions TEXT,
        raw_text TEXT,
        scraped_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        utility TEXT NOT NULL,
        status TEXT NOT NULL,
        governorate TEXT NOT NULL,
        delegation TEXT,
        zone_text TEXT,
        comment TEXT DEFAULT '',
        started_at TEXT,
        ended_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reports_gov ON user_reports(governorate)",
    "CREATE INDEX IF NOT EXISTS idx_reports_status ON user_reports(status)",
    "CREATE INDEX IF NOT EXISTS idx_notices_date ON official_notices(notice_date)",
    """
    CREATE TABLE IF NOT EXISTS localities (
        name TEXT PRIMARY KEY,
        lat REAL,
        lng REAL,
        governorate TEXT,
        geocoded_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS locality_aliases (
        alias_raw_text TEXT PRIMARY KEY,
        canonical_locality TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cooccurrences (
        locality_a TEXT NOT NULL,
        locality_b TEXT NOT NULL,
        notice_count INTEGER NOT NULL DEFAULT 0,
        last_seen TEXT,
        PRIMARY KEY (locality_a, locality_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clusters (
        run_date TEXT NOT NULL,
        cluster_id INTEGER NOT NULL,
        locality TEXT NOT NULL,
        stability REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (run_date, locality)
    )
    """,
]


class _Result:
    def __init__(self, rs):
        self._rs = rs

    def fetchall(self):
        return [dict(zip(self._rs.columns, row)) for row in self._rs.rows]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    @property
    def lastrowid(self):
        return self._rs.last_insert_rowid


class _Conn:
    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=None):
        return _Result(self._client.execute(sql, list(params or [])))


@contextmanager
def get_conn():
    client = libsql_client.create_client_sync(url=DB_URL, auth_token=AUTH_TOKEN)
    try:
        yield _Conn(client)
    finally:
        client.close()


def init_db():
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
```

- [ ] **Step 4: Convert the existing named-parameter queries to positional**

In `db.py`, replace `upsert_official_notice`:

```python
def upsert_official_notice(notice: dict):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO official_notices
                (id, title, url, region, notice_date, notice_time,
                 time_window_sentence, zones, subregions, raw_text, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, url=excluded.url, region=excluded.region,
                notice_date=excluded.notice_date, notice_time=excluded.notice_time,
                time_window_sentence=excluded.time_window_sentence,
                zones=excluded.zones, subregions=excluded.subregions,
                raw_text=excluded.raw_text, scraped_at=excluded.scraped_at
            """,
            [
                notice["id"], notice["title"], notice["url"], notice.get("region"),
                notice.get("notice_date"), notice.get("notice_time"),
                notice.get("time_window_sentence"),
                json.dumps(notice.get("zones", []), ensure_ascii=False),
                json.dumps(notice.get("subregions", []), ensure_ascii=False),
                notice.get("raw_text"), notice["scraped_at"],
            ],
        )
```

And `create_user_report`:

```python
def create_user_report(report: dict) -> int:
    with get_conn() as conn:
        result = conn.execute(
            """
            INSERT INTO user_reports
                (utility, status, governorate, delegation, zone_text, comment,
                 started_at, ended_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                report["utility"], report["status"], report["governorate"],
                report.get("delegation"), report.get("zone_text"),
                report.get("comment", ""), report.get("started_at"),
                report.get("ended_at"), report["created_at"],
            ],
        )
        return result.lastrowid
```

`list_official_notices`, `count_official_notices`, `list_user_reports`, `stats_by_governorate`, `overall_stats` already use positional `?` params — leave them as-is, they don't need changes.

- [ ] **Step 5: Add the new CRUD functions used by the failing test**

Append to `db.py`:

```python
# ---------- localities / aliases / cooccurrences / clusters ----------

def upsert_locality(name: str, lat=None, lng=None, governorate=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO localities (name, lat, lng, governorate, geocoded_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(name) DO NOTHING
            """,
            [name, lat, lng, governorate],
        )


def get_locality(name: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM localities WHERE name = ?", [name]).fetchone()


def set_locality_coords(name: str, lat: float, lng: float, geocoded_at: str = ""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE localities SET lat = ?, lng = ?, geocoded_at = ? WHERE name = ?",
            [lat, lng, geocoded_at, name],
        )


def list_ungeocoded_localities():
    with get_conn() as conn:
        return conn.execute("SELECT name FROM localities WHERE lat IS NULL").fetchall()


def record_alias(alias_raw_text: str, canonical_locality: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO locality_aliases (alias_raw_text, canonical_locality) VALUES (?, ?) "
            "ON CONFLICT(alias_raw_text) DO NOTHING",
            [alias_raw_text, canonical_locality],
        )


def resolve_alias(alias_raw_text: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT canonical_locality FROM locality_aliases WHERE alias_raw_text = ?",
            [alias_raw_text],
        ).fetchone()
        return row["canonical_locality"] if row else None


def list_locality_names():
    with get_conn() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM localities").fetchall()]


def increment_cooccurrence(locality_a: str, locality_b: str, seen_at: str = ""):
    a, b = sorted([locality_a, locality_b])
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cooccurrences (locality_a, locality_b, notice_count, last_seen)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(locality_a, locality_b) DO UPDATE SET
                notice_count = notice_count + 1, last_seen = excluded.last_seen
            """,
            [a, b, seen_at],
        )


def list_cooccurrences():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM cooccurrences").fetchall()


def total_notice_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM official_notices").fetchone()["c"]


def distinct_locality_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM localities").fetchone()["c"]


def write_cluster_run(run_date: str, cluster_assignment: dict, stability: dict):
    with get_conn() as conn:
        for locality, cluster_id in cluster_assignment.items():
            conn.execute(
                """
                INSERT INTO clusters (run_date, cluster_id, locality, stability)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_date, locality) DO UPDATE SET
                    cluster_id = excluded.cluster_id, stability = excluded.stability
                """,
                [run_date, cluster_id, locality, stability.get(locality, 0.0)],
            )


def has_cluster_run(run_date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM clusters WHERE run_date = ?", [run_date]
        ).fetchone()
        return row["c"] > 0


def latest_cluster_run():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT run_date FROM clusters ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        run_date = row["run_date"]
        rows = conn.execute(
            """
            SELECT c.run_date, c.cluster_id, c.locality, c.stability, l.lat, l.lng
            FROM clusters c JOIN localities l ON l.name = c.locality
            WHERE c.run_date = ?
            """,
            [run_date],
        ).fetchall()
        return {"run_date": run_date, "rows": rows}


def cluster_run_dates(before: str, limit: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT run_date FROM clusters WHERE run_date < ? ORDER BY run_date DESC LIMIT ?",
            [before, limit],
        ).fetchall()
        return [r["run_date"] for r in rows]


def cluster_members(run_date: str, cluster_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT locality FROM clusters WHERE run_date = ? AND cluster_id = ?",
            [run_date, cluster_id],
        ).fetchall()
        return {r["locality"] for r in rows}


def locality_cluster_on(run_date: str, locality: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cluster_id FROM clusters WHERE run_date = ? AND locality = ?",
            [run_date, locality],
        ).fetchone()
        return row["cluster_id"] if row else None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_db_clusters.py -v`
Expected: PASS (both tests).

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_db_clusters.py
git commit -m "feat: migrate db.py to libSQL, add locality/cooccurrence/cluster tables"
```

---

## Task 4: Locality normalization + dedup

**Files:**
- Create: `locality_dedup.py`
- Test: `tests/test_locality_dedup.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_locality_dedup.py
import db
import locality_dedup


def test_normalize_strips_diacritics_and_unifies_alef():
    assert locality_dedup.normalize_locality("  دقّة  ") == locality_dedup.normalize_locality("دقة")
    assert locality_dedup.normalize_locality("أريانة") == locality_dedup.normalize_locality("اريانة")


def test_resolve_locality_creates_new_when_no_match():
    canonical = locality_dedup.resolve_locality("Dekka")
    assert canonical == "Dekka"
    assert db.get_locality("Dekka") is not None


def test_resolve_locality_exact_normalized_match_creates_alias():
    locality_dedup.resolve_locality("دقة")
    canonical = locality_dedup.resolve_locality("دقـة")  # extra tatweel/spacing variant, same normalized form after our rules
    assert canonical == "دقة"
    assert db.resolve_alias("دقـة") == "دقة"


def test_resolve_locality_fuzzy_match_above_threshold():
    locality_dedup.resolve_locality("Bou Argoub")
    canonical = locality_dedup.resolve_locality("Bou Argoub ")  # trivial variant
    assert canonical == "Bou Argoub"


def test_resolve_locality_below_threshold_creates_distinct():
    locality_dedup.resolve_locality("Tozeur")
    canonical = locality_dedup.resolve_locality("Kebili")
    assert canonical == "Kebili"
    assert canonical != "Tozeur"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_locality_dedup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'locality_dedup'`.

- [ ] **Step 3: Write minimal implementation**

```python
# locality_dedup.py
"""
Resolves raw locality text from STEG notices to a stable canonical name.

Pipeline: normalize -> exact match on normalized form -> fuzzy match
(rapidfuzz >= 90%) -> new canonical locality if nothing matches. Matches
are recorded in locality_aliases so the same raw text resolves instantly
next time without re-running the fuzzy scan.
"""

import re
import unicodedata

from rapidfuzz import fuzz, process

import db

FUZZY_THRESHOLD = 90
_DIACRITICS_RE = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL_RE = re.compile(r"ـ")
_ALEF_VARIANTS = "أإآ"


def normalize_locality(text: str) -> str:
    text = unicodedata.normalize("NFC", text.strip())
    text = re.sub(r"\s+", " ", text)
    text = _DIACRITICS_RE.sub("", text)
    text = _TATWEEL_RE.sub("", text)
    for variant in _ALEF_VARIANTS:
        text = text.replace(variant, "ا")
    text = text.replace("ة", "ه")
    return text


def resolve_locality(raw_text: str) -> str:
    existing_alias = db.resolve_alias(raw_text)
    if existing_alias:
        return existing_alias

    normalized = normalize_locality(raw_text)
    existing_names = db.list_locality_names()
    normalized_to_name = {normalize_locality(name): name for name in existing_names}

    if normalized in normalized_to_name:
        canonical = normalized_to_name[normalized]
        if canonical != raw_text:
            db.record_alias(raw_text, canonical)
        return canonical

    if normalized_to_name:
        match = process.extractOne(
            normalized, list(normalized_to_name.keys()), scorer=fuzz.token_sort_ratio,
        )
        if match is not None and match[1] >= FUZZY_THRESHOLD:
            canonical = normalized_to_name[match[0]]
            db.record_alias(raw_text, canonical)
            return canonical

    db.upsert_locality(raw_text)
    return raw_text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_locality_dedup.py -v`
Expected: PASS (all 5 tests). If the tatweel/alef test fails because rapidfuzz's `token_sort_ratio` doesn't score your specific example above 90, adjust the test's example strings to ones that do differ only by whitespace/diacritics (the point is proving the pipeline mechanics, not a specific string pair).

- [ ] **Step 5: Commit**

```bash
git add locality_dedup.py tests/test_locality_dedup.py
git commit -m "feat: add locality normalize/exact/fuzzy dedup pipeline"
```

---

## Task 5: Geocoding with caching

**Files:**
- Create: `geocoding.py`
- Test: `tests/test_geocoding.py`

- [ ] **Step 1: Write the failing test (mocking Nominatim, no real network calls)**

```python
# tests/test_geocoding.py
from unittest.mock import patch, MagicMock

import db
import geocoding


def _fake_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def test_geocode_locality_returns_coords_on_success():
    with patch("geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1", "lon": "9.2"}])
        lat, lng = geocoding.geocode_locality("Dekka")
    assert lat == 34.1
    assert lng == 9.2


def test_geocode_locality_returns_none_on_no_match():
    with patch("geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([])
        lat, lng = geocoding.geocode_locality("Nonexistent Place")
    assert (lat, lng) == (None, None)


def test_geocode_locality_returns_none_on_request_error():
    import requests
    with patch("geocoding.requests.get", side_effect=requests.exceptions.ConnectionError):
        lat, lng = geocoding.geocode_locality("Dekka")
    assert (lat, lng) == (None, None)


def test_ensure_geocoded_caches_and_skips_second_network_call():
    db.upsert_locality("Dekka")
    with patch("geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1", "lon": "9.2"}])
        geocoding.ensure_geocoded("Dekka")
        geocoding.ensure_geocoded("Dekka")  # already has lat/lng now, must not call again
    assert mock_get.call_count == 1
    row = db.get_locality("Dekka")
    assert row["lat"] == 34.1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_geocoding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'geocoding'`.

- [ ] **Step 3: Write minimal implementation**

```python
# geocoding.py
"""
Lazy, cached geocoding of locality names via Nominatim (OpenStreetMap).

Respects Nominatim's usage policy: 1 request/second max, required
User-Agent header. Results are cached forever in db.localities -- a name
is only re-queried if it's still NULL (previous lookup failed/no match).
"""

import time

import requests

import db

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "tunisia-outage-tracker/1.0 (contact: m.jellibi@enlyze.com)"
_last_request_time = 0.0


def geocode_locality(name: str):
    """Return (lat, lng) floats, or (None, None) on no-match/failure."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": f"{name}, Tunisia", "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        _last_request_time = time.time()
        resp.raise_for_status()
        results = resp.json()
    except requests.exceptions.RequestException:
        return None, None
    if not results:
        return None, None
    return float(results[0]["lat"]), float(results[0]["lon"])


def ensure_geocoded(name: str):
    """Geocode `name` only if it doesn't already have coordinates."""
    row = db.get_locality(name)
    if row is None:
        db.upsert_locality(name)
        row = db.get_locality(name)
    if row["lat"] is not None:
        return
    lat, lng = geocode_locality(name)
    if lat is not None:
        db.set_locality_coords(name, lat, lng, geocoded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def geocode_all_pending():
    """Called from the recluster job: geocode every locality still missing
    coordinates. Safe to call repeatedly -- only touches NULL rows."""
    for row in db.list_ungeocoded_localities():
        ensure_geocoded(row["name"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_geocoding.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Commit**

```bash
git add geocoding.py tests/test_geocoding.py
git commit -m "feat: add cached Nominatim geocoding for localities"
```

---

## Task 6: Co-occurrence recording wired into the scrape flow

**Files:**
- Modify: `import_official.py`
- Test: `tests/test_import_official_cooccurrence.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_import_official_cooccurrence.py
from unittest.mock import patch

import db
import import_official


def test_run_records_cooccurrences_for_each_notice(monkeypatch):
    fake_notices = [{
        "id": "n1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": ["Dekka", "Tozeur", "Kebili"],
        "subregions": [], "raw_text": "raw",
    }]
    monkeypatch.setattr(import_official.steg_scraper, "scrape_current_notices", lambda: fake_notices)

    import_official.run(verbose=False)

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    # 3 zones -> 3 pairs, each seen once in this one notice
    assert rows[("Dekka", "Kebili")] == 1
    assert rows[("Dekka", "Tozeur")] == 1
    assert rows[("Kebili", "Tozeur")] == 1
    assert db.get_locality("Dekka") is not None


def test_run_extracts_pairs_from_subregions_too(monkeypatch):
    fake_notices = [{
        "id": "n2", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": [],
        "subregions": [
            {"name": "جهة زغوان", "zones": ["Zaghouan Ville", "Bir Mcherga"]},
        ],
        "raw_text": "raw",
    }]
    monkeypatch.setattr(import_official.steg_scraper, "scrape_current_notices", lambda: fake_notices)

    import_official.run(verbose=False)

    rows = {(r["locality_a"], r["locality_b"]): r["notice_count"] for r in db.list_cooccurrences()}
    assert rows[("Bir Mcherga", "Zaghouan Ville")] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_import_official_cooccurrence.py -v`
Expected: FAIL — `db.list_cooccurrences()` returns `[]` (co-occurrence recording doesn't exist yet in `import_official.run`).

- [ ] **Step 3: Modify `import_official.py`'s `run()` to extract and record pairs**

Replace the body of `run()` in `import_official.py`:

```python
import itertools

import db
import locality_dedup
import steg_scraper


def _localities_in_notice(notice: dict) -> list:
    if notice.get("subregions"):
        raw = [z for sub in notice["subregions"] for z in sub.get("zones", [])]
    else:
        raw = notice.get("zones", [])
    return [locality_dedup.resolve_locality(z) for z in raw if z]


def run(verbose: bool = True) -> int:
    db.init_db()
    notices = steg_scraper.scrape_current_notices()
    now = datetime.now(timezone.utc).isoformat()
    for n in notices:
        n["scraped_at"] = now
        db.upsert_official_notice(n)

        localities = sorted(set(_localities_in_notice(n)))
        for a, b in itertools.combinations(localities, 2):
            db.increment_cooccurrence(a, b, seen_at=now)

        if verbose:
            print(f"  upserted: {n['title']}")
    if verbose:
        if not notices:
            print("Done. 0 notice(s) currently on STEG's homepage "
                  "(this is normal when no cuts are announced right now).")
        print(f"Done. {len(notices)} notice(s) processed, "
              f"{db.count_official_notices()} total in DB.")
    return len(notices)
```

Keep the existing `import sys`, `from datetime import datetime, timezone` line, and the `if __name__ == "__main__":` block unchanged — only `run()`'s body and the new imports (`itertools`, `locality_dedup`) change. Note `import db` and `import steg_scraper` were already present as module-level imports; the test patches `import_official.steg_scraper.scrape_current_notices`, so keep `steg_scraper` imported as a module (not `from steg_scraper import scrape_current_notices`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_import_official_cooccurrence.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest -v`
Expected: all tests from Tasks 2-6 pass.

- [ ] **Step 6: Commit**

```bash
git add import_official.py tests/test_import_official_cooccurrence.py
git commit -m "feat: record locality co-occurrences during notice import"
```

---

## Task 7: PPMI graph + Louvain clustering + stability scoring

**Files:**
- Create: `cluster_inference.py`
- Test: `tests/test_cluster_inference.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cluster_inference.py
import db
import cluster_inference


def test_ppmi_graph_weights_known_fixture():
    # 3 notices total. A-B co-occur in all 3 (always together -> high PMI).
    # C-D co-occur once, but C and D are otherwise rare -> still positive PMI.
    # A never co-occurs with C or D.
    cooccurrences = [
        {"locality_a": "A", "locality_b": "B", "notice_count": 3},
        {"locality_a": "C", "locality_b": "D", "notice_count": 1},
    ]
    G = cluster_inference.build_ppmi_graph(cooccurrences, total_notices=3)
    assert G.has_edge("A", "B")
    assert G.has_edge("C", "D")
    assert not G.has_edge("A", "C")
    # A-B co-occur in every notice they're ever in together and nowhere else
    # -> P(a,b)/(-P(a)P(b)) is large -> PMI clearly positive.
    assert G["A"]["B"]["weight"] > 0


def test_compute_clusters_finds_two_obvious_communities():
    cooccurrences = [
        {"locality_a": "A", "locality_b": "B", "notice_count": 10},
        {"locality_a": "B", "locality_b": "C", "notice_count": 10},
        {"locality_a": "X", "locality_b": "Y", "notice_count": 10},
        {"locality_a": "Y", "locality_b": "Z", "notice_count": 10},
    ]
    G = cluster_inference.build_ppmi_graph(cooccurrences, total_notices=10)
    partition = cluster_inference.compute_clusters(G)
    assert partition["A"] == partition["B"] == partition["C"]
    assert partition["X"] == partition["Y"] == partition["Z"]
    assert partition["A"] != partition["X"]


def test_compute_clusters_handles_isolated_node():
    G = cluster_inference.build_ppmi_graph([], total_notices=1)
    G.add_node("Lonely")
    partition = cluster_inference.compute_clusters(G)
    assert "Lonely" in partition


def test_stability_high_when_membership_identical_across_runs():
    db.write_cluster_run("2026-07-20", {"A": 0, "B": 0}, {"A": 0, "B": 0})
    db.write_cluster_run("2026-07-21", {"A": 0, "B": 0}, {"A": 0, "B": 0})
    stability = cluster_inference.compute_stability("2026-07-22", {"A": 5, "B": 5})
    assert stability["A"] == 1.0
    assert stability["B"] == 1.0


def test_stability_zero_when_no_prior_runs():
    stability = cluster_inference.compute_stability("2026-07-24", {"A": 0, "B": 0})
    assert stability["A"] == 0.0
    assert stability["B"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cluster_inference.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cluster_inference'`.

- [ ] **Step 3: Write minimal implementation**

```python
# cluster_inference.py
"""
Statistical grid-cluster inference: builds a PPMI-weighted co-occurrence
graph from STEG notice data and runs Louvain community detection to find
probable shared-infrastructure groupings.

These are statistical groupings only -- never real physical infrastructure
identities or locations. See docs/superpowers/specs/2026-07-24-grid-cooccurrence-clusters-design.md.
"""

import math
from collections import defaultdict
from datetime import date

import networkx as nx
import community as community_louvain

import db
import geocoding

MIN_NOTICES = 30
MIN_LOCALITIES = 10
STABILITY_LOOKBACK_DAYS = 7


def build_ppmi_graph(cooccurrences: list, total_notices: int) -> nx.Graph:
    """cooccurrences: rows with locality_a, locality_b, notice_count.
    Edge weight = positive PMI (negatives clipped to 0, Louvain needs
    non-negative weights)."""
    occurrence_count = defaultdict(int)
    for row in cooccurrences:
        occurrence_count[row["locality_a"]] += row["notice_count"]
        occurrence_count[row["locality_b"]] += row["notice_count"]

    G = nx.Graph()
    for row in cooccurrences:
        a, b, count = row["locality_a"], row["locality_b"], row["notice_count"]
        G.add_node(a)
        G.add_node(b)
        p_ab = count / total_notices
        p_a = occurrence_count[a] / total_notices
        p_b = occurrence_count[b] / total_notices
        if p_a <= 0 or p_b <= 0 or p_ab <= 0:
            continue
        pmi = math.log(p_ab / (p_a * p_b))
        ppmi = max(0.0, pmi)
        if ppmi > 0:
            G.add_edge(a, b, weight=ppmi)
    return G


def compute_clusters(G: nx.Graph) -> dict:
    """{locality: cluster_id}. Isolated nodes each become a singleton
    cluster with a unique id (Louvain only partitions connected structure)."""
    if G.number_of_nodes() == 0:
        return {}
    if G.number_of_edges() == 0:
        return {node: i for i, node in enumerate(G.nodes())}
    partition = community_louvain.best_partition(G, weight="weight")
    # Any node with no edges still needs its own cluster id even if Louvain
    # grouped connected components already -- best_partition covers all
    # nodes passed to it, isolated or not, so this is already complete.
    return partition


def compute_stability(run_date: str, cluster_assignment: dict) -> dict:
    """Jaccard similarity of each locality's cluster co-membership today
    vs. each of the last STABILITY_LOOKBACK_DAYS run_dates, averaged."""
    today_members = defaultdict(set)
    for locality, cid in cluster_assignment.items():
        today_members[cid].add(locality)

    past_dates = db.cluster_run_dates(before=run_date, limit=STABILITY_LOOKBACK_DAYS)

    stability = {}
    for locality, cid in cluster_assignment.items():
        today_set = today_members[cid] - {locality}
        if not past_dates:
            stability[locality] = 0.0
            continue
        scores = []
        for past_date in past_dates:
            past_cid = db.locality_cluster_on(past_date, locality)
            if past_cid is None:
                scores.append(0.0)
                continue
            past_members = db.cluster_members(past_date, past_cid) - {locality}
            union = today_set | past_members
            inter = today_set & past_members
            scores.append(len(inter) / len(union) if union else 0.0)
        stability[locality] = sum(scores) / len(scores)
    return stability


def run_recluster() -> dict:
    """Full daily job: check data floor, geocode pending localities, build
    the PPMI graph, cluster, score stability, persist. Returns a summary
    dict mirroring the /api/internal/recluster response."""
    run_date = date.today().isoformat()

    if db.has_cluster_run(run_date):
        return {"status": "already_done", "run_date": run_date}

    notices_so_far = db.total_notice_count()
    localities_so_far = db.distinct_locality_count()
    if notices_so_far < MIN_NOTICES or localities_so_far < MIN_LOCALITIES:
        return {
            "status": "insufficient_data",
            "run_date": run_date,
            "notices_so_far": notices_so_far,
            "needed": MIN_NOTICES,
        }

    geocoding.geocode_all_pending()

    cooccurrences = db.list_cooccurrences()
    G = build_ppmi_graph(cooccurrences, total_notices=notices_so_far)
    for name in db.list_locality_names():
        G.add_node(name)  # localities with zero co-occurrence still get a singleton cluster

    partition = compute_clusters(G)
    stability = compute_stability(run_date, partition)
    db.write_cluster_run(run_date, partition, stability)

    return {
        "status": "ok",
        "run_date": run_date,
        "localities_clustered": len(partition),
        "cluster_count": len(set(partition.values())),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cluster_inference.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add cluster_inference.py tests/test_cluster_inference.py
git commit -m "feat: add PPMI graph, Louvain clustering, stability scoring, recluster job"
```

---

## Task 8: Protected internal endpoints + `GET /api/clusters`

**Files:**
- Modify: `app.py`
- Test: `tests/test_app_internal_endpoints.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_internal_endpoints.py
import os
from fastapi.testclient import TestClient

import db


def _client(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-secret")
    import importlib
    import app as app_module
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app_internal_endpoints.py -v`
Expected: FAIL — 404s on `/api/internal/scrape`, `/api/internal/recluster`, `/api/clusters` (routes don't exist yet).

- [ ] **Step 3: Add the endpoints, models, and auth dependency to `app.py`**

Add these imports near the top of `app.py` (alongside the existing ones):

```python
import os

from fastapi import Depends, Header

import cluster_inference
import import_official
```

Add after the `ReportIn` schema:

```python
CRON_SECRET = os.environ.get("CRON_SECRET")


def verify_cron_secret(x_cron_secret: str = Header(None)):
    if not CRON_SECRET or x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Cron-Secret")


class ScrapeResult(BaseModel):
    notices_processed: int
    total_in_db: int


class RecheckResult(BaseModel):
    status: str
    run_date: str
    notices_so_far: Optional[int] = None
    needed: Optional[int] = None
    localities_clustered: Optional[int] = None
    cluster_count: Optional[int] = None


class ClusterPoint(BaseModel):
    locality: str
    cluster_id: int
    stability: float
    lat: float
    lng: float


class ClustersResponse(BaseModel):
    data: list[ClusterPoint]
    insufficient_data: bool
    notices_so_far: int
    needed: int = cluster_inference.MIN_NOTICES
```

Add the endpoints after `get_stats()`:

```python
@app.post("/api/internal/scrape", response_model=ScrapeResult, dependencies=[Depends(verify_cron_secret)])
def internal_scrape():
    try:
        count = import_official.run(verbose=False)
    except import_official.steg_scraper.FetchError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return ScrapeResult(notices_processed=count, total_in_db=db.count_official_notices())


@app.post("/api/internal/recluster", response_model=RecheckResult, dependencies=[Depends(verify_cron_secret)])
def internal_recluster():
    return RecheckResult(**cluster_inference.run_recluster())


@app.get("/api/clusters", response_model=ClustersResponse)
def get_clusters():
    notices_so_far = db.total_notice_count()
    latest = db.latest_cluster_run()
    if latest is None:
        return ClustersResponse(data=[], insufficient_data=True, notices_so_far=notices_so_far)
    points = [
        ClusterPoint(locality=r["locality"], cluster_id=r["cluster_id"],
                     stability=r["stability"], lat=r["lat"], lng=r["lng"])
        for r in latest["rows"] if r["lat"] is not None
    ]
    return ClustersResponse(data=points, insufficient_data=False, notices_so_far=notices_so_far)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_app_internal_endpoints.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: all tests across every task pass, no regressions in the pre-existing endpoints (`/api/reports`, `/api/official`, `/api/stats` are untouched).

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_app_internal_endpoints.py
git commit -m "feat: add protected scrape/recluster endpoints and GET /api/clusters"
```

---

## Task 9: GitHub Actions cron workflow

**Files:**
- Create: `.github/workflows/scrape.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: Scrape and recluster

on:
  schedule:
    - cron: "0 * * * *"   # hourly, at minute 0 -- scrape
    - cron: "30 2 * * *"  # once daily at 02:30 UTC -- recluster
  workflow_dispatch: {}    # allow manual runs from the Actions tab

jobs:
  scrape:
    if: github.event.schedule == '0 * * * *' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    steps:
      - name: Hit /api/internal/scrape
        run: |
          curl -sf -X POST "${{ secrets.APP_URL }}/api/internal/scrape" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}"

  recluster:
    if: github.event.schedule == '30 2 * * *'
    runs-on: ubuntu-latest
    steps:
      - name: Hit /api/internal/recluster
        run: |
          curl -sf -X POST "${{ secrets.APP_URL }}/api/internal/recluster" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}"
```

- [ ] **Step 2: Verify locally (syntax only, can't trigger a real schedule)**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/scrape.yml'))"`
Expected: no output, no exception — confirms valid YAML. (Requires `pyyaml`; `pip install pyyaml` if not already present, it's a one-off check, not a new project dependency.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/scrape.yml
git commit -m "ci: add GitHub Actions cron trigger for scrape/recluster endpoints"
```

---

## Task 10: Frontend — inferred clusters map layer

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add the toggle control and legend markup**

In `static/index.html`, inside `<div class="map-wrap">`, right after the existing `<div class="map-legend">...</div>` block, add:

```html
    <div class="cluster-toggle-row">
      <label class="cluster-toggle">
        <input type="checkbox" id="toggle-clusters">
        Afficher les groupes de réseau inférés (bêta)
      </label>
      <div class="cluster-disclaimer" id="cluster-disclaimer" hidden>
        Regroupement statistique basé sur les coupures groupées dans les avis STEG —
        ce n'est pas une carte d'infrastructure officielle vérifiée.
      </div>
    </div>
```

Add matching CSS inside the existing `<style>` block, near `.map-legend`:

```css
  .cluster-toggle-row { margin-top: var(--space-2); font-size: 0.8rem; }
  .cluster-toggle { display: flex; align-items: center; gap: 6px; color: var(--muted); cursor: pointer; }
  .cluster-disclaimer {
    margin-top: 6px; color: var(--muted); font-size: 0.74rem;
    border-left: 2px solid var(--border); padding-left: var(--space-2);
  }
```

- [ ] **Step 2: Add the cluster-layer rendering logic**

In the `<script>` block, add a second layer group next to `markerLayer` and the fetch/render logic. Modify the top of the script (where `let map, markerLayer;` is declared):

```javascript
let map, markerLayer, clusterLayer;
const CLUSTER_PALETTE = [
  "#eab308", "#38bdf8", "#a78bfa", "#f87171", "#4ade80", "#fb923c",
  "#22d3ee", "#e879f9", "#facc15", "#818cf8", "#34d399", "#fb7185",
];
```

In `initGovernorates()`, right after `markerLayer = L.layerGroup().addTo(map);`, add:

```javascript
  clusterLayer = L.layerGroup(); // not added to map yet -- off by default
```

After the existing `refreshMap()` function, add:

```javascript
async function loadClusters() {
  clusterLayer.clearLayers();
  let body;
  try {
    const resp = await fetch(`${API}/clusters`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    body = await resp.json();
  } catch (e) {
    return;
  }
  if (body.insufficient_data || !body.data.length) {
    return;
  }
  body.data.forEach(point => {
    const color = CLUSTER_PALETTE[point.cluster_id % CLUSTER_PALETTE.length];
    const opacity = 0.25 + 0.65 * Math.max(0, Math.min(1, point.stability));
    L.circleMarker([point.lat, point.lng], {
      radius: 6, color, fillColor: color, fillOpacity: opacity, weight: 1,
    }).bindTooltip(
      `Cluster #${point.cluster_id} — stabilité ${(point.stability * 100).toFixed(0)}%`
    ).addTo(clusterLayer);
  });
}

document.getElementById("toggle-clusters").addEventListener("change", async (e) => {
  const disclaimer = document.getElementById("cluster-disclaimer");
  if (e.target.checked) {
    disclaimer.hidden = false;
    await loadClusters();
    clusterLayer.addTo(map);
  } else {
    disclaimer.hidden = true;
    map.removeLayer(clusterLayer);
  }
});
```

- [ ] **Step 3: Manual verification**

Run: `uvicorn app:app --reload --port 8010` (from the project directory, with dependencies installed and at least one `write_cluster_run` in the DB — use the Python REPL to call `db.upsert_locality(...)` and `db.write_cluster_run(...)` with fake data if no real data has accumulated yet).
Then open `http://127.0.0.1:8010`, check the "Afficher les groupes de réseau inférés (bêta)" checkbox, and confirm: colored dots appear at the seeded coordinates, the disclaimer text becomes visible, hovering a dot shows the cluster/stability tooltip, and unchecking removes the layer and hides the disclaimer.
Expected: all of the above behave as described, matching the design spec's UI section.

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat: add opt-in inferred-cluster map layer with disclaimer and tooltip"
```

---

## Task 11: Turso setup docs + README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a new section documenting the env vars and Actions secrets**

Append to `README.md`:

```markdown
## Grid co-occurrence clusters (new)

This adds a statistical, opt-in "inferred grid clusters" map layer — see
`docs/superpowers/specs/2026-07-24-grid-cooccurrence-clusters-design.md`
for the full design. It requires:

**Environment variables** (set wherever the app is deployed):
- `TURSO_DATABASE_URL` — e.g. `libsql://<your-db>.turso.io`. If unset, the
  app falls back to a local `tracker.db` file (fine for local dev/tests).
- `TURSO_AUTH_TOKEN` — Turso auth token (only needed alongside a real
  `TURSO_DATABASE_URL`).
- `CRON_SECRET` — any random string; must match the `CRON_SECRET` GitHub
  Actions secret below. Without it, `/api/internal/scrape` and
  `/api/internal/recluster` reject all requests with 401.

**GitHub Actions secrets** (repo Settings → Secrets and variables → Actions):
- `APP_URL` — the deployed app's base URL (e.g. `https://your-app.onrender.com`)
- `CRON_SECRET` — must match the env var above

Once both are set, `.github/workflows/scrape.yml` runs hourly (scrape) and
daily (recluster) automatically — no manual steps needed after initial setup.

**Turso setup** (one-time): create a free account at turso.tech, create a
database, and get the URL/token from their dashboard or `turso db show`/
`turso db tokens create` CLI commands.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document Turso env vars and GitHub Actions secrets setup"
```

---

## Self-Review

**1. Spec coverage:**
- 3 new tables + `locality_aliases` → Task 3. ✅
- Protected `/api/internal/scrape`, `/api/internal/recluster`, shared-secret auth → Task 8. ✅
- GitHub Actions as pure HTTP trigger → Task 9. ✅
- PPMI + Louvain + stability → Task 7. ✅
- Geocoding with caching → Task 5. ✅
- Locality dedup (normalize/exact/fuzzy/alias) → Task 4. ✅
- `GET /api/clusters` + Pydantic models → Task 8. ✅
- Map layer/toggle + disclaimer + tooltip → Task 10. ✅
- Turso migration (replacing Fly.io/sqlite3) → Task 3 (db.py) + Task 11 (docs). ✅
- Testing plan (PPMI/Louvain/stability unit tests, endpoint auth/idempotency/insufficient-data, geocoding cache, cooccurrence extraction) → Tasks 3-8, each ships its own tests. ✅
- Data-floor tuning signals (modularity, singleton fraction, median stability) from the spec are documented as *signals to monitor*, not enforced in code — intentional, since the spec frames them as things to watch once real data accumulates, not a v1 implementation requirement. Not a gap.
- Cluster-ID matching across days (deferred to v2 per spec) — correctly not implemented here.

**2. Placeholder scan:** No "TBD"/"TODO" strings in any step. Every code step is complete, runnable code, not a description of code.

**3. Type consistency:** `db.get_locality`/`upsert_locality`/`set_locality_coords` names used consistently across Tasks 3-8. `cluster_inference.run_recluster()`'s return dict keys (`status`, `run_date`, `notices_so_far`, `needed`, `localities_clustered`, `cluster_count`) match `RecheckResult`'s optional fields in Task 8 exactly. `ClusterPoint`/`ClustersResponse` field names match what `get_clusters()` constructs and what `static/index.html`'s `loadClusters()` reads (`point.cluster_id`, `point.stability`, `point.lat`, `point.lng`) — verified consistent across Tasks 8 and 10.

---

**Plan complete and saved to `docs/superpowers/plans/2026-07-24-grid-cooccurrence-clusters.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
</content>
