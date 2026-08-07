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

from .model.config import CONFIG

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
        from time import perf_counter
        from .request_metrics import record_db_call
        started = perf_counter()
        try:
            result = _Result(self._client.execute(sql, list(params or [])))
        except Exception:
            record_db_call((perf_counter() - started) * 1000, failed=True)
            raise
        else:
            record_db_call((perf_counter() - started) * 1000, failed=False)
            return result

    def batch(self, statements):
        return [_Result(result) for result in self._client.batch(statements)]


# NOTE: there is deliberately no transaction helper here, and none should be
# added. libsql_client 0.3.1's HTTP transport refuses interactive transactions
# outright (LibsqlError: TRANSACTIONS_NOT_SUPPORTED) and production's
# TURSO_DATABASE_URL is https://. Its WebSocket transport does support them,
# but this Turso host rejects the Hrana WebSocket handshake with
# "400 Invalid response status", so switching schemes takes the app down at
# startup instead (see _client_kwargs and tests/test_db_client_url.py).
# Every guarded write below therefore folds its guard into a single atomic SQL
# statement and decides the outcome from rows_affected -- the same pattern
# acquire_lock/heartbeat_lock/release_lock already use.


def _client_kwargs() -> dict:
    # NOTE: do NOT rewrite an https:// DB_URL to wss:// here. It looks like an
    # easy way to get transaction support (libsql_client's HTTP transport
    # raises TRANSACTIONS_NOT_SUPPORTED, its WebSocket transport doesn't), but
    # this Turso host rejects the Hrana WebSocket handshake outright with
    # "400 Invalid response status", which takes the whole app down at
    # startup on the very first init_db() call. Use the URL as configured.
    kwargs = {"url": DB_URL}
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


