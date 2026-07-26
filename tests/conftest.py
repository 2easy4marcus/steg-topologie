# tests/conftest.py
import os
import tempfile
import pytest

from app import backfill_official, db


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


@pytest.fixture(autouse=True)
def _reset_backfill_status():
    """backfill_official._status is module-level global state shared across
    the whole test session (unlike the DB, it isn't reset by isolated_db) --
    reset it before every test, in every test file, so no test's backfill
    run leaks into another's assertions."""
    backfill_official._status.update({
        "running": False, "page": 0, "new_links_this_page": 0, "imported": 0,
        "total_in_db": 0, "started_at": None, "finished_at": None, "error": None,
    })
    yield
