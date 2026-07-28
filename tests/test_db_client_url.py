# tests/test_db_client_url.py
"""
Guards the DB URL handling against a fix that made things worse.

The problem being guarded: libsql_client's HTTP transport cannot run
transactions (TRANSACTIONS_NOT_SUPPORTED), which breaks
evidence_pipeline.persist_notice() in production. The tempting fix is to
rewrite an https:// TURSO_DATABASE_URL to wss:// so the WebSocket transport
(which does support transactions) is used instead.

That fix was tried and took production down completely: this Turso host
rejects the Hrana WebSocket handshake with "400 Invalid response status", so
the very first init_db() at app startup raised WSServerHandshakeError and
uvicorn exited with status 3. A hosted DB whose transactions fail is bad; an
app that cannot boot is worse.

So: the configured URL must be passed through untouched. The real transaction
problem needs a different solution (see the note in db._client_kwargs).
"""
from app import db


<<<<<<< Updated upstream
def test_client_url_maps_https_to_websocket():
    # The scheme Turso's dashboard hands out -- must not reach the client
    # as-is, or transactions are silently unavailable.
    assert db.client_url("https://db-org.turso.io") == "libsql://db-org.turso.io"


def test_client_url_maps_http_to_websocket():
    assert db.client_url("http://127.0.0.1:8080") == "libsql://127.0.0.1:8080"
=======
def test_client_kwargs_passes_configured_url_through_unchanged(monkeypatch):
    monkeypatch.setattr(db, "DB_URL", "https://db-org.turso.io")
    monkeypatch.setattr(db, "AUTH_TOKEN", None)
    assert db._client_kwargs()["url"] == "https://db-org.turso.io"


def test_client_kwargs_does_not_rewrite_to_websocket(monkeypatch):
    """Explicitly pins the regression: no ws:// or wss:// rewriting."""
    for configured in ("https://db-org.turso.io", "http://127.0.0.1:8080"):
        monkeypatch.setattr(db, "DB_URL", configured)
        monkeypatch.setattr(db, "AUTH_TOKEN", None)
        url = db._client_kwargs()["url"]
        assert url == configured
        assert not url.startswith("ws")
>>>>>>> Stashed changes


def test_client_kwargs_includes_auth_token_only_when_set(monkeypatch):
    monkeypatch.setattr(db, "DB_URL", "libsql://db-org.turso.io")
    monkeypatch.setattr(db, "AUTH_TOKEN", "secret-token")
    assert db._client_kwargs() == {
        "url": "libsql://db-org.turso.io",
        "auth_token": "secret-token",
    }

<<<<<<< Updated upstream

def test_client_url_preserves_path_and_query():
    assert db.client_url("https://host/db?mode=rw") == "libsql://host/db?mode=rw"


def test_get_transaction_actually_commits_under_test_url():
    """The transaction path works end-to-end (guards against a normalization
    change that produces a URL the client rejects outright)."""
    db.upsert_official_notice({
        "id": "tx-1", "title": "t", "url": "http://x", "region": "جهة الشمال",
        "notice_date": "23/07/2026", "notice_time": "18:00",
        "time_window_sentence": "s", "zones": [], "subregions": [],
        "raw_text": "raw", "scraped_at": "2026-07-23T18:00:00+00:00",
    })
    assert db.official_notice_exists("tx-1") is True
=======
    monkeypatch.setattr(db, "AUTH_TOKEN", None)
    assert db._client_kwargs() == {"url": "libsql://db-org.turso.io"}
>>>>>>> Stashed changes