def init_db():
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
    from . import migrations

    migrations.apply_all()


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
    # The "snapshot belongs to notice" guard is the SELECT ... WHERE EXISTS
    # feeding the upsert: no matching snapshot means no source row, so the
    # upsert touches nothing and rows_affected is 0.
    with get_conn() as conn:
        result = conn.execute(
            """
            INSERT INTO notice_state(
                notice_id, latest_snapshot_id, active_parse_id, updated_at
            )
            SELECT ?, ?, NULL, ?
            WHERE EXISTS (
                SELECT 1 FROM notice_snapshots
                WHERE snapshot_id = ? AND notice_id = ?
            )
            ON CONFLICT(notice_id) DO UPDATE SET
                latest_snapshot_id = excluded.latest_snapshot_id,
                updated_at = excluded.updated_at
            """,
            [notice_id, snapshot_id, updated_at, snapshot_id, notice_id],
        )
        if result.rows_affected == 0:
            raise ValueError("snapshot does not belong to notice")


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
    with get_conn() as conn:
        # The "parse_id already exists" guard is the WHERE NOT EXISTS: a
        # duplicate parse_id yields no source row, so rows_affected is 0 and
        # we report the duplicate exactly as before, without writing anything.
        result = conn.execute(
            """
            INSERT INTO notice_parses(
                parse_id, snapshot_id, notice_id, title, notice_date_raw,
                notice_date_iso, parser_version, normalization_version,
                parse_status, parse_warnings, parsed_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM notice_parses WHERE parse_id = ?
            )
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
                parse_id,
            ],
        )
        if result.rows_affected == 0:
            return False
        if evidence.localities:
            # One multi-row INSERT so the locality set is all-or-nothing.
            rows = ", ".join(["(?, ?, ?, ?, ?, ?)"] * len(evidence.localities))
            params: list = []
            for locality in evidence.localities:
                params.extend(
                    [
                        parse_id,
                        locality.ordinal,
                        locality.raw_name,
                        locality.canonical_name,
                        locality.subregion_name,
                        locality.scope_ordinal,
                    ]
                )
            conn.execute(
                f"""
                INSERT INTO notice_localities(
                    parse_id, ordinal, raw_name, canonical_name,
                    subregion_name, scope_ordinal
                ) VALUES {rows}
                """,
                params,
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
    with get_conn() as conn:
        # Both guards (parse belongs to the notice's latest snapshot, and the
        # parse is eligible) live in the UPDATE's WHERE clause, so the success
        # path is one atomic statement.
        result = conn.execute(
            """
            UPDATE notice_state
            SET active_parse_id = ?, updated_at = ?
            WHERE notice_id = ?
              AND EXISTS (
                  SELECT 1 FROM notice_parses p
                  WHERE p.parse_id = ?
                    AND p.notice_id = notice_state.notice_id
                    AND p.snapshot_id = notice_state.latest_snapshot_id
                    AND (
                        p.parse_status = 'ok'
                        OR (
                            p.parse_status = 'warning'
                            AND (
                                SELECT COUNT(DISTINCT nl.canonical_name)
                                FROM notice_localities nl
                                WHERE nl.parse_id = p.parse_id
                            ) >= 2
                        )
                    )
              )
            """,
            [parse_id, activated_at, notice_id, parse_id],
        )
        if result.rows_affected > 0:
            return
        # Failure path only: a single rows_affected == 0 cannot say which
        # guard failed, so re-read (read-only) to raise the same error the
        # transactional version would have raised.
        row = conn.execute(
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
        # Unreachable unless a concurrent writer moved notice_state between
        # the guarded UPDATE and this re-read. Refuse rather than retry.
        raise ValueError("parse does not belong to latest snapshot")


def create_model_build(
    build_id: str, created_at: str, canonical_build_id: str | None = None
) -> None:
    """Open a build, pinning the canonical geography it will resolve through.

    The pin is taken once, here, and never re-read: a canonical import that
    activates mid-build must not change what this build's geography says, for
    the same reason a parser activation must not change its source population.
    None means no canonical import was active, and the build's geographic
    confidence stays unmeasured rather than being borrowed from a later one.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO model_builds(
                build_id, status, created_at, canonical_build_id
            ) VALUES (?, 'building', ?, ?)
            """,
            [build_id, created_at, canonical_build_id],
        )


def active_canonical_build_id():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_build_id FROM canonical_state WHERE state_id = 1"
        ).fetchone()
        return row["active_build_id"] if row else None


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
    # The "build must be completed" guard is the WHERE EXISTS feeding the
    # upsert: a missing or non-completed build yields no source row.
    with get_conn() as conn:
        result = conn.execute(
            """
            INSERT INTO model_state(singleton_id, active_build_id)
            SELECT 1, ?
            WHERE EXISTS (
                SELECT 1 FROM model_builds
                WHERE build_id = ? AND status = 'completed'
            )
            ON CONFLICT(singleton_id) DO UPDATE SET
                active_build_id = excluded.active_build_id
            """,
            [build_id, build_id],
        )
        if result.rows_affected == 0:
            raise ValueError("model build must be completed before activation")


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
    config_version: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cluster_runs(
                run_id, build_id, algorithm_version, status, started_at,
                config_version
            ) VALUES (?, ?, ?, 'running', ?, ?)
            """,
            [run_id, build_id, algorithm_version, started_at, config_version],
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
    with get_conn() as conn:
        # All four guards (run completed, run's build is the active build, a
        # completed validation run exists, a published decision is stored)
        # live in the WHERE EXISTS feeding the upsert, so the success path is
        # one atomic statement.
        result = conn.execute(
            """
            INSERT INTO cluster_state(singleton_id, active_cluster_run_id)
            SELECT 1, ?
            WHERE EXISTS (
                SELECT 1 FROM cluster_runs cr
                WHERE cr.run_id = ?
                  AND cr.status = 'completed'
                  AND cr.build_id = (
                      SELECT ms.active_build_id FROM model_state ms
                      WHERE ms.singleton_id = 1
                  )
                  AND EXISTS (
                      SELECT 1 FROM cluster_validation_runs cvr
                      WHERE cvr.run_id = cr.run_id
                        AND cvr.status = 'completed'
                  )
                  AND EXISTS (
                      SELECT 1 FROM publication_decisions pd
                      WHERE pd.product_type = 'cluster_run'
                        AND pd.product_id = cr.run_id
                        AND pd.decision = 'published'
                  )
            )
            ON CONFLICT(singleton_id) DO UPDATE SET
                active_cluster_run_id = excluded.active_cluster_run_id
            """,
            [run_id, run_id],
        )
        if result.rows_affected > 0:
            return
        # Failure path only: re-read (read-only) to tell the guards apart and
        # raise exactly the error the transactional version raised. The first
        # two messages are load-bearing -- callers and tests match on them.
        row = conn.execute(
            """
            SELECT cr.status, cr.build_id, ms.active_build_id,
                   (SELECT cvr.status FROM cluster_validation_runs cvr
                    WHERE cvr.run_id = cr.run_id) AS validation_status,
                   (SELECT pd.decision FROM publication_decisions pd
                    WHERE pd.product_type = 'cluster_run'
                      AND pd.product_id = cr.run_id) AS decision
            FROM cluster_runs cr
            LEFT JOIN model_state ms ON ms.singleton_id = 1
            WHERE cr.run_id = ?
            """,
            [run_id],
        ).fetchone()
        if row is None or row["status"] != "completed":
            raise ValueError("cluster run must be completed before activation")
        if row["build_id"] != row["active_build_id"]:
            # Reached both when the build genuinely differs and (rarely) when
            # a concurrent writer changed model_state; either way the run is
            # not provably tied to the active build, so refuse.
            raise ValueError("cluster run does not reference active build")
        if row["validation_status"] != "completed":
            raise ValueError(
                "cluster run requires a completed validation run"
            )
        raise ValueError("cluster run is not published")


def reserve_cluster_ids(count: int) -> int:
    """Reserve `count` cluster ids and return the first one.

    One atomic statement, so two concurrent runs can never be handed the same
    id. Ids are never reconstructed from MAX(cluster_id): retention deletes
    cluster_members rows, and a maximum recomputed afterwards would reissue
    ids that already belonged to something else. Reservations the caller does
    not use are burned rather than returned.
    """
    with get_conn() as conn:
        if count <= 0:
            row = conn.execute(
                "SELECT next_cluster_id FROM cluster_id_allocator "
                "WHERE singleton_id = 1"
            ).fetchone()
            return row["next_cluster_id"]
        row = conn.execute(
            """
            UPDATE cluster_id_allocator
            SET next_cluster_id = next_cluster_id + ?
            WHERE singleton_id = 1
            RETURNING next_cluster_id
            """,
            [count],
        ).fetchone()
    # RETURNING yields the post-update value; the reserved block starts before.
    return row["next_cluster_id"] - count


def write_cluster_lineage(
    run_id: str, previous_run_id: str, rows: list
) -> None:
    if not rows:
        return
    placeholders = ", ".join(["(?, ?, ?, ?, ?, ?)"] * len(rows))
    params: list = []
    for row in rows:
        params.extend(
            [
                run_id,
                row["cluster_id"],
                previous_run_id,
                row["previous_cluster_id"],
                row["jaccard_similarity"],
                row["role"],
            ]
        )
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO cluster_lineage(
                run_id, cluster_id, previous_run_id, previous_cluster_id,
                jaccard_similarity, role
            ) VALUES {placeholders}
            """,
            params,
        )


def cluster_lineage(run_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM cluster_lineage WHERE run_id = ?
            ORDER BY cluster_id, previous_cluster_id
            """,
            [run_id],
        ).fetchall()


