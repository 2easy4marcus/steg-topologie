# tests/test_db_client_url.py
"""
Guards the DB URL handling against a "fix" that made things worse.

The underlying problem: libsql_client's HTTP transport cannot run
transactions (TRANSACTIONS_NOT_SUPPORTED), which breaks
evidence_pipeline.persist_notice() against hosted Turso. The tempting fix is
to rewrite an https:// TURSO_DATABASE_URL to a WebSocket scheme (wss:// or
libsql://, which libsql_client resolves to a WebSocket connection) since that
transport does support transactions.

That was tried and took production down completely: the Turso host rejects
the Hrana WebSocket handshake with "400 Invalid response status", so the very
first init_db() at app startup raised WSServerHandshakeError and uvicorn
exited with status 3. A hosted DB whose transactions fail is bad; an app that
cannot boot is worse.

So the configured URL is passed through untouched. The real transaction
problem needs a different solution -- folding each guard into a single atomic
SQL statement so no interactive transaction is needed at all.
"""
from app import db


def test_client_kwargs_passes_configured_url_through_unchanged(monkeypatch):
    monkeypatch.setattr(db, "DB_URL", "https://db-org.turso.io")
    monkeypatch.setattr(db, "AUTH_TOKEN", None)
    assert db._client_kwargs()["url"] == "https://db-org.turso.io"


def test_client_kwargs_does_not_rewrite_to_websocket_scheme(monkeypatch):
    """Pins the regression: no ws://, wss:// or libsql:// rewriting."""
    for configured in ("https://db-org.turso.io", "http://127.0.0.1:8080"):
        monkeypatch.setattr(db, "DB_URL", configured)
        monkeypatch.setattr(db, "AUTH_TOKEN", None)
        url = db._client_kwargs()["url"]
        assert url == configured
        assert not url.startswith(("ws", "libsql:"))


def test_client_kwargs_includes_auth_token_only_when_set(monkeypatch):
    monkeypatch.setattr(db, "DB_URL", "libsql://db-org.turso.io")
    monkeypatch.setattr(db, "AUTH_TOKEN", "secret-token")
    assert db._client_kwargs() == {
        "url": "libsql://db-org.turso.io",
        "auth_token": "secret-token",
    }

    monkeypatch.setattr(db, "AUTH_TOKEN", None)
    assert db._client_kwargs() == {"url": "libsql://db-org.turso.io"}
