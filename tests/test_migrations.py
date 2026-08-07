import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest

from app import db, migrations


class BusyError(Exception):
    """Stand-in for the driver's SQLITE_BUSY, independent of driver version."""

    def __init__(self, message="SQLITE: database is locked", code="SQLITE"):
        super().__init__(message)
        self.code = code


class FakeBackoffClock:
    """Virtual clock for migrations' retry loop.

    _retry_on_busy budgets itself in seconds, which is the point of the fix --
    but a test must not actually sleep for them. Time only advances when the
    code under test sleeps, so multi-second budgets cost no wall time and the
    assertions are deterministic regardless of the jitter that gets rolled.
    """

    def __init__(self):
        self.now = 0.0
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds

    def install(self, monkeypatch):
        monkeypatch.setattr(migrations, "_monotonic", self.monotonic)
        monkeypatch.setattr(migrations, "_sleep", self.sleep)
        return self


@pytest.fixture
def fake_backoff_clock(monkeypatch):
    return FakeBackoffClock().install(monkeypatch)


class ConnSpy:
    """Wraps db.get_conn so a test can watch or break one call.

    `on_batch(statements)` / `on_execute(sql, params)` run before the real
    call and may raise to simulate a busy driver; `batch_errors` records what
    the caller's batch actually failed with.
    """

    def __init__(self, monkeypatch):
        self.real_get_conn = db.get_conn
        self.on_batch = None
        self.on_execute = None
        self.batch_errors = []
        spy = self

        class Wrapper:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=None):
                if spy.on_execute is not None:
                    spy.on_execute(sql, params)
                return self._conn.execute(sql, params)

            def batch(self, statements):
                if spy.on_batch is not None:
                    spy.on_batch(statements)
                try:
                    return self._conn.batch(statements)
                except Exception as error:
                    spy.batch_errors.append(error)
                    raise

        class Context:
            def __enter__(self):
                self._context = spy.real_get_conn()
                return Wrapper(self._context.__enter__())

            def __exit__(self, *args):
                return self._context.__exit__(*args)

        monkeypatch.setattr(db, "get_conn", Context)

    def claim(self, version, checksum):
        """Record `version` from a separate connection, as a winner would."""
        with self.real_get_conn() as conn:
            conn.execute(
                """
                INSERT INTO schema_migrations(
                    version, checksum_sha256, applied_at
                ) VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                [version, checksum],
            )


@pytest.fixture
def conn_spy(monkeypatch):
    return ConnSpy(monkeypatch)


@pytest.fixture
def empty_migration_db(isolated_db, monkeypatch, tmp_path):
    database_path = tmp_path / "migrations.db"
    monkeypatch.setattr(db, "DB_URL", f"file:{database_path}")
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", migration_dir)
    return migration_dir


def _versions():
    with db.get_conn() as conn:
        return conn.execute(
            """
            SELECT version, checksum_sha256
            FROM schema_migrations
            ORDER BY version
            """
        ).fetchall()


def test_migrations_are_ordered_idempotent_and_record_checksums(
    empty_migration_db: Path,
):
    (empty_migration_db / "0002_second.sql").write_text(
        "INSERT INTO migration_events(value) VALUES ('second');"
    )
    (empty_migration_db / "0001_first.sql").write_text(
        """
        CREATE TABLE migration_events(value TEXT NOT NULL);
        INSERT INTO migration_events(value) VALUES ('first');
        """
    )

    migrations.apply_all()
    migrations.apply_all()

    with db.get_conn() as conn:
        events = conn.execute(
            "SELECT value FROM migration_events ORDER BY rowid"
        ).fetchall()
    assert [row["value"] for row in events] == ["first", "second"]
    rows = _versions()
    assert [row["version"] for row in rows] == ["0001", "0002"]
    assert all(len(row["checksum_sha256"]) == 64 for row in rows)


def test_changed_applied_migration_is_a_hard_error(empty_migration_db: Path):
    migration = empty_migration_db / "0001_example.sql"
    migration.write_text("CREATE TABLE original(id INTEGER PRIMARY KEY);")
    migrations.apply_all()
    migration.write_text("CREATE TABLE changed(id INTEGER PRIMARY KEY);")

    with pytest.raises(
        migrations.MigrationChecksumError, match="checksum mismatch.*0001"
    ):
        migrations.apply_all()


def test_failing_migration_rolls_back_schema_and_version(
    empty_migration_db: Path,
):
    (empty_migration_db / "0001_broken.sql").write_text(
        """
        CREATE TABLE must_rollback(value TEXT NOT NULL);
        INSERT INTO table_that_does_not_exist(value) VALUES ('failure');
        """
    )

    with pytest.raises(Exception, match="table_that_does_not_exist"):
        migrations.apply_all()

    with db.get_conn() as conn:
        table = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'must_rollback'
            """
        ).fetchone()
    assert table is None
    assert _versions() == []