def cluster_run_memberships(run_id: str) -> dict:
    """{cluster_id: {locality, ...}} for one versioned run."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT locality, cluster_id FROM cluster_members WHERE run_id = ?",
            [run_id],
        ).fetchall()
    memberships: dict = {}
    for row in rows:
        memberships.setdefault(row["cluster_id"], set()).add(row["locality"])
    return memberships


def record_validation_run(
    run_id: str,
    report,
    *,
    status: str,
    evaluated_at: str,
    split_count: int = 0,
    merge_count: int = 0,
) -> None:
    from .model.validation import report_json

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cluster_validation_runs(
                run_id, build_id, config_version, algorithm_version,
                validation_version, random_seed, bootstrap_runs, status,
                mean_membership_agreement, held_out_edge_recall,
                raw_cooccurrence_baseline, geography_baseline,
                service_unit_baseline, largest_notice_removed_agreement,
                config_sensitivity_agreement, split_count, merge_count,
                report_json, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                report.build_id,
                report.config_version,
                report.algorithm_version,
                report.validation_version,
                report.random_seed,
                report.bootstrap_runs,
                status,
                report.mean_membership_agreement,
                report.held_out_edge_recall,
                report.raw_cooccurrence_baseline,
                report.geography_baseline,
                report.service_unit_baseline,
                report.largest_notice_removed_agreement,
                report.config_sensitivity_agreement,
                split_count,
                merge_count,
                report_json(report),
                evaluated_at,
            ],
        )


def validation_run(run_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM cluster_validation_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()


def active_cluster_run_id():
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT active_cluster_run_id FROM cluster_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        return row["active_cluster_run_id"] if row else None


def pin_build_snapshot(build_id: str) -> None:
    """Freeze this build's source population before anything measures it.

    Everything downstream -- scoped pairs, marginals, counts, readiness --
    reads only `build_notice_parses` and `build_locality_observations`. A
    parser activation that lands after this call therefore cannot change what
    the build observed, which is what stops a build from ever reporting a
    pair count larger than one of its own marginals.

    Idempotent: a retried build clears its own pinned rows first, so
    re-running converges on the same population instead of failing on a
    primary-key conflict.
    """
    with get_conn() as conn:
        for table in (
            "build_locality_observations",
            "build_notice_parses",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE build_id = ?", [build_id]
            )
        conn.execute(
            """
            INSERT INTO build_notice_parses(
                build_id, notice_id, parse_id, outage_date, parse_status
            )
            SELECT ?, ns.notice_id, ns.active_parse_id, np.notice_date_iso,
                   np.parse_status
            FROM notice_state ns
            JOIN notice_parses np ON np.parse_id = ns.active_parse_id
            """,
            [build_id],
        )
        # A locality's scope is the source table cell it came from. Cells are
        # identified by ordinal, never by heading text, so duplicate headings
        # stay distinct and an unheaded cell is still its own scope. Only a
        # parse with no cell structure at all falls back to whole-notice
        # scoping.
        #
        # GROUP BY, not SELECT DISTINCT: scope_name is display-only and is not
        # part of the primary key, so a parse written before scope ordinals
        # existed -- every row NULL ordinal, but different headings -- can
        # carry one canonical name under two headings. DISTINCT would keep
        # both rows and the insert would abort on the primary key. MIN picks
        # one heading deterministically for display.
        conn.execute(
            """
            INSERT INTO build_locality_observations(
                build_id, notice_id, scope_kind, scope_ordinal, scope_name,
                canonical_name
            )
            SELECT bnp.build_id, bnp.notice_id,
                   CASE WHEN nl.scope_ordinal IS NULL
                        THEN 'notice_fallback'
                        ELSE 'subregion' END AS scope_kind,
                   COALESCE(nl.scope_ordinal, 0) AS scope_ordinal,
                   MIN(NULLIF(TRIM(COALESCE(nl.subregion_name, '')), '')),
                   nl.canonical_name
            FROM build_notice_parses bnp
            JOIN notice_localities nl ON nl.parse_id = bnp.parse_id
            WHERE bnp.build_id = ?
            GROUP BY bnp.build_id, bnp.notice_id, scope_kind, scope_ordinal,
                     nl.canonical_name
            """,
            [build_id],
        )


def populate_scoped_observations(build_id: str, config) -> None:
    """Pair localities only inside a shared scope, with separate confidences.

    Confidence components are stored side by side and never blended.

    `geographic_confidence` resolves through the build's pinned canonical
    geography and nowhere else. Three distinct outcomes, deliberately:
    NULL when either locality has no measured service unit under that pin (or
    the build has no pin at all) -- unmeasured; 0.0 when both were measured
    and sit in different service units -- measured disagreement; otherwise the
    weaker of the two spatial confidences. Defaulting the unmeasured case to
    1.0 would fabricate evidence the build does not have, and the graph reads
    NULL as "no bonus" rather than "no agreement".
    """
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM build_pair_observations WHERE build_id = ?",
            [build_id],
        )
        conn.execute(
            """
            INSERT INTO build_pair_observations(
                build_id, notice_id, outage_date, scope_kind, scope_ordinal,
                scope_name, locality_a, locality_b, parse_confidence,
                scope_confidence, canonicalization_confidence,
                temporal_confidence, geographic_confidence, config_version
            )
            SELECT a.build_id, a.notice_id, bnp.outage_date, a.scope_kind,
                   a.scope_ordinal, a.scope_name,
                   a.canonical_name, b.canonical_name,
                   CASE WHEN bnp.parse_status = 'ok' THEN ? ELSE ? END,
                   CASE WHEN a.scope_kind = 'subregion' THEN ? ELSE ? END,
                   ?,
                   CASE WHEN bnp.outage_date IS NULL THEN NULL ELSE 1.0 END,
                   CASE
                       WHEN ca.service_unit_id IS NULL
                            OR cb.service_unit_id IS NULL THEN NULL
                       WHEN ca.service_unit_id = cb.service_unit_id
                           THEN MIN(ca.spatial_confidence,
                                    cb.spatial_confidence)
                       ELSE 0.0
                   END,
                   ?
            FROM build_locality_observations a
            JOIN build_locality_observations b
              ON b.build_id = a.build_id
             AND b.notice_id = a.notice_id
             AND b.scope_kind = a.scope_kind
             AND b.scope_ordinal = a.scope_ordinal
             AND a.canonical_name < b.canonical_name
            JOIN build_notice_parses bnp
              ON bnp.build_id = a.build_id
             AND bnp.notice_id = a.notice_id
            -- LEFT, so a build row that does not exist yet leaves geography
            -- unmeasured rather than silently emptying the whole population.
            LEFT JOIN model_builds mb ON mb.build_id = a.build_id
            LEFT JOIN locality_context ca
              ON ca.canonical_build_id = mb.canonical_build_id
             AND ca.locality = a.canonical_name
            LEFT JOIN locality_context cb
              ON cb.canonical_build_id = mb.canonical_build_id
             AND cb.locality = b.canonical_name
            WHERE a.build_id = ?
            """,
            [
                config.ok_parse_confidence,
                config.warning_parse_confidence,
                config.subregion_scope_confidence,
                config.notice_fallback_confidence,
                config.canonicalization_confidence,
                config.version,
                build_id,
            ],
        )


def build_scoped_cooccurrences(build_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM build_pair_observations
            WHERE build_id = ?
            ORDER BY locality_a, locality_b, scope_kind, scope_ordinal
            """,
            [build_id],
        ).fetchall()


