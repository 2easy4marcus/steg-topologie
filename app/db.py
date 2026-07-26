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
    """
    CREATE TABLE IF NOT EXISTS notice_fetch_attempts (
        id TEXT PRIMARY KEY,
        notice_id TEXT,
        source_url TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        http_status INTEGER,
        content_hash TEXT,
        public_error_code TEXT,
        internal_error_detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notice_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        notice_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        raw_html TEXT NOT NULL,
        first_fetched_at TEXT NOT NULL,
        UNIQUE(notice_id, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notice_parses (
        parse_id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL,
        notice_id TEXT NOT NULL,
        title TEXT NOT NULL,
        notice_date_raw TEXT,
        notice_date_iso TEXT,
        parser_version TEXT NOT NULL,
        normalization_version TEXT NOT NULL,
        parse_status TEXT NOT NULL,
        parse_warnings TEXT NOT NULL DEFAULT '[]',
        parsed_at TEXT NOT NULL,
        UNIQUE(snapshot_id, parser_version, normalization_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notice_localities (
        parse_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        raw_name TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        subregion_name TEXT,
        PRIMARY KEY(parse_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notice_state (
        notice_id TEXT PRIMARY KEY,
        latest_snapshot_id TEXT,
        active_parse_id TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_builds (
        build_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        notice_count INTEGER NOT NULL DEFAULT 0,
        locality_count INTEGER NOT NULL DEFAULT 0,
        pair_count INTEGER NOT NULL DEFAULT 0,
        public_error_code TEXT,
        internal_error_detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS build_locality_counts (
        build_id TEXT NOT NULL,
        locality TEXT NOT NULL,
        notice_count INTEGER NOT NULL,
        PRIMARY KEY(build_id, locality)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS build_cooccurrences (
        build_id TEXT NOT NULL,
        locality_a TEXT NOT NULL,
        locality_b TEXT NOT NULL,
        notice_count INTEGER NOT NULL,
        distinct_date_count INTEGER NOT NULL,
        first_observed_on TEXT,
        last_observed_on TEXT,
        PRIMARY KEY(build_id, locality_a, locality_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_state (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        active_build_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_runs (
        run_id TEXT PRIMARY KEY,
        build_id TEXT NOT NULL,
        algorithm_version TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        cluster_count INTEGER NOT NULL DEFAULT 0,
        locality_count INTEGER NOT NULL DEFAULT 0,
        public_error_code TEXT,
        internal_error_detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_members (
        run_id TEXT NOT NULL,
        locality TEXT NOT NULL,
        cluster_id INTEGER NOT NULL,
        stability REAL NOT NULL,
        PRIMARY KEY(run_id, locality)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_state (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        active_cluster_run_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_locks (
        lock_name TEXT PRIMARY KEY,
        owner_job_id TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fetch_attempts_notice ON notice_fetch_attempts(notice_id)",
    "CREATE INDEX IF NOT EXISTS idx_fetch_attempts_time ON notice_fetch_attempts(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_notice ON notice_snapshots(notice_id)",
    "CREATE INDEX IF NOT EXISTS idx_parses_notice ON notice_parses(notice_id)",
    "CREATE INDEX IF NOT EXISTS idx_parses_snapshot ON notice_parses(snapshot_id)",
    "CREATE INDEX IF NOT EXISTS idx_parses_date ON notice_parses(notice_date_iso)",
    "CREATE INDEX IF NOT EXISTS idx_notice_localities_name ON notice_localities(canonical_name)",
    "CREATE INDEX IF NOT EXISTS idx_build_localities_build ON build_locality_counts(build_id)",
    "CREATE INDEX IF NOT EXISTS idx_build_pairs_build ON build_cooccurrences(build_id)",
    "CREATE INDEX IF NOT EXISTS idx_cluster_members_run ON cluster_members(run_id)",
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

    @property
    def rows_affected(self):
        return self._rs.rows_affected


class _Conn:
    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=None):
        return _Result(self._client.execute(sql, list(params or [])))


class _Transaction(_Conn):
    def commit(self):
        self._client.commit()

    def rollback(self):
        self._client.rollback()

    def close(self):
        self._client.close()


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


@contextmanager
def get_transaction():
    kwargs = {"url": DB_URL}
    if AUTH_TOKEN:
        kwargs["auth_token"] = AUTH_TOKEN
    client = libsql_client.create_client_sync(**kwargs)
    transaction = _Transaction(client.transaction())
    try:
        yield transaction
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    finally:
        transaction.close()
        client.close()


def init_db():
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


def get_model_build_public(build_id: str):
    """Return the public-safe model-build fields only."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT build_id, status, created_at, completed_at, notice_count,
                   locality_count, pair_count, public_error_code
            FROM model_builds
            WHERE build_id = ?
            """,
            [build_id],
        ).fetchone()


# ---------- evidence state and build activation ----------

def select_latest_snapshot(
    notice_id: str, snapshot_id: str, updated_at: str
) -> None:
    with get_transaction() as tx:
        snapshot = tx.execute(
            """
            SELECT snapshot_id FROM notice_snapshots
            WHERE snapshot_id = ? AND notice_id = ?
            """,
            [snapshot_id, notice_id],
        ).fetchone()
        if snapshot is None:
            raise ValueError("snapshot does not belong to notice")
        tx.execute(
            """
            INSERT INTO notice_state(
                notice_id, latest_snapshot_id, active_parse_id, updated_at
            ) VALUES (?, ?, NULL, ?)
            ON CONFLICT(notice_id) DO UPDATE SET
                latest_snapshot_id = excluded.latest_snapshot_id,
                updated_at = excluded.updated_at
            """,
            [notice_id, snapshot_id, updated_at],
        )


def get_notice_state(notice_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM notice_state WHERE notice_id = ?", [notice_id]
        ).fetchone()


def activate_notice_parse(
    notice_id: str, parse_id: str, activated_at: str
) -> None:
    with get_transaction() as tx:
        row = tx.execute(
            """
            SELECT p.parse_status, p.snapshot_id, s.latest_snapshot_id,
                   (SELECT COUNT(DISTINCT canonical_name)
                    FROM notice_localities nl
                    WHERE nl.parse_id = p.parse_id) AS locality_count
            FROM notice_parses p
            JOIN notice_state s ON s.notice_id = p.notice_id
            WHERE p.parse_id = ? AND p.notice_id = ?
            """,
            [parse_id, notice_id],
        ).fetchone()
        if row is None or row["snapshot_id"] != row["latest_snapshot_id"]:
            raise ValueError("parse does not belong to latest snapshot")
        eligible = row["parse_status"] == "ok" or (
            row["parse_status"] == "warning" and row["locality_count"] >= 2
        )
        if not eligible:
            raise ValueError("parse is not eligible for activation")
        tx.execute(
            """
            UPDATE notice_state
            SET active_parse_id = ?, updated_at = ?
            WHERE notice_id = ?
            """,
            [parse_id, activated_at, notice_id],
        )


def create_model_build(build_id: str, created_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO model_builds(build_id, status, created_at)
            VALUES (?, 'building', ?)
            """,
            [build_id, created_at],
        )