def test_sql_parser_preserves_semicolons_in_strings_and_trigger_bodies(
    empty_migration_db: Path,
):
    (empty_migration_db / "0001_parser.sql").write_text(
        """
        CREATE TABLE messages(value TEXT NOT NULL);
        CREATE TABLE message_audit(value TEXT NOT NULL);
        CREATE TRIGGER audit_message
        AFTER INSERT ON messages
        BEGIN
            INSERT INTO message_audit(value) VALUES ('trigger;value');
            UPDATE message_audit SET value = value || ';updated';
        END;
        INSERT INTO messages(value) VALUES ('plain;value');
        """
    )

    migrations.apply_all()

    with db.get_conn() as conn:
        message = conn.execute("SELECT value FROM messages").fetchone()
        audit = conn.execute("SELECT value FROM message_audit").fetchone()
    assert message["value"] == "plain;value"
    assert audit["value"] == "trigger;value;updated"


def test_sql_parser_handles_multiple_statements_on_one_line(
    empty_migration_db: Path,
):
    (empty_migration_db / "0001_single_line.sql").write_text(
        "CREATE TABLE values_log(value TEXT NOT NULL); "
        "INSERT INTO values_log(value) VALUES ('first;value'); "
        "INSERT INTO values_log(value) VALUES ('second');"
    )

    migrations.apply_all()

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT value FROM values_log ORDER BY rowid"
        ).fetchall()
    assert [row["value"] for row in rows] == ["first;value", "second"]