def populate_model_build(build_id: str) -> None:
    """Aggregate the build's marginals and edges from its pinned snapshot."""
    with get_conn() as conn:
        for table in ("build_cooccurrences", "build_locality_counts"):
            conn.execute(
                f"DELETE FROM {table} WHERE build_id = ?", [build_id]
            )
        conn.execute(
            """
            INSERT INTO build_locality_counts(
                build_id, locality, notice_count
            )
            SELECT ?, canonical_name, COUNT(DISTINCT notice_id)
            FROM (
                SELECT DISTINCT notice_id, canonical_name
                FROM build_locality_observations
                WHERE build_id = ?
            )
            GROUP BY canonical_name
            """,
            [build_id, build_id],
        )
        # distinct_date_count ignores NULL outage dates by design: a notice
        # whose date could not be parsed is not evidence of a distinct outage
        # day, so such a pair reports 0 distinct dates and cannot satisfy
        # config.min_edge_distinct_dates.
        conn.execute(
            """
            INSERT INTO build_cooccurrences(
                build_id, locality_a, locality_b, notice_count,
                distinct_date_count, first_observed_on, last_observed_on
            )
            SELECT ?, locality_a, locality_b, COUNT(DISTINCT notice_id),
                   COUNT(DISTINCT outage_date),
                   MIN(outage_date), MAX(outage_date)
            FROM build_pair_observations
            WHERE build_id = ?
            GROUP BY locality_a, locality_b
            """,
            [build_id, build_id],
        )


