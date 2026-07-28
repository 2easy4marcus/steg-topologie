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
from urllib.parse import urlparse

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
    """
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        current_page INTEGER,
        pages_scanned INTEGER NOT NULL DEFAULT 0,
        links_discovered INTEGER NOT NULL DEFAULT 0,
        notices_imported INTEGER NOT NULL DEFAULT 0,
        notices_unchanged INTEGER NOT NULL DEFAULT 0,
        notices_skipped INTEGER NOT NULL DEFAULT 0,
        notices_failed INTEGER NOT NULL DEFAULT 0,
        last_progress_at TEXT,
        request_id TEXT,
        public_error_code TEXT,
        internal_error_detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notice_rollbacks (
        id TEXT PRIMARY KEY,
        notice_id TEXT NOT NULL,
        from_parse_id TEXT,
        to_parse_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        rolled_back_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        level TEXT NOT NULL,
        event_type TEXT NOT NULL,
        public_message TEXT NOT NULL,
        current_page INTEGER,
        request_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_events_job_time ON job_events(job_id, occurred_at)",
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


def client_url(url: str) -> str:
    """Normalize a database URL to a scheme that supports everything we need.

    libsql_client's HTTP transport cannot do transactions at all -- it raises
    TRANSACTIONS_NOT_SUPPORTED -- while its WebSocket transport can. Turso
    serves the same database over both, but its dashboard hands out an
    https:// URL, so a deployment configured the obvious way breaks the
    moment any code path opens a transaction (this happened in production:
    every evidence-pipeline notice import failed). Mapping https:// to libsql://
    here means the scheme in the env var is parsed natively as the standard
    Turso/libSQL scheme (which leverages a fully-configured WebSocket connection
    and handshake protocol) rather than falling back to HTTP.
    file:, libsql:, ws: and wss: URLs pass through unchanged.
    """
    parsed = urlparse(url)
    if parsed.scheme in ("https", "http"):
        return parsed._replace(scheme="libsql").geturl()
    return url


def _client_kwargs() -> dict:
    kwargs = {"url": client_url(DB_URL)}
    if AUTH_TOKEN:
        kwargs["auth_token"] = AUTH_TOKEN
    return kwargs


@contextmanager
def get_conn():
    client = libsql_client.create_client_sync(**_client_kwargs())
    try:
        yield _Conn(client)
    finally:
        client.close()


@contextmanager
def get_transaction():
    client = libsql_client.create_client_sync(**_client_kwargs())
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


def get_or_create_snapshot(
    notice_id: str,
    source_url: str,
    content_hash: str,
    raw_html: str,
    fetched_at: str,
):
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT * FROM notice_snapshots
            WHERE notice_id = ? AND content_hash = ?
            """,
            [notice_id, content_hash],
        ).fetchone()
        if existing:
            return existing, False
        snapshot_id = f"{notice_id}-{content_hash[:20]}"
        conn.execute(
            """
            INSERT INTO notice_snapshots(
                snapshot_id, notice_id, source_url, content_hash,
                raw_html, first_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_id,
                notice_id,
                source_url,
                content_hash,
                raw_html,
                fetched_at,
            ],
        )
        return conn.execute(
            "SELECT * FROM notice_snapshots WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone(), True


def save_parse_with_localities(evidence, parsed_at: str) -> bool:
    parse_id = processing_parse_id(
        evidence.snapshot_id,
        evidence.parser_version,
        evidence.normalization_version,
    )
    with get_transaction() as tx:
        existing = tx.execute(
            "SELECT parse_id FROM notice_parses WHERE parse_id = ?",
            [parse_id],
        ).fetchone()
        if existing:
            return False
        tx.execute(
            """
            INSERT INTO notice_parses(
                parse_id, snapshot_id, notice_id, title, notice_date_raw,
                notice_date_iso, parser_version, normalization_version,
                parse_status, parse_warnings, parsed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                parse_id,
                evidence.snapshot_id,
                evidence.notice_id,
                evidence.title,
                evidence.notice_date_raw,
                evidence.notice_date_iso.isoformat()
                if evidence.notice_date_iso
                else None,
                evidence.parser_version,
                evidence.normalization_version,
                evidence.parse_status.value,
                json.dumps(evidence.warnings, ensure_ascii=False),
                parsed_at,
            ],
        )
        for locality in evidence.localities:
            tx.execute(
                """
                INSERT INTO notice_localities(
                    parse_id, ordinal, raw_name, canonical_name,
                    subregion_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    parse_id,
                    locality.ordinal,
                    locality.raw_name,
                    locality.canonical_name,
                    locality.subregion_name,
                ],
            )
    return True


def processing_parse_id(
    snapshot_id: str, parser_version: str, normalization_version: str
) -> str:
    import hashlib

    value = "\0".join(
        [snapshot_id, parser_version, normalization_version]
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


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


def write_cluster_members(
    run_id: str, cluster_assignment: dict, stability: dict
) -> None:
    with get_conn() as conn:
        for locality, cluster_id in cluster_assignment.items():
            conn.execute(
                """
                INSERT INTO cluster_members(
                    run_id, locality, cluster_id, stability
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, locality) DO UPDATE SET
                    cluster_id = excluded.cluster_id,
                    stability = excluded.stability
                """,
                [
                    run_id,
                    locality,
                    cluster_id,
                    stability.get(locality, 0.0),
                ],
            )


def active_cluster_run():
    run_id = active_cluster_run_id()
    if not run_id:
        return None
    with get_conn() as conn:
        run = conn.execute(
            "SELECT * FROM cluster_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        rows = conn.execute(
            """
            SELECT cm.locality, cm.cluster_id, cm.stability, l.lat, l.lng
            FROM cluster_members cm
            LEFT JOIN localities l ON l.name = cm.locality
            WHERE cm.run_id = ?
            """,
            [run_id],
        ).fetchall()
        return {**run, "rows": rows}


def completed_cluster_run_for(
    run_date: str, build_id: str, algorithm_version: str
):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM cluster_runs
            WHERE build_id = ? AND algorithm_version = ?
              AND status = 'completed'
              AND substr(started_at, 1, 10) = ?
            ORDER BY completed_at DESC LIMIT 1
            """,
            [build_id, algorithm_version, run_date],
        ).fetchone()


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