@pytest.mark.parametrize(
    "comment",
    [
        "-- trailing line comment",
        "/* trailing block comment */",
    ],
)
def test_sql_parser_allows_trailing_comments(
    empty_migration_db: Path, comment: str
):
    (empty_migration_db / "0001_comment.sql").write_text(
        f"CREATE TABLE comment_safe(id INTEGER PRIMARY KEY); {comment}"
    )

    migrations.apply_all()

    with db.get_conn() as conn:
        table = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'comment_safe'
            """
        ).fetchone()
    assert table == {"name": "comment_safe"}


def test_migration_bytes_are_read_once_for_checksum_and_sql(
    empty_migration_db: Path, monkeypatch
):
    migration = empty_migration_db / "0001_single_read.sql"
    migration.write_text("CREATE TABLE single_read(id INTEGER PRIMARY KEY);")
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text
    reads = 0

    def counted_read_bytes(path):
        nonlocal reads
        if path == migration:
            reads += 1
        return real_read_bytes(path)

    def forbidden_read_text(path, *args, **kwargs):
        if path == migration:
            raise AssertionError("migration content was read a second time")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    monkeypatch.setattr(Path, "read_text", forbidden_read_text)

    migrations.apply_all()

    assert reads == 1


def test_claim_insert_leads_the_batch(
    empty_migration_db: Path, monkeypatch
):
    """Claim-first is an optimization, and this pins its shape only.

    It is deliberately NOT the exactly-once test: exactly-once comes from
    conn.batch() being atomic, so moving this INSERT to the end of the batch
    would keep the concurrency test below green (the loser's whole batch
    rolls back either way). What claim-first buys is that the loser aborts on
    statement 0 without first redoing the migration body, and fails with an
    unambiguous conflict on schema_migrations -- and *that* is asserted from
    the losing caller's observed error in
    test_reconcile_absorbs_busy_reads_after_a_lost_race.
    """
    (empty_migration_db / "0001_claim_first.sql").write_text(
        "CREATE TABLE claimed(id INTEGER PRIMARY KEY);"
    )
    real_get_conn = db.get_conn
    captured = []

    class RecordingConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=None):
            return self._conn.execute(sql, params)

        def batch(self, statements):
            captured.extend(statements)
            return self._conn.batch(statements)

    class RecordingContext:
        def __enter__(self):
            self._context = real_get_conn()
            return RecordingConn(self._context.__enter__())

        def __exit__(self, *args):
            return self._context.__exit__(*args)

    monkeypatch.setattr(db, "get_conn", RecordingContext)

    migrations.apply_all()

    claim_sql, claim_params = captured[0]
    assert " ".join(claim_sql.split()).startswith(
        "INSERT INTO schema_migrations("
    )
    assert "SELECT" not in claim_sql.upper()
    assert claim_params[0] == "0001"
    assert captured[1].lstrip().startswith("CREATE TABLE claimed")


def test_two_concurrent_callers_execute_migration_body_once(
    empty_migration_db: Path, monkeypatch
):
    (empty_migration_db / "0001_concurrent.sql").write_text(
        """
        CREATE TABLE migration_effects(value TEXT NOT NULL);
        INSERT INTO migration_effects(value) VALUES ('applied');
        CREATE TABLE contention_hold(value INTEGER NOT NULL);
        WITH RECURSIVE numbers(value) AS (
            VALUES(1)
            UNION ALL
            SELECT value + 1 FROM numbers WHERE value < 20000
        )
        INSERT INTO contention_hold(value) SELECT value FROM numbers;
        """
    )
    real_get_conn = db.get_conn
    both_ready = Barrier(2)
    role_lock = Lock()
    first_batches = 0
    connection_count = 0

    class SimultaneousConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=None):
            return self._conn.execute(sql, params)

        def batch(self, statements):
            nonlocal first_batches
            with role_lock:
                wait_for_peer = first_batches < 2
                first_batches += 1
            if wait_for_peer:
                both_ready.wait(timeout=5)
            return self._conn.batch(statements)

    class SimultaneousContext:
        def __enter__(self):
            nonlocal connection_count
            with role_lock:
                connection_count += 1
            self._context = real_get_conn()
            return SimultaneousConn(self._context.__enter__())

        def __exit__(self, *args):
            return self._context.__exit__(*args)

    monkeypatch.setattr(db, "get_conn", SimultaneousContext)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(migrations.apply_all) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    with real_get_conn() as conn:
        effects = conn.execute("SELECT value FROM migration_effects").fetchall()
        versions = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    assert effects == [{"value": "applied"}]
    assert versions == [{"version": "0001"}]
    assert connection_count >= 2


# --------------------------------------------------------------------------
# Backoff budget (H1). No DB, no real sleeping: the fake clock only advances
# when the code under test sleeps, so a multi-second budget costs no wall time.
# --------------------------------------------------------------------------


def _always_busy():
    def operation():
        raise BusyError()

    return operation


def test_busy_retry_spends_a_multi_second_budget_with_a_capped_delay(
    fake_backoff_clock,
):
    with pytest.raises(BusyError):
        migrations._retry_on_busy(_always_busy())

    # Every sleep is clamped to the remaining budget, so a run that gives up
    # has consumed the budget exactly -- a sub-second budget cannot pass this.
    assert fake_backoff_clock.now == pytest.approx(
        migrations.BUSY_RETRY_BUDGET_SECONDS
    )
    assert migrations.BUSY_RETRY_BUDGET_SECONDS >= 5.0
    assert max(fake_backoff_clock.delays) <= migrations.BUSY_MAX_DELAY_SECONDS
    # The delay actually reaches the cap (jitter keeps it in 0.5x-1.0x of it)
    # instead of crawling at the initial delay for the whole budget.
    assert max(fake_backoff_clock.delays) >= (
        migrations.BUSY_MAX_DELAY_SECONDS * 0.5
    )
    # Exponential growth, so seconds of budget cost tens of attempts, not
    # thousands of pointless round trips at the initial delay.
    assert len(fake_backoff_clock.delays) < 30


def test_busy_retry_delay_doubles_and_is_jittered(
    fake_backoff_clock, monkeypatch
):
    # Pin the jitter roll to its floor: delay == nominal * (0.5 + 0.0).
    monkeypatch.setattr(migrations.random, "random", lambda: 0.0)

    with pytest.raises(BusyError):
        migrations._retry_on_busy(_always_busy(), budget_seconds=1.0)

    initial = migrations.BUSY_INITIAL_DELAY_SECONDS
    assert fake_backoff_clock.delays[:4] == pytest.approx(
        [initial * 0.5, initial, initial * 2, initial * 4]
    )


def test_reconcile_budget_outlives_the_batch_budget():
    assert (
        migrations.RECONCILE_RETRY_BUDGET_SECONDS
        > migrations.BUSY_RETRY_BUDGET_SECONDS
    )


# --------------------------------------------------------------------------
# Reconciliation after a lost race (M1).
# --------------------------------------------------------------------------


def _lose_the_race(conn_spy, version, checksum, *, busy_reads):
    """Claim `version` from another connection just before the caller's batch.

    Returns a mutable state dict; `state["busy_reads"]` counts how many of the
    reconciliation reads were answered with SQLITE_BUSY.
    """
    state = {"claimed": False, "busy_reads": 0}

    def on_batch(_statements):
        if not state["claimed"]:
            state["claimed"] = True
            conn_spy.claim(version, checksum)

    def on_execute(sql, _params):
        if state["claimed"] and "FROM schema_migrations" in sql:
            if state["busy_reads"] < busy_reads:
                state["busy_reads"] += 1
                raise BusyError()

    conn_spy.on_batch = on_batch
    conn_spy.on_execute = on_execute
    return state


def test_reconcile_absorbs_busy_reads_after_a_lost_race(
    empty_migration_db: Path, conn_spy, fake_backoff_clock
):
    migration = empty_migration_db / "0001_reconcile.sql"
    migration.write_text("CREATE TABLE reconciled(id INTEGER PRIMARY KEY);")
    checksum = migrations._checksum(migration.read_bytes())
    state = _lose_the_race(conn_spy, "0001", checksum, busy_reads=3)

    migrations.apply_all()  # the busy read must not become the caller's error

    assert state["busy_reads"] == 3
    assert fake_backoff_clock.delays  # it retried rather than propagating
    # Claim-first means the loser trips on statement 0, and says so.
    assert "schema_migrations" in str(conn_spy.batch_errors[0])
    assert [row["version"] for row in _versions()] == ["0001"]
    with db.get_conn() as conn:
        table = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'reconciled'
            """
        ).fetchone()
    assert table is None  # the loser's batch rolled back in full