def model_build_counts(build_id: str):
    with get_conn() as conn:
        notice_count = conn.execute(
            """
            SELECT COUNT(DISTINCT notice_id) AS c
            FROM build_locality_observations
            WHERE build_id = ?
            """,
            [build_id],
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


def build_locality_geography(build_id: str) -> dict:
    """{locality: {latitude, longitude, service_unit_id, spatial_confidence}}.

    Resolved through the build's pinned canonical build, so two runs against
    the same build always see the same geography. A build with no pin gets an
    empty mapping rather than the currently-active import's rows. Coordinates
    come from the geocoded `localities` table and may be NULL; the caller
    decides whether an unpositioned locality can bound anything.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT lc.locality, l.lat AS latitude, l.lng AS longitude,
                   lc.service_unit_id, lc.spatial_confidence
            FROM model_builds mb
            JOIN locality_context lc
              ON lc.canonical_build_id = mb.canonical_build_id
            LEFT JOIN localities l ON l.name = lc.locality
            WHERE mb.build_id = ?
            ORDER BY lc.locality
            """,
            [build_id],
        ).fetchall()
    return {
        row["locality"]: {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "service_unit_id": row["service_unit_id"],
            "spatial_confidence": row["spatial_confidence"],
        }
        for row in rows
    }


def cluster_independent_dates(build_id: str, localities) -> int:
    """Distinct outage dates on which two of `localities` co-occurred.

    The measured evidence behind a cluster, counted from the build's own
    pinned observations. Fewer than two is one event, not a repeated
    relationship, and no candidate ranking may be derived from it.
    """
    names = sorted(localities)
    if len(names) < 2:
        return 0
    placeholders = ", ".join(["?"] * len(names))
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT COUNT(DISTINCT outage_date) AS c
            FROM build_pair_observations
            WHERE build_id = ?
              AND locality_a IN ({placeholders})
              AND locality_b IN ({placeholders})
            """,
            [build_id, *names, *names],
        ).fetchone()["c"]


def record_candidate_run(
    run_id: str,
    *,
    cluster_run_id: str,
    build_id: str,
    source_snapshot_id: str,
    config_version: str,
    scoring_version: str,
    radius_km: float,
    status: str,
    created_at: str,
    completed_at: str | None = None,
    public_error_code: str | None = None,
) -> None:
    """Private, experimental. No public route reads this table.

    `radius_km` is required because a scoring version alone does not identify
    the run: the radius is variable within a version, and the pilot exists to
    vary it.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO asset_candidate_runs(
                run_id, cluster_run_id, build_id, source_snapshot_id,
                config_version, scoring_version, radius_km, status,
                created_at, completed_at, public_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                cluster_run_id,
                build_id,
                source_snapshot_id,
                config_version,
                scoring_version,
                radius_km,
                status,
                created_at,
                completed_at,
                public_error_code,
            ],
        )