def populate_model_build(build_id: str) -> None:
    """Populate one inactive build from last-known-valid active parses."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO build_locality_counts(
                build_id, locality, notice_count
            )
            SELECT ?, canonical_name, COUNT(DISTINCT notice_id)
            FROM (
                SELECT DISTINCT ns.notice_id, nl.canonical_name
                FROM notice_state ns
                JOIN notice_localities nl
                  ON nl.parse_id = ns.active_parse_id
            )
            GROUP BY canonical_name
            """,
            [build_id],
        )
        conn.execute(
            """
            INSERT INTO build_cooccurrences(
                build_id, locality_a, locality_b, notice_count,
                distinct_date_count, first_observed_on, last_observed_on
            )
            WITH notice_names AS (
                SELECT DISTINCT ns.notice_id, nl.canonical_name,
                       np.notice_date_iso
                FROM notice_state ns
                JOIN notice_parses np ON np.parse_id = ns.active_parse_id
                JOIN notice_localities nl ON nl.parse_id = ns.active_parse_id
            ),
            observations AS (
                SELECT a.notice_id, a.canonical_name AS locality_a,
                       b.canonical_name AS locality_b,
                       a.notice_date_iso
                FROM notice_names a
                JOIN notice_names b
                  ON b.notice_id = a.notice_id
                 AND a.canonical_name < b.canonical_name
            )
            SELECT ?, locality_a, locality_b, COUNT(DISTINCT notice_id),
                   COUNT(DISTINCT notice_date_iso),
                   MIN(notice_date_iso), MAX(notice_date_iso)
            FROM observations
            GROUP BY locality_a, locality_b
            """,
            [build_id],
        )


def model_build_counts(build_id: str):
    with get_conn() as conn:
        notice_count = conn.execute(
            """
            SELECT COUNT(DISTINCT ns.notice_id) AS c
            FROM notice_state ns
            JOIN notice_localities nl ON nl.parse_id = ns.active_parse_id
            """,
        ).fetchone()["c"]
        locality_count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM build_locality_counts
            WHERE build_id = ?
            """,
            [build_id],
        ).fetchone()["c"]
        pair_count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM build_cooccurrences
            WHERE build_id = ?
            """,
            [build_id],
        ).fetchone()["c"]
        return notice_count, locality_count, pair_count


