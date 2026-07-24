# tests/test_geocoding.py
from unittest.mock import patch, MagicMock

from app import db, geocoding


def _fake_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def test_geocode_locality_returns_coords_on_success():
    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1", "lon": "9.2"}])
        lat, lng = geocoding.geocode_locality("Dekka")
    assert lat == 34.1
    assert lng == 9.2


def test_geocode_locality_returns_none_on_no_match():
    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([])
        lat, lng = geocoding.geocode_locality("Nonexistent Place")
    assert (lat, lng) == (None, None)


def test_geocode_locality_returns_none_on_request_error():
    import requests
    with patch("app.geocoding.requests.get", side_effect=requests.exceptions.ConnectionError):
        lat, lng = geocoding.geocode_locality("Dekka")
    assert (lat, lng) == (None, None)


def test_ensure_geocoded_caches_and_skips_second_network_call():
    db.upsert_locality("Dekka")
    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1", "lon": "9.2"}])
        geocoding.ensure_geocoded("Dekka")
        geocoding.ensure_geocoded("Dekka")  # already has lat/lng now, must not call again
    assert mock_get.call_count == 1
    row = db.get_locality("Dekka")
    assert row["lat"] == 34.1


def test_geocode_locality_returns_none_on_malformed_response():
    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1"}])  # missing 'lon'
        lat, lng = geocoding.geocode_locality("Dekka")
    assert (lat, lng) == (None, None)


def test_ensure_geocoded_retries_after_failed_attempt():
    db.upsert_locality("Dekka")
    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([])  # no match -- fails first time
        geocoding.ensure_geocoded("Dekka")
    row = db.get_locality("Dekka")
    assert row["lat"] is None  # still not geocoded after failure

    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1", "lon": "9.2"}])  # succeeds this time
        geocoding.ensure_geocoded("Dekka")
    assert mock_get.call_count == 1  # DID retry -- made a new network call since lat was still NULL
    row = db.get_locality("Dekka")
    assert row["lat"] == 34.1


def test_geocode_all_pending_geocodes_every_ungeocoded_locality():
    db.upsert_locality("Dekka")
    db.upsert_locality("Tozeur")
    with patch("app.geocoding.requests.get") as mock_get:
        mock_get.return_value = _fake_response([{"lat": "34.1", "lon": "9.2"}])
        geocoding.geocode_all_pending()
    assert mock_get.call_count == 2
    assert db.get_locality("Dekka")["lat"] == 34.1
    assert db.get_locality("Tozeur")["lat"] == 34.1