def write_candidate_scores(
    run_id: str, cluster_id: int, candidates: list, sensitivity: dict
) -> None:
    with get_conn() as conn:
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO asset_candidate_scores(
                    run_id, cluster_id, asset_id, rank, score,
                    component_json, sensitivity_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    cluster_id,
                    candidate.asset_id,
                    candidate.rank,
                    candidate.score,
                    json.dumps(candidate.components, sort_keys=True),
                    json.dumps(sensitivity[candidate.asset_id]),
                ],
            )


def candidate_scores(run_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM asset_candidate_scores WHERE run_id = ?
            ORDER BY cluster_id, rank
            """,
            [run_id],
        ).fetchall()


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
    # Every metric derives from the build's own pinned, scoped observations.
    # A notice counts as valid only if it produced at least one *scoped* pair,
    # and influence is measured over scoped observations, so two localities
    # named in different table cells no longer inflate either number the way a
    # whole-notice Cartesian join did.
    with get_conn() as conn:
        notice_metrics = conn.execute(
            """
            WITH valid AS (
                SELECT DISTINCT bpo.notice_id, bnp.parse_status,
                       bnp.outage_date
                FROM build_pair_observations bpo
                JOIN build_notice_parses bnp
                  ON bnp.build_id = bpo.build_id
                 AND bnp.notice_id = bpo.notice_id
                WHERE bpo.build_id = ?
            )
            SELECT COUNT(*) AS valid_notices,
                   COUNT(DISTINCT outage_date) AS distinct_dates,
                   COALESCE(AVG(CASE WHEN parse_status = 'ok'
                                     THEN 1.0 ELSE 0.0 END), 0) AS ok_ratio
            FROM valid
            """,
            [build_id],
        ).fetchone()
        build_metrics = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM build_locality_counts
                 WHERE build_id = ?) AS unique_localities,
                (SELECT COUNT(*) FROM build_cooccurrences
                 WHERE build_id = ?
                   AND distinct_date_count >= ?) AS repeated_pairs
            """,
            [build_id, build_id, CONFIG.min_edge_distinct_dates],
        ).fetchone()
        share = conn.execute(
            """
            WITH pair_counts AS (
                SELECT notice_id, COUNT(*) AS pair_count
                FROM build_pair_observations
                WHERE build_id = ?
                GROUP BY notice_id
            )
            SELECT CASE WHEN COALESCE(SUM(pair_count), 0) = 0 THEN 0.0
                        ELSE CAST(MAX(pair_count) AS REAL) / SUM(pair_count)
                   END AS largest_share
            FROM pair_counts
            """,
            [build_id],
        ).fetchone()
    return {
        "valid_notices": notice_metrics["valid_notices"],
        "distinct_outage_dates": notice_metrics["distinct_dates"],
        "unique_localities": build_metrics["unique_localities"],
        "repeated_pairs": build_metrics["repeated_pairs"],
        "active_ok_ratio": notice_metrics["ok_ratio"],
        "largest_notice_pair_share": share["largest_share"] or 0.0,
    }