def validate_model_build(build_id: str) -> None:
    with get_conn() as conn:
        invalid = conn.execute(
            """
            SELECT COUNT(*) AS c FROM build_cooccurrences
            WHERE build_id = ? AND locality_a >= locality_b
            """,
            [build_id],
        ).fetchone()["c"]
        if invalid:
            raise ValueError("model build contains invalid edge ordering")


def build_locality_counts(build_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT locality, notice_count FROM build_locality_counts
            WHERE build_id = ? ORDER BY locality
            """,
            [build_id],
        ).fetchall()
        return {row["locality"]: row["notice_count"] for row in rows}


def build_cooccurrences(build_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM build_cooccurrences
            WHERE build_id = ? ORDER BY locality_a, locality_b
            """,
            [build_id],
        ).fetchall()


def model_readiness_metrics(build_id: str | None):
    if not build_id:
        return {
            "valid_notices": 0,
            "distinct_outage_dates": 0,
            "unique_localities": 0,
            "repeated_pairs": 0,
            "active_ok_ratio": 0.0,
            "largest_notice_pair_share": 0.0,
        }
    with get_conn() as conn:
        notice_metrics = conn.execute(
            """
            WITH valid AS (
                SELECT ns.notice_id, ns.active_parse_id, np.parse_status,
                       np.notice_date_iso,
                       COUNT(DISTINCT nl.canonical_name) AS locality_count
                FROM notice_state ns
                JOIN notice_parses np ON np.parse_id = ns.active_parse_id
                JOIN notice_localities nl ON nl.parse_id = ns.active_parse_id
                GROUP BY ns.notice_id, ns.active_parse_id,
                         np.parse_status, np.notice_date_iso
                HAVING COUNT(DISTINCT nl.canonical_name) >= 2
            )
            SELECT COUNT(*) AS valid_notices,
                   COUNT(DISTINCT notice_date_iso) AS distinct_dates,
                   COALESCE(AVG(CASE WHEN parse_status = 'ok'
                                     THEN 1.0 ELSE 0.0 END), 0) AS ok_ratio
            FROM valid
            """
        ).fetchone()
        build_metrics = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM build_locality_counts
                 WHERE build_id = ?) AS unique_localities,
                (SELECT COUNT(*) FROM build_cooccurrences
                 WHERE build_id = ? AND notice_count >= 2) AS repeated_pairs
            """,
            [build_id, build_id],
        ).fetchone()
        share = conn.execute(
            """
            WITH valid_names AS (
                SELECT DISTINCT ns.notice_id, nl.canonical_name
                FROM notice_state ns
                JOIN notice_localities nl ON nl.parse_id = ns.active_parse_id
                WHERE (
                    SELECT COUNT(DISTINCT nl2.canonical_name)
                    FROM notice_localities nl2
                    WHERE nl2.parse_id = ns.active_parse_id
                ) >= 2
            ),
            pair_counts AS (
                SELECT a.notice_id, COUNT(*) AS pair_count
                FROM valid_names a
                JOIN valid_names b
                  ON b.notice_id = a.notice_id
                 AND a.canonical_name < b.canonical_name
                GROUP BY a.notice_id
            )
            SELECT CASE WHEN COALESCE(SUM(pair_count), 0) = 0 THEN 0.0
                        ELSE CAST(MAX(pair_count) AS REAL) / SUM(pair_count)
                   END AS largest_share
            FROM pair_counts
            """
        ).fetchone()
    return {
        "valid_notices": notice_metrics["valid_notices"],
        "distinct_outage_dates": notice_metrics["distinct_dates"],
        "unique_localities": build_metrics["unique_localities"],
        "repeated_pairs": build_metrics["repeated_pairs"],
        "active_ok_ratio": notice_metrics["ok_ratio"],
        "largest_notice_pair_share": share["largest_share"] or 0.0,
    }


def operational_health_metrics(cutoff: str):
    with get_conn() as conn:
        parses = conn.execute(
            """
            WITH ranked AS (
                SELECT np.parse_status,
                       ROW_NUMBER() OVER (
                           PARTITION BY np.snapshot_id
                           ORDER BY np.parsed_at DESC, np.parse_id DESC
                       ) AS rn
                FROM notice_state ns
                JOIN notice_parses np
                  ON np.snapshot_id = ns.latest_snapshot_id
                WHERE np.parsed_at >= ?
            )
            SELECT COALESCE(
                AVG(CASE WHEN parse_status = 'ok' THEN 1.0 ELSE 0.0 END),
                0
            ) AS success_ratio
            FROM ranked WHERE rn = 1
            """,
            [cutoff],
        ).fetchone()
        scrape = conn.execute(
            """
            SELECT finished_at FROM ingestion_runs
            WHERE job_type = 'scrape' AND status = 'completed'
            ORDER BY finished_at DESC LIMIT 1
            """
        ).fetchone()
    return {
        "recent_parse_success_ratio": parses["success_ratio"],
        "last_successful_scrape_at": scrape["finished_at"] if scrape else None,
    }


def edge_evidence(
    build_id: str, locality_a: str, locality_b: str
):
    a, b = sorted([locality_a, locality_b])
    with get_conn() as conn:
        edge = conn.execute(
            """
            SELECT * FROM build_cooccurrences
            WHERE build_id = ? AND locality_a = ? AND locality_b = ?
            """,
            [build_id, a, b],
        ).fetchone()
        if edge is None:
            return None
        rows = conn.execute(
            """
            SELECT DISTINCT np.notice_id, np.title, np.notice_date_iso,
                   nsnap.source_url, nla.subregion_name
            FROM notice_state state
            JOIN notice_parses np ON np.parse_id = state.active_parse_id
            JOIN notice_snapshots nsnap
              ON nsnap.snapshot_id = np.snapshot_id
            JOIN notice_localities nla ON nla.parse_id = np.parse_id
            WHERE EXISTS (
                SELECT 1 FROM notice_localities x
                WHERE x.parse_id = np.parse_id AND x.canonical_name = ?
            )
              AND EXISTS (
                SELECT 1 FROM notice_localities y
                WHERE y.parse_id = np.parse_id AND y.canonical_name = ?
            )
            ORDER BY np.notice_date_iso DESC, np.notice_id DESC
            """,
            [a, b],
        ).fetchall()

    notices = {}
    order = []
    for row in rows:
        notice_id = row["notice_id"]
        if notice_id not in notices:
            order.append(notice_id)
            notices[notice_id] = {
                "notice_id": notice_id,
                "title": row["title"],
                "notice_date": row["notice_date_iso"],
                "source_url": row["source_url"],
                "subregions": [],
            }
        subregion = row["subregion_name"]
        if subregion and subregion not in notices[notice_id]["subregions"]:
            notices[notice_id]["subregions"].append(subregion)
    return {
        "locality_a": a,
        "locality_b": b,
        "distinct_notice_count": edge["notice_count"],
        "distinct_outage_dates": edge["distinct_date_count"],
        "first_observed_on": edge["first_observed_on"],
        "last_observed_on": edge["last_observed_on"],
        "active_build_id": build_id,
        "notices": [notices[notice_id] for notice_id in order],
    }


def stale_active_parse_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM notice_state ns
            JOIN notice_parses np ON np.parse_id = ns.active_parse_id
            WHERE ns.active_parse_id IS NOT NULL
              AND np.snapshot_id <> ns.latest_snapshot_id
            """
        ).fetchone()["c"]


