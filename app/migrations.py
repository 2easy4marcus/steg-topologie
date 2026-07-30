import functools
import hashlib
import random
import re
import sqlite3
import time
from pathlib import Path

from . import db

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Migration files are NNNN_name.sql: exactly four digits, then an underscore.
# Anything else is rejected rather than silently parsed, so the version a file
# claims and the version the duplicate guard sees can never disagree.
_VERSION_PATTERN = re.compile(r"^(\d{4})_")

# ---------------------------------------------------------------------------
# Backoff for SQLITE_BUSY.
#
# libsql_client's sqlite3 transport opens a fresh connection per call with
# timeout=0, so SQLite raises "database is locked" immediately instead of
# waiting -- the wait has to live here. Budgets are in SECONDS, not
# milliseconds: the writer we contend with is another apply_all() running the
# same migration, and a large migration over the production HTTPS/Hrana
# transport routinely holds the write lock for hundreds of milliseconds to
# seconds. A sub-100ms budget is smaller than a single production round trip,
# so the loser used to fall out of apply_all() with LibsqlError and take app
# startup (or a background job) down with it.
#
# The budget is a module-level constant so tests can shrink it (monkeypatch)
# instead of really sleeping, and _retry_on_busy also accepts an explicit
# budget_seconds for call sites that need a different one.
BUSY_RETRY_BUDGET_SECONDS = 8.0

# The reconciliation read on the batch-failure path gets a longer budget of its
# own. It runs *because* we just lost a race, i.e. exactly when the winner is
# still holding the write lock, so it is the single most likely read in the
# module to hit SQLITE_BUSY -- and it is the read whose whole job is to decide
# that a busy-looking failure was benign. Giving it the same budget as the
# batch made the reconciliation throw the very error it exists to absorb.
RECONCILE_RETRY_BUDGET_SECONDS = 20.0

BUSY_INITIAL_DELAY_SECONDS = 0.01
BUSY_MAX_DELAY_SECONDS = 1.0

# Seams so tests can drive the backoff with a fake clock and stay fast.
_sleep = time.sleep
_monotonic = time.monotonic

_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "sqlite_busy",
)

# Statements that open, close or nest a transaction. conn.batch() wraps the
# statement list in its own BEGIN/COMMIT, so a stray COMMIT inside a migration
# would commit the version claim early and destroy both the exactly-once
# guarantee and the all-or-nothing rollback of a failed migration.
_TRANSACTION_CONTROL_KEYWORDS = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
)

_LEADING_WORD_PATTERN = re.compile(r"[A-Za-z]+")

_REGISTRY_COLUMNS = ("version", "checksum_sha256", "applied_at")