def _gate_rows(build_id, readiness, config_version, evaluated_at):
    for section in (readiness.model_quality, readiness.operational_health):
        for signal in section.signals:
            if signal.passed:
                reason = f"{signal.key}_pass"
            elif signal.current is None:
                reason = f"{signal.key}_not_measured"
            elif signal.operator == "<=":
                reason = f"{signal.key}_above_maximum"
            else:
                reason = f"{signal.key}_below_minimum"
            yield [
                build_id,
                signal.key,
                "pass" if signal.passed else "fail",
                signal.current,
                signal.required,
                reason,
                config_version,
                evaluated_at,
            ]


def record_quality_gates(
    build_id: str, readiness, config_version: str, evaluated_at: str
) -> None:
    """Store one row per readiness signal, tagged with its configuration."""
    rows = list(_gate_rows(build_id, readiness, config_version, evaluated_at))
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM quality_gate_results WHERE build_id = ?", [build_id]
        )
        if not rows:
            return
        placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?)"] * len(rows))
        conn.execute(
            f"""
            INSERT INTO quality_gate_results(
                build_id, gate_key, outcome, measured_value, required_value,
                reason_code, config_version, evaluated_at
            ) VALUES {placeholders}
            """,
            [value for row in rows for value in row],
        )


def quality_gate_results(build_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM quality_gate_results
            WHERE build_id = ? ORDER BY gate_key
            """,
            [build_id],
        ).fetchall()


_DECISION_REASONS = {
    "published": "all_gates_pass",
    "experimental": "operational_gates_failed",
    "blocked": "model_gates_failed",
}


def record_publication_decision(
    product_type: str,
    product_id: str,
    *,
    build_id: str | None,
    decision: str,
    config_version: str,
    decided_at: str,
    reason_code: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO publication_decisions(
                product_type, product_id, build_id, decision, reason_code,
                config_version, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_type, product_id) DO UPDATE SET
                build_id = excluded.build_id,
                decision = excluded.decision,
                reason_code = excluded.reason_code,
                config_version = excluded.config_version,
                decided_at = excluded.decided_at
            """,
            [
                product_type,
                product_id,
                build_id,
                decision,
                reason_code or _DECISION_REASONS[decision],
                config_version,
                decided_at,
            ],
        )