def snapshots_missing_current_parse(
    parser_version: str, normalization_version: str
):
    with get_conn() as conn:
        states = conn.execute(
            """
            SELECT ns.notice_id, ns.latest_snapshot_id, ns.active_parse_id,
                   snap.source_url, snap.raw_html,
                   source.title, source.notice_date_raw,
                   source.notice_date_iso, source.parse_status,
                   source.parse_warnings
            FROM notice_state ns
            JOIN notice_snapshots snap
              ON snap.snapshot_id = ns.latest_snapshot_id
            LEFT JOIN notice_parses current
              ON current.snapshot_id = ns.latest_snapshot_id
             AND current.parser_version = ?
             AND current.normalization_version = ?
            LEFT JOIN notice_parses source
              ON source.parse_id = COALESCE(
                  (
                    SELECT p.parse_id FROM notice_parses p
                    WHERE p.snapshot_id = ns.latest_snapshot_id
                    ORDER BY p.parsed_at DESC, p.parse_id DESC LIMIT 1
                  ),
                  ns.active_parse_id
              )
            WHERE current.parse_id IS NULL AND source.parse_id IS NOT NULL
            """,
            [parser_version, normalization_version],
        ).fetchall()
        out = []
        for state in states:
            localities = conn.execute(
                """
                SELECT nl.* FROM notice_localities nl
                JOIN notice_parses np ON np.parse_id = nl.parse_id
                WHERE np.parse_id = COALESCE(
                    (
                        SELECT p.parse_id FROM notice_parses p
                        WHERE p.snapshot_id = ?
                        ORDER BY p.parsed_at DESC, p.parse_id DESC LIMIT 1
                    ),
                    ?
                )
                ORDER BY nl.ordinal
                """,
                [state["latest_snapshot_id"], state["active_parse_id"]],
            ).fetchall()
            out.append({**state, "localities": localities})
        return out