def complete_model_build(
    build_id: str,
    completed_at: str,
    notice_count: int,
    locality_count: int,
    pair_count: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE model_builds
            SET status = 'completed', completed_at = ?, notice_count = ?,
                locality_count = ?, pair_count = ?
            WHERE build_id = ? AND status = 'building'
            """,
            [
                completed_at,
                notice_count,
                locality_count,
                pair_count,
                build_id,
            ],
        )


def activate_completed_model_build(build_id: str) -> None:
    with get_transaction() as tx:
        build = tx.execute(
            "SELECT status FROM model_builds WHERE build_id = ?", [build_id]
        ).fetchone()
        if build is None or build["status"] != "completed":
            raise ValueError("model build must be completed before activation")
        tx.execute(
            """
            INSERT INTO model_state(singleton_id, active_build_id)
            VALUES (1, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                active_build_id = excluded.active_build_id
            """,
            [build_id],
        )


def active_build_id():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_build_id FROM model_state WHERE singleton_id = 1"
        ).fetchone()
        return row["active_build_id"] if row else None


def create_cluster_run(
    run_id: str,
    build_id: str,
    algorithm_version: str,
    started_at: str,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cluster_runs(
                run_id, build_id, algorithm_version, status, started_at
            ) VALUES (?, ?, ?, 'running', ?)
            """,
            [run_id, build_id, algorithm_version, started_at],
        )


def complete_cluster_run(
    run_id: str,
    completed_at: str,
    cluster_count: int,
    locality_count: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE cluster_runs
            SET status = 'completed', completed_at = ?, cluster_count = ?,
                locality_count = ?
            WHERE run_id = ? AND status = 'running'
            """,
            [completed_at, cluster_count, locality_count, run_id],
        )


def activate_completed_cluster_run(run_id: str) -> None:
    with get_transaction() as tx:
        row = tx.execute(
            """
            SELECT cr.status, cr.build_id, ms.active_build_id
            FROM cluster_runs cr
            LEFT JOIN model_state ms ON ms.singleton_id = 1
            WHERE cr.run_id = ?
            """,
            [run_id],
        ).fetchone()
        if row is None or row["status"] != "completed":
            raise ValueError("cluster run must be completed before activation")
        if row["build_id"] != row["active_build_id"]:
            raise ValueError("cluster run does not reference active build")
        tx.execute(
            """
            INSERT INTO cluster_state(singleton_id, active_cluster_run_id)
            VALUES (1, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                active_cluster_run_id = excluded.active_cluster_run_id
            """,
            [run_id],
        )


def active_cluster_run_id():
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT active_cluster_run_id FROM cluster_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        return row["active_cluster_run_id"] if row else None


# ---------- job locks ----------

def acquire_lock(
    lock_name: str,
    owner_job_id: str,
    acquired_at: str,
    expires_at: str,
):
    with get_conn() as conn:
        result = conn.execute(
            """
            INSERT INTO job_locks(
                lock_name, owner_job_id, acquired_at, heartbeat_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lock_name) DO UPDATE SET
                owner_job_id = excluded.owner_job_id,
                acquired_at = excluded.acquired_at,
                heartbeat_at = excluded.heartbeat_at,
                expires_at = excluded.expires_at
            WHERE job_locks.expires_at < excluded.acquired_at
            """,
            [lock_name, owner_job_id, acquired_at, acquired_at, expires_at],
        )
        row = conn.execute(
            "SELECT owner_job_id FROM job_locks WHERE lock_name = ?",
            [lock_name],
        ).fetchone()
        return result.rows_affected > 0, row["owner_job_id"]


def heartbeat_lock(
    lock_name: str,
    owner_job_id: str,
    heartbeat_at: str,
    expires_at: str,
) -> bool:
    with get_conn() as conn:
        result = conn.execute(
            """
            UPDATE job_locks SET heartbeat_at = ?, expires_at = ?
            WHERE lock_name = ? AND owner_job_id = ?
            """,
            [heartbeat_at, expires_at, lock_name, owner_job_id],
        )
        return result.rows_affected > 0


def release_lock(lock_name: str, owner_job_id: str) -> bool:
    with get_conn() as conn:
        result = conn.execute(
            "DELETE FROM job_locks WHERE lock_name = ? AND owner_job_id = ?",
            [lock_name, owner_job_id],
        )
        return result.rows_affected > 0


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


def official_notice_exists(notice_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM official_notices WHERE id = ?", [notice_id]
        ).fetchone()
        return row is not None


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


def count_cluster_run_dates() -> int:
    """Total distinct run_dates that have EVER existed in clusters -- unbounded,
    unlike cluster_run_dates() which is a bounded lookback for stability scoring."""
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(DISTINCT run_date) c FROM clusters").fetchone()["c"]


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
