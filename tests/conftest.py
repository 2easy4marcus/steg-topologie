# tests/conftest.py
import os
import tempfile
import pytest

from app import db


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