def rollback_notice_parse(
    notice_id: str,
    parse_id: str,
    reason: str,
    rolled_back_at: str,
    rollback_id: str,
) -> None:
    with get_transaction() as tx:
        parse = tx.execute(
            """
            SELECT parse_id, notice_id, parse_status
            FROM notice_parses WHERE parse_id = ?
            """,
            [parse_id],
        ).fetchone()
        if parse is None or parse["notice_id"] != notice_id:
            raise ValueError("parse does not belong to notice")
        if parse["parse_status"] == "failed":
            raise ValueError("failed parse is not eligible for rollback")
        state = tx.execute(
            "SELECT active_parse_id FROM notice_state WHERE notice_id = ?",
            [notice_id],
        ).fetchone()
        if state is None:
            raise ValueError("notice state not found")
        tx.execute(
            """
            UPDATE notice_state SET active_parse_id = ?, updated_at = ?
            WHERE notice_id = ?
            """,
            [parse_id, rolled_back_at, notice_id],
        )
        tx.execute(
            """
            INSERT INTO notice_rollbacks(
                id, notice_id, from_parse_id, to_parse_id, reason,
                rolled_back_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                rollback_id,
                notice_id,
                state["active_parse_id"],
                parse_id,
                reason,
                rolled_back_at,
            ],
        )


def latest_notice_rollback(notice_id: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM notice_rollbacks WHERE notice_id = ?
            ORDER BY rolled_back_at DESC, id DESC LIMIT 1
            """,
            [notice_id],
        ).fetchone()


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


# ---------- persistent ingestion status ----------

_INGESTION_COUNTERS = {
    "current_page",
    "pages_scanned",
    "links_discovered",
    "notices_imported",
    "notices_unchanged",
    "notices_skipped",
    "notices_failed",
}


def start_ingestion_run(
    run_id: str,
    job_type: str,
    started_at: str,
    *,
    request_id: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ingestion_runs(
                id, job_type, status, started_at, last_progress_at,
                request_id
            ) VALUES (?, ?, 'running', ?, ?, ?)
            """,
            [run_id, job_type, started_at, started_at, request_id],
        )


def update_ingestion_run(
    run_id: str, *, last_progress_at: str | None = None, **values
) -> None:
    invalid = set(values) - _INGESTION_COUNTERS
    if invalid:
        raise ValueError(f"unsupported ingestion counters: {sorted(invalid)}")
    if not values and last_progress_at is None:
        return
    assignments = [f"{name} = ?" for name in values]
    params = list(values.values())
    if last_progress_at is not None:
        assignments.append("last_progress_at = ?")
        params.append(last_progress_at)
    params.append(run_id)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE ingestion_runs SET {', '.join(assignments)} WHERE id = ?",
            params,
        )


def finish_ingestion_run(
    run_id: str,
    status: str,
    finished_at: str,
    *,
    public_error_code: str | None = None,
    internal_error_detail: str | None = None,
) -> None:
    if status not in {"completed", "failed"}:
        raise ValueError("invalid terminal ingestion status")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE ingestion_runs
            SET status = ?, finished_at = ?, last_progress_at = ?,
                public_error_code = ?, internal_error_detail = ?
            WHERE id = ?
            """,
            [
                status,
                finished_at,
                finished_at,
                public_error_code,
                internal_error_detail,
                run_id,
            ],
        )