def test_reconcile_gives_up_after_its_budget_and_reraises_the_batch_error(
    empty_migration_db: Path, conn_spy, fake_backoff_clock
):
    """The reconciliation read stays busy for the whole RECONCILE budget.

    Unlike the absorbs-busy-reads test above, this busy condition never
    clears, so _retry_on_busy inside _applied_checksum eventually exhausts
    RECONCILE_RETRY_BUDGET_SECONDS and raises. apply_all() must still surface
    the original batch failure, not that busy error -- the reconciliation
    read exists only to explain the batch failure away, and on its own
    failure the caller's real error must win.
    """
    migration = empty_migration_db / "0001_persistent_busy.sql"
    migration.write_text("CREATE TABLE persistent(id INTEGER PRIMARY KEY);")
    checksum = migrations._checksum(migration.read_bytes())
    _lose_the_race(conn_spy, "0001", checksum, busy_reads=10**9)

    with pytest.raises(Exception) as exc_info:
        migrations.apply_all()

    assert not isinstance(exc_info.value, BusyError)
    assert "schema_migrations" in str(exc_info.value)
    assert fake_backoff_clock.delays  # it retried before giving up


def test_reconcile_still_raises_on_a_recorded_checksum_mismatch(
    empty_migration_db: Path, conn_spy, fake_backoff_clock
):
    migration = empty_migration_db / "0001_mismatch.sql"
    migration.write_text("CREATE TABLE mismatched(id INTEGER PRIMARY KEY);")
    _lose_the_race(conn_spy, "0001", "f" * 64, busy_reads=1)

    with pytest.raises(
        migrations.MigrationChecksumError, match="checksum mismatch.*0001"
    ):
        migrations.apply_all()


