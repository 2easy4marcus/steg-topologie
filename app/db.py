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
    """
    CREATE TABLE IF NOT EXISTS locality_notice_counts (
        locality TEXT PRIMARY KEY,
        notice_count INTEGER NOT NULL DEFAULT 0
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
    kwargs = {"url": DB_URL}
    if AUTH_TOKEN:
        kwargs["auth_token"] = AUTH_TOKEN
    client = libsql_client.create_client_sync(**kwargs)
    try:
        yield _Conn(client)
    finally:
        client.close()


def init_db():
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


# ---------- official notices ----------

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


def list_official_notices(region: str = None, limit: int = 100, offset: int = 0):
    query = "SELECT * FROM official_notices"
    params = []
    if region:
        query += " WHERE region = ?"
        params.append(region)
    query += " ORDER BY notice_date DESC, notice_time DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for d in rows:
        d = dict(d)
        d["zones"] = json.loads(d["zones"] or "[]")
        d["subregions"] = json.loads(d["subregions"] or "[]")
        out.append(d)
    return out


def count_official_notices() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM official_notices").fetchone()["c"]


# ---------- user reports ----------

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


def list_user_reports(utility: str = None, status: str = None, governorate: str = None,
                       limit: int = 200, offset: int = 0):
    query = "SELECT * FROM user_reports WHERE 1=1"
    params = []
    if utility:
        query += " AND utility = ?"
        params.append(utility)
    if status:
        query += " AND status = ?"
        params.append(status)
    if governorate:
        query += " AND governorate = ?"
        params.append(governorate)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def stats_by_governorate():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT governorate, utility, COUNT(*) as c
            FROM user_reports
            WHERE status = 'active'
            GROUP BY governorate, utility
            """
        ).fetchall()
    return [dict(r) for r in rows]


def overall_stats():
    with get_conn() as conn:
        active_elec = conn.execute(
            "SELECT COUNT(*) c FROM user_reports WHERE status='active' AND utility='electricity'"
        ).fetchone()["c"]
        active_water = conn.execute(
            "SELECT COUNT(*) c FROM user_reports WHERE status='active' AND utility='water'"
        ).fetchone()["c"]
        total_reports = conn.execute("SELECT COUNT(*) c FROM user_reports").fetchone()["c"]
    return {
        "active_electricity": active_elec,
        "active_water": active_water,
        "total_reports": total_reports,
        "official_notices": count_official_notices(),
    }


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


def increment_locality_notice_count(locality: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO locality_notice_counts (locality, notice_count)
            VALUES (?, 1)
            ON CONFLICT(locality) DO UPDATE SET notice_count = notice_count + 1
            """,
            [locality],
        )


def get_locality_notice_counts() -> dict:
    """{locality: number of distinct notices it has appeared in}."""
    with get_conn() as conn:
        rows = conn.execute("SELECT locality, notice_count FROM locality_notice_counts").fetchall()
    return {r["locality"]: r["notice_count"] for r in rows}


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
    return count_official_notices()


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
            FROM clusters c LEFT JOIN localities l ON l.name = c.locality
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