def publication_decision(product_type: str, product_id: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM publication_decisions
            WHERE product_type = ? AND product_id = ?
            """,
            [product_type, product_id],
        ).fetchone()


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


def build_edge_evidence(build_id: str) -> list:
    """Per-pair graph inputs, averaged from the build's scoped observations.

    Averaging is two-level -- within a notice, then across notices -- because
    one notice can contribute the same pair from several table cells. A flat
    average over observation rows would weight that notice by its cell count
    and let one wide table pull an edge's reliability down, which is the same
    largest-notice influence readiness is required to exclude.

    geographic_confidence stays NULL until canonical geography is pinned to a
    build; the graph reads NULL as "unmeasured" and applies no bonus.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            WITH per_notice AS (
                SELECT locality_a, locality_b, notice_id, outage_date,
                       AVG(parse_confidence) AS parse_confidence,
                       AVG(scope_confidence) AS scope_confidence,
                       AVG(canonicalization_confidence)
                           AS canonicalization_confidence,
                       AVG(geographic_confidence) AS geographic_confidence
                FROM build_pair_observations
                WHERE build_id = ?
                GROUP BY locality_a, locality_b, notice_id, outage_date
            )
            SELECT locality_a, locality_b,
                   COUNT(*) AS notice_count,
                   COUNT(DISTINCT outage_date) AS distinct_date_count,
                   AVG(parse_confidence) AS mean_parse_confidence,
                   AVG(scope_confidence) AS mean_scope_confidence,
                   AVG(canonicalization_confidence)
                       AS mean_canonicalization_confidence,
                   AVG(geographic_confidence) AS geographic_confidence
            FROM per_notice
            GROUP BY locality_a, locality_b
            ORDER BY locality_a, locality_b
            """,
            [build_id],
        ).fetchall()


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
        # Provenance comes from the build's own scoped observations, not from
        # live notice state. A notice that merely mentions both localities in
        # different table cells is not evidence for this edge, and a parser
        # activation after the build must not change what an already-pinned
        # build claims to have seen.
        rows = conn.execute(
            """
            SELECT bpo.notice_id, np.title, bpo.outage_date AS notice_date_iso,
                   nsnap.source_url, bpo.scope_name AS subregion_name
            FROM build_pair_observations bpo
            JOIN build_notice_parses bnp
              ON bnp.build_id = bpo.build_id
             AND bnp.notice_id = bpo.notice_id
            JOIN notice_parses np ON np.parse_id = bnp.parse_id
            JOIN notice_snapshots nsnap
              ON nsnap.snapshot_id = np.snapshot_id
            WHERE bpo.build_id = ?
              AND bpo.locality_a = ? AND bpo.locality_b = ?
            ORDER BY bpo.outage_date DESC, bpo.notice_id DESC
            """,
            [build_id, a, b],
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
    with get_conn() as conn:
        # The audit row is written FIRST, from a single statement that both
        # enforces all three guards and reads the pre-update active_parse_id
        # straight out of notice_state -- so from_parse_id can never be a
        # stale value read in an earlier round trip. The guards are:
        #   * FROM notice_state WHERE notice_id = ?  -> state must exist
        #   * EXISTS(... p.notice_id = ?)            -> parse owned by notice
        #   * ... AND p.parse_status <> 'failed'     -> parse not failed
        # No source row means rows_affected == 0 and nothing is written.
        result = conn.execute(
            """
            INSERT INTO notice_rollbacks(
                id, notice_id, from_parse_id, to_parse_id, reason,
                rolled_back_at
            )
            SELECT ?, ?, ns.active_parse_id, ?, ?, ?
            FROM notice_state ns
            WHERE ns.notice_id = ?
              AND EXISTS (
                  SELECT 1 FROM notice_parses p
                  WHERE p.parse_id = ?
                    AND p.notice_id = ns.notice_id
                    AND p.parse_status <> 'failed'
              )
            """,
            [
                rollback_id,
                notice_id,
                parse_id,
                reason,
                rolled_back_at,
                notice_id,
                parse_id,
            ],
        )
        if result.rows_affected == 0:
            # Failure path only: re-read (read-only) to tell the three guards
            # apart, in the same order the transactional version checked them.
            parse = conn.execute(
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
            raise ValueError("notice state not found")
        # Guards already passed atomically above, so this needs no guard of
        # its own beyond addressing the right notice.
        conn.execute(
            """
            UPDATE notice_state SET active_parse_id = ?, updated_at = ?
            WHERE notice_id = ?
            """,
            [parse_id, rolled_back_at, notice_id],
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
