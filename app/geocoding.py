"""
Lazy, cached geocoding of locality names via Nominatim (OpenStreetMap).

Respects Nominatim's usage policy: 1 request/second max, required
User-Agent header. Results are cached forever in db.localities -- a name
is only re-queried if it's still NULL (previous lookup failed/no match).
"""

import threading
import time

import requests

from . import db

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "tunisia-outage-tracker/1.0 (contact: m.jellibi@enlyze.com)"
_last_request_time = 0.0
_rate_limit_lock = threading.Lock()


def geocode_locality(name: str):
    """Return (lat, lng) floats, or (None, None) on no-match/failure."""
    global _last_request_time
    with _rate_limit_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={"q": f"{name}, Tunisia", "format": "json", "limit": 1},
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            _last_request_time = time.time()
            resp.raise_for_status()
            results = resp.json()
            if not results:
                return None, None
            return float(results[0]["lat"]), float(results[0]["lon"])
        except (requests.exceptions.RequestException, KeyError, ValueError, TypeError):
            return None, None


def ensure_geocoded(name: str):
    """Geocode `name` only if it doesn't already have coordinates."""
    row = db.get_locality(name)
    if row is None:
        db.upsert_locality(name)
        row = db.get_locality(name)
    if row["lat"] is not None:
        return
    lat, lng = geocode_locality(name)
    if lat is not None:
        db.set_locality_coords(name, lat, lng, geocoded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def geocode_all_pending():
    """Called from the recluster job: geocode every locality still missing
    coordinates. Safe to call repeatedly -- only touches NULL rows."""
    for row in db.list_ungeocoded_localities():
        ensure_geocoded(row["name"])