def latest_ingestion_run(job_type: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, job_type, status, started_at, finished_at,
                   current_page, pages_scanned, links_discovered,
                   notices_imported, notices_unchanged, notices_skipped,
                   notices_failed, last_progress_at, request_id,
                   public_error_code
            FROM ingestion_runs
            WHERE job_type = ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            [job_type],
        ).fetchone()


def insert_job_event(
    event_id: str,
    job_id: str,
    occurred_at: str,
    level: str,
    event_type: str,
    public_message: str,
    current_page: int | None,
    request_id: str | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO job_events(
                id, job_id, occurred_at, level, event_type,
                public_message, current_page, request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                job_id,
                occurred_at,
                level,
                event_type,
                public_message,
                current_page,
                request_id,
            ],
        )


def list_job_events(job_id: str, limit: int = 200):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, job_id, occurred_at, level, event_type,
                   public_message, current_page, request_id
            FROM job_events
            WHERE job_id = ?
            ORDER BY occurred_at ASC, id ASC
            LIMIT ?
            """,
            [job_id, min(max(limit, 1), 500)],
        ).fetchall()


def list_ingestion_runs(limit: int = 20, before=None):
    where = ""
    params = []
    if before:
        where = (
            "WHERE started_at < ? OR (started_at = ? AND id < ?)"
        )
        params.extend(
            [before["started_at"], before["started_at"], before["id"]]
        )
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT id, job_type, status, started_at, finished_at,
                   current_page, pages_scanned, links_discovered,
                   notices_imported, notices_unchanged, notices_skipped,
                   notices_failed, last_progress_at, request_id,
                   public_error_code
            FROM ingestion_runs
            {where}
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def get_ingestion_run_public(job_id: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, job_type, status, started_at, finished_at,
                   current_page, pages_scanned, links_discovered,
                   notices_imported, notices_unchanged, notices_skipped,
                   notices_failed, last_progress_at, request_id,
                   public_error_code
            FROM ingestion_runs WHERE id = ?
            """,
            [job_id],
        ).fetchone()


def list_job_events_page(job_id: str, limit: int = 200, after=None):
    where = "job_id = ?"
    params = [job_id]
    if after:
        where += (
            " AND (occurred_at > ? OR (occurred_at = ? AND id > ?))"
        )
        params.extend(
            [after["occurred_at"], after["occurred_at"], after["id"]]
        )
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT id, job_id, occurred_at, level, event_type,
                   public_message, current_page, request_id
            FROM job_events
            WHERE {where}
            ORDER BY occurred_at ASC, id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()


def delete_expired_operations_batch(
    *, jobs_before: str, events_before: str, batch_size: int = 500
) -> tuple[int, int]:
    """Delete one bounded batch of sanitized operations history."""
    limit = min(max(batch_size, 1), 500)
    with get_conn() as conn:
        events_result = conn.execute(
            """
            DELETE FROM job_events
            WHERE id IN (
                SELECT id FROM job_events
                WHERE occurred_at < ?
                ORDER BY occurred_at ASC, id ASC
                LIMIT ?
            )
            """,
            [events_before, limit],
        )
        jobs_result = conn.execute(
            """
            DELETE FROM ingestion_runs
            WHERE id IN (
                SELECT id FROM ingestion_runs
                WHERE started_at < ? AND status IN ('completed', 'failed')
                ORDER BY started_at ASC, id ASC
                LIMIT ?
            )
            """,
            [jobs_before, limit],
        )
    return jobs_result.rows_affected, events_result.rows_affected


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
    build_id = active_build_id()
    if build_id:
        return build_locality_counts(build_id)
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
    build_id = active_build_id()
    if build_id:
        rows = build_cooccurrences(build_id)
        return [
            {
                **row,
                "last_seen": row["last_observed_on"],
            }
            for row in rows
        ]
    with get_conn() as conn:
        return conn.execute("SELECT * FROM cooccurrences").fetchall()


def total_notice_count() -> int:
    build_id = active_build_id()
    if build_id:
        public = get_model_build_public(build_id)
        return public["notice_count"]
    return count_official_notices()


def distinct_locality_count() -> int:
    build_id = active_build_id()
    if build_id:
        public = get_model_build_public(build_id)
        return public["locality_count"]
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