# --------------------------------------------------------------------------
# Parser and discovery guards (M2, L1-L4).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "BEGIN",
        "BEGIN IMMEDIATE",
        "COMMIT",
        "END",
        "ROLLBACK",
        "SAVEPOINT restore_point",
        "RELEASE restore_point",
    ],
)
def test_transaction_control_statements_are_rejected(
    empty_migration_db: Path, statement: str
):
    # conn.batch() supplies the transaction; an inline COMMIT would commit the
    # version claim before the migration body ran. Note the trigger-body
    # BEGIN...END in test_sql_parser_preserves_semicolons_in_strings_and_
    # trigger_bodies must stay accepted -- it is one statement, not three.
    (empty_migration_db / "0001_transaction.sql").write_text(
        f"CREATE TABLE guarded(id INTEGER PRIMARY KEY);\n{statement};"
    )

    with pytest.raises(ValueError, match="transaction control"):
        migrations.apply_all()

    assert _versions() == []


def test_stray_semicolons_do_not_reach_the_batch(
    empty_migration_db: Path, conn_spy
):
    (empty_migration_db / "0001_stray.sql").write_text(
        "CREATE TABLE stray(id INTEGER PRIMARY KEY);;;\n"
        "-- a comment, then a stray terminator\n"
        ";\n"
        "INSERT INTO stray(id) VALUES (1);"
    )
    batched = []
    conn_spy.on_batch = batched.extend

    migrations.apply_all()

    bodies = [
        statement if isinstance(statement, str) else statement[0]
        for statement in batched
    ]
    assert not [body for body in bodies if body.strip().endswith(";;")]
    assert [body.split()[0].upper() for body in bodies] == [
        "INSERT",  # the version claim
        "CREATE",
        "INSERT",
    ]


@pytest.mark.parametrize(
    ("error", "is_busy"),
    [
        # Generic code, so the message is all we have: trust it.
        (BusyError(), True),
        # The driver named a specific non-busy condition; believe it over a
        # message that merely happens to mention a lock.
        (BusyError("no such column: database is locked", "SQLITE_ERROR"), False),
        (BusyError("that table is gone", "SQLITE_BUSY"), True),
        # No code at all: message only, and "locked" alone is not busy.
        (sqlite3.OperationalError("no such column: locked"), False),
    ],
)
def test_is_busy_error_trusts_a_specific_driver_code_over_the_message(
    error: BaseException, is_busy: bool
):
    assert migrations._is_busy_error(error) is is_busy


def test_migration_filename_must_be_four_digits_then_underscore(
    empty_migration_db: Path,
):
    (empty_migration_db / "0001-dash.sql").write_text("SELECT 1;")

    with pytest.raises(ValueError, match="NNNN_name.sql"):
        migrations.apply_all()


def test_duplicate_migration_version_is_rejected(empty_migration_db: Path):
    (empty_migration_db / "0001_first.sql").write_text("SELECT 1;")
    (empty_migration_db / "0001_also_first.sql").write_text("SELECT 2;")

    with pytest.raises(ValueError, match="duplicate migration version: 0001"):
        migrations.apply_all()


def test_migrations_run_in_parsed_version_order_not_directory_order(
    empty_migration_db: Path, monkeypatch
):
    (empty_migration_db / "0010_second.sql").write_text(
        "INSERT INTO ordering_log(value) VALUES ('second');"
    )
    (empty_migration_db / "0002_first.sql").write_text(
        """
        CREATE TABLE ordering_log(value TEXT NOT NULL);
        INSERT INTO ordering_log(value) VALUES ('first');
        """
    )
    # Directory/glob order happens to be alphabetical (== numeric here) on
    # this filesystem, which would let an unsorted _discover_migrations pass
    # by accident. Force glob to hand back the wrong order so only the
    # explicit sort-by-parsed-version can produce the right one.
    real_glob = Path.glob

    def reversed_glob(self, pattern):
        return reversed(list(real_glob(self, pattern)))

    monkeypatch.setattr(Path, "glob", reversed_glob)

    migrations.apply_all()

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT value FROM ordering_log ORDER BY rowid"
        ).fetchall()
    assert [row["value"] for row in rows] == ["first", "second"]
    assert [row["version"] for row in _versions()] == ["0002", "0010"]


def test_pre_checksum_registry_table_is_a_hard_error(
    empty_migration_db: Path,
):
    """A schema_migrations from a runner that predates checksums.

    CREATE TABLE IF NOT EXISTS no-ops against it, so without an explicit
    column check the next SELECT dies on a bare "no such column".
    """
    with db.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations(
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
    (empty_migration_db / "0001_after_legacy.sql").write_text("SELECT 1;")

    with pytest.raises(
        migrations.MigrationRegistryError, match="checksum_sha256"
    ):
        migrations.apply_all()