_CREATE_REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
        DEFAULT CURRENT_TIMESTAMP
)
"""


class MigrationChecksumError(RuntimeError):
    pass


class MigrationRegistryError(RuntimeError):
    """schema_migrations exists but is not the table this runner expects."""


def _error_code(error: BaseException) -> str:
    """The driver's SQLite error code, if it reported one.

    libsql_client puts it on `.code` (from sqlite3's `sqlite_errorname`, so
    "SQLITE_BUSY" on Python 3.11+ and a bare "SQLITE" before that); a raw
    sqlite3 error carries `sqlite_errorname` directly on 3.11+.
    """
    for attribute in ("code", "sqlite_errorname"):
        value = str(getattr(error, attribute, "") or "").strip().upper()
        if value:
            return value
    return ""


def _is_busy_error(error: BaseException) -> bool:
    """True for SQLITE_BUSY/SQLITE_LOCKED, however the driver reported it.

    The driver's code is preferred and, when it is specific, is trusted
    outright: message sniffing on its own retries anything whose text happens
    to mention a lock, including e.g. `no such column: locked`. Message
    matching remains the fallback for the drivers/versions that only report a
    generic "SQLITE" code.
    """
    code = _error_code(error)
    if code.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return True
    if "_" in code:
        # The driver named a specific, non-busy condition. Believe it.
        return False
    text = str(error).lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def _retry_on_busy(operation, *, budget_seconds: float | None = None):
    """Run `operation`, retrying while SQLite is busy within a time budget.

    Exponential backoff with full-ish jitter (0.5x-1.5x the nominal delay) so
    two racing callers do not resynchronise on the same retry schedule. The
    loop is bounded by wall-clock budget, not by attempt count: once the budget
    is spent the busy error propagates rather than looping forever.

    Only SQLITE_BUSY is retried; every other failure propagates on the first
    attempt. Retrying is safe because each call site is a single statement or a
    single batch, and a batch that fails is rolled back in full before we see
    the error.
    """
    budget = (
        BUSY_RETRY_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    )
    started = _monotonic()
    delay = BUSY_INITIAL_DELAY_SECONDS
    while True:
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - re-raised unless busy
            if not _is_busy_error(error):
                raise
            remaining = budget - (_monotonic() - started)
            if remaining <= 0:
                raise
            _sleep(
                min(
                    delay * (0.5 + random.random()),
                    BUSY_MAX_DELAY_SECONDS,
                    remaining,
                )
            )
            delay = min(delay * 2, BUSY_MAX_DELAY_SECONDS)


def _checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _skip_comments_and_space(text: str, index: int = 0) -> tuple[int, bool]:
    """Advance past whitespace and SQL comments.

    Returns (index of the first real character, saw an unterminated `/*`).
    An unterminated block comment is reported rather than skipped: it is far
    more likely to be a truncated file than an intentional trailing comment.
    """
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
        elif text.startswith("--", index):
            newline = text.find("\n", index)
            if newline == -1:
                return length, False
            index = newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                return length, True
            index = end + 2
        else:
            break
    return index, False


def _is_comment_or_whitespace(text: str) -> bool:
    """True if `text` holds nothing but whitespace and SQL comments.

    Used on whatever follows the final `;` so a migration may end with a
    `-- line` or `/* block */ ` comment without looking truncated.
    """
    index, unterminated_block = _skip_comments_and_space(text)
    if unterminated_block:
        return False
    return index >= len(text)


def _leading_keyword(statement: str) -> str:
    """The first SQL word of `statement`, ignoring leading comments."""
    index, _ = _skip_comments_and_space(statement)
    match = _LEADING_WORD_PATTERN.match(statement, index)
    return match.group(0).upper() if match else ""


def _parse_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for character in sql:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if not statement:
                continue
            index, _ = _skip_comments_and_space(statement)
            if statement[index:].strip() == ";":
                # A stray `;` (e.g. "CREATE TABLE a(x);;;"). Harmless, but it
                # would occupy a batch slot and turn any later error message
                # into nonsense, so drop it.
                continue
            keyword = _leading_keyword(statement)
            if keyword in _TRANSACTION_CONTROL_KEYWORDS:
                raise ValueError(
                    "migration must not contain transaction control "
                    f"statements (found {keyword}); conn.batch() supplies the "
                    "transaction, and an inline COMMIT would commit the "
                    "version claim before the migration body has run"
                )
            statements.append(statement)
    if buffer.strip() and not _is_comment_or_whitespace(buffer):
        raise ValueError("migration contains an incomplete SQL statement")
    return statements


def _discover_migrations() -> list[tuple[str, Path]]:
    """Every migration file, validated, ordered by parsed version number."""
    discovered: dict[str, Path] = {}
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _VERSION_PATTERN.match(path.stem)
        if match is None:
            raise ValueError(
                "migration filename must be NNNN_name.sql (exactly four "
                f"digits then an underscore): {path.name}"
            )
        version = match.group(1)
        if version in discovered:
            raise ValueError(f"duplicate migration version: {version}")
        discovered[version] = path
    return [
        (version, discovered[version])
        for version in sorted(discovered, key=int)
    ]


def _ensure_registry_table(conn) -> None:
    _retry_on_busy(functools.partial(conn.execute, _CREATE_REGISTRY_SQL))
    # CREATE TABLE IF NOT EXISTS silently no-ops against a schema_migrations
    # left behind by an older runner that had no checksum_sha256 column; the
    # next SELECT would then fail with a bare "no such column". Say so here.
    columns = {
        row["name"]
        for row in _retry_on_busy(
            functools.partial(
                conn.execute, "PRAGMA table_info(schema_migrations)"
            )
        ).fetchall()
    }
    missing = [name for name in _REGISTRY_COLUMNS if name not in columns]
    if missing:
        raise MigrationRegistryError(
            "existing schema_migrations table is missing column(s) "
            f"{', '.join(missing)}; it was created by an older migration "
            "runner. Recreate or migrate that table before running "
            "apply_all()."
        )


def _applied_checksum(
    conn, version: str, *, budget_seconds: float | None = None
) -> str | None:
    row = _retry_on_busy(
        functools.partial(
            conn.execute,
            """
            SELECT checksum_sha256
            FROM schema_migrations
            WHERE version = ?
            """,
            [version],
        ),
        budget_seconds=budget_seconds,
    ).fetchone()
    return row["checksum_sha256"] if row else None


def _require_matching_checksum(
    version: str, expected: str, actual: str | None
) -> bool:
    if actual is None:
        return False
    if actual != expected:
        raise MigrationChecksumError(
            f"migration checksum mismatch for version {version}"
        )
    return True


def _reconciles_with_recorded_checksum(conn, version: str, expected: str):
    """Did a concurrent apply_all() already record this exact migration?

    Failure path only. Uses the longer reconciliation budget, because the
    contention that made the batch fail is still in progress. A busy error
    *here* is never allowed to become the caller's error: it says nothing
    about the migration, and letting it out would replace the real batch
    failure with a transient lock message. On any read failure we return False
    so the caller re-raises the original batch exception instead.

    A recorded-but-different checksum is still a hard error and propagates.
    """
    try:
        applied = _applied_checksum(
            conn, version, budget_seconds=RECONCILE_RETRY_BUDGET_SECONDS
        )
    except MigrationChecksumError:
        raise
    except Exception:  # noqa: BLE001 - original batch error is the real one
        return False
    return _require_matching_checksum(version, expected, applied)


def apply_all() -> None:
    with db.get_conn() as conn:
        _ensure_registry_table(conn)
        for version, path in _discover_migrations():
            # Read the file exactly once: the checksum must describe the very
            # bytes we are about to execute. Reading again for the SQL would
            # open a TOCTOU window where an edit between the two reads gets
            # recorded under the checksum of the previous content.
            content = path.read_bytes()
            checksum = _checksum(content)
            if _require_matching_checksum(
                version, checksum, _applied_checksum(conn, version)
            ):
                continue

            # The claim INSERT leads the batch as an OPTIMIZATION, not as the
            # correctness mechanism. Exactly-once comes from conn.batch()
            # being atomic: whichever statement a losing racer trips over,
            # its whole batch (claim included) rolls back, so the migration
            # body can never be committed twice. Putting the claim first just
            # means the loser aborts on the PRIMARY KEY conflict without
            # first doing the body's work, and fails with a clear conflict
            # rather than an arbitrary "table already exists".
            statements = [
                (
                    """
                    INSERT INTO schema_migrations(
                        version, checksum_sha256, applied_at
                    )
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    """,
                    [version, checksum],
                ),
                *_parse_statements(content.decode("utf-8")),
            ]
            try:
                _retry_on_busy(functools.partial(conn.batch, statements))
            except Exception:
                # Either a concurrent apply_all() claimed this version first,
                # or the migration itself is broken. Exactly-once comes from
                # conn.batch() being atomic -- a losing racer's batch is rolled
                # back in full, whatever statement it failed on -- so the
                # recorded checksum is what tells the two cases apart.
                if _reconciles_with_recorded_checksum(conn, version, checksum):
                    continue
                raise
            _require_matching_checksum(
                version, checksum, _applied_checksum(conn, version)
            )
