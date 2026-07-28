# tests/test_db_client_url.py
"""
Regression tests for a production-only failure the rest of the suite is
structurally blind to.

Tests run against `file:` URLs (see conftest.isolated_db), and the file
transport supports transactions, so nothing here ever exercised the hosted
scheme. In production TURSO_DATABASE_URL was an https:// URL, whose libsql
transport does NOT support transactions -- so every evidence-pipeline notice
import died with TRANSACTIONS_NOT_SUPPORTED while the whole suite stayed
green. These tests pin the URL normalization that prevents that.
"""
from app import db


def test_client_url_maps_https_to_websocket():
    # The scheme Turso's dashboard hands out -- must not reach the client
    # as-is, or transactions are silently unavailable.
    assert db.client_url("https://db-org.turso.io") == "libsql://db-org.turso.io"


def test_client_url_maps_http_to_websocket():
    assert db.client_url("http://127.0.0.1:8080") == "libsql://127.0.0.1:8080"


def test_client_url_leaves_transaction_capable_schemes_untouched():
    for url in (
        "file:/tmp/tracker.db",
        "libsql://db-org.turso.io",
        "ws://127.0.0.1:8080",
        "wss://db-org.turso.io",
    ):
        assert db.client_url(url) == url


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
