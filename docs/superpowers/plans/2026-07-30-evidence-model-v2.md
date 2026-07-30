# Evidence Model V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, observable evidence pipeline that produces validated national outage clusters and a private Sfax/Kerkennah asset-candidate pilot without presenting inferred topology as fact.

**Architecture:** Preserve the existing immutable notice snapshots, active evidence builds, job locks, and last-known-good activation model. Add registered source artifacts, canonical geography/service units, subregion-scoped observations, versioned model configuration, stable cluster lineage, and a private topology ranking layer. Docker uses local file-backed libSQL by default; Turso remains opt-in.

**Tech Stack:** Python 3.13, FastAPI, Pydantic 2, libSQL/Turso, NetworkX, python-louvain, Shapely, PyYAML, xlrd, osmium, pytest, Docker Compose, OpenAPI, Postman/Newman.

---

## Delivery boundaries

This plan is executed in order on branch `feat/evidence-model-v2`.

1. Foundation and visibility.
2. Canonical source ingestion.
3. Scoped evidence build.
4. National clustering V2.
5. Private Sfax/Kerkennah pilot.

Each boundary must leave working, testable software. The public visual redesign
and hosting changes are separate projects.

## Target file structure

```text
app/
├── api/
│   ├── docs.py                  # public/internal OpenAPI schemas
│   └── ops.py                   # protected operations routes
├── data/
│   ├── models.py                # source/canonical Pydantic contracts
│   ├── registry.py              # source manifest loading and checksums
│   ├── geography.py             # GeoJSON validation and spatial joins
│   └── steg_units.py            # district/agency XLS ingestion
├── model/
│   ├── config.py                # versioned gates and model weights
│   ├── graph.py                 # weighted graph construction
│   ├── cluster_ids.py           # stable ID and lineage matching
│   ├── validation.py            # bootstrap and temporal evaluation
│   └── candidates.py            # private asset ranking
├── topology/
│   └── osm.py                   # bounded PBF extraction and graph loading
├── migrations.py               # ordered SQL migrations
├── request_metrics.py           # bounded in-memory request metrics
├── evidence_pipeline.py         # orchestration only
├── cluster_inference.py         # compatibility façade and run orchestration
├── db.py                        # existing compatibility API
└── main.py                      # application composition
migrations/
├── 0001_source_registry.sql
├── 0002_scoped_observations.sql
├── 0003_cluster_lineage.sql
└── 0004_private_candidates.sql
scripts/
├── validate_sources.py
├── import_canonical_data.py
├── export_openapi.py
└── extract_sfax_topology.py
postman/
├── environment.example.json
└── tunisia-outage-tracker.postman_collection.json
tests/
├── fixtures/data/
├── test_migrations.py
├── test_source_registry.py
├── test_canonical_geography.py
├── test_request_metrics.py
├── test_openapi_boundaries.py
├── test_scoped_observations.py
├── test_weighted_graph_v2.py
├── test_cluster_lineage.py
├── test_model_validation.py
└── test_private_candidates.py
Dockerfile
compose.yaml
.dockerignore
.env.example
```

---

### Task 1: Reproducible Docker development environment

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.dockerignore`
- Create: `.env.example`
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Test: `tests/test_docker_contract.py`

- [ ] **Step 1: Write the failing Docker contract test**

```python
# tests/test_docker_contract.py
from pathlib import Path


def test_compose_defaults_to_local_database_and_never_embeds_secrets():
    compose = Path("compose.yaml").read_text()
    env_example = Path(".env.example").read_text()
    dockerfile = Path("Dockerfile").read_text()

    assert "tracker-data:/data" in compose
    assert "TURSO_DATABASE_URL: file:/data/tracker.db" in compose
    assert "docs/data:/app/docs/data:ro" in compose
    assert "healthcheck:" in compose
    assert "USER app" in dockerfile
    assert "CRON_SECRET=" in env_example
    assert "OPS_SECRET=" in env_example
    assert "ae0685" not in env_example
```

- [ ] **Step 2: Run the contract test and verify failure**

Run:

```bash
pytest tests/test_docker_contract.py -v
```

Expected: FAIL because `compose.yaml` and `Dockerfile` do not exist.

- [ ] **Step 3: Add pinned data dependencies**

Append to `requirements.txt`:

```text
PyYAML>=6.0,<7
shapely>=2.0,<3
xlrd>=2.0,<3
osmium>=4.0,<5
```

- [ ] **Step 4: Add the Docker image**

```dockerfile
# Dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --home /app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY static static

RUN mkdir -p /data && chown -R app:app /app /data
USER app

EXPOSE 8010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
```

- [ ] **Step 5: Add Compose profiles**

```yaml
# compose.yaml
services:
  app:
    build: .
    ports:
      - "8010:8010"
    environment:
      TURSO_DATABASE_URL: file:/data/tracker.db
      CRON_SECRET: ${CRON_SECRET:-local-cron-secret}
      OPS_SECRET: ${OPS_SECRET:-local-ops-secret}
    volumes:
      - tracker-data:/data
      - ./docs/data:/app/docs/data:ro
    healthcheck:
      test:
        ["CMD", "python", "-c",
         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/status', timeout=3)"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 20s

  test:
    build: .
    profiles: ["test"]
    command: ["pytest", "-q"]
    environment:
      TURSO_DATABASE_URL: file:/tmp/test.db
    volumes:
      - .:/app

  newman:
    image: postman/newman:alpine
    profiles: ["smoke"]
    depends_on:
      app:
        condition: service_healthy
    command:
      ["run", "/etc/newman/collection.json",
       "-e", "/etc/newman/environment.json"]
    volumes:
      - ./postman/tunisia-outage-tracker.postman_collection.json:/etc/newman/collection.json:ro
      - ./postman/environment.example.json:/etc/newman/environment.json:ro

volumes:
  tracker-data:
```

- [ ] **Step 6: Add safe local configuration**

```dotenv
# .env.example
CRON_SECRET=local-cron-secret
OPS_SECRET=local-ops-secret
# Leave unset to use Compose's local libSQL database.
TURSO_DATABASE_URL=
TURSO_AUTH_TOKEN=
```

Add to `.dockerignore`:

```text
.git
.env
**/__pycache__
*.pyc
*.db
.pytest_cache
docs/tunisia-*.osm.pbf
.superpowers
```

Add to `.gitignore`:

```text
.env
.superpowers/
docs/tunisia-*.osm.pbf
data/processed/
```

- [ ] **Step 7: Verify Docker and tests**

Run:

```bash
pytest tests/test_docker_contract.py -v
docker compose --profile test run --rm test
docker compose up -d --build
docker compose ps
```

Expected: contract test PASS, full suite PASS, and `app` becomes `healthy`.

- [ ] **Step 8: Commit**

```bash
git add Dockerfile compose.yaml .dockerignore .env.example .gitignore \
  requirements.txt tests/test_docker_contract.py
git commit -m "build: add reproducible local Docker environment"
```

---

### Task 2: Ordered database migrations and source contracts

**Files:**
- Create: `app/migrations.py`
- Create: `migrations/0001_source_registry.sql`
- Create: `app/data/__init__.py`
- Create: `app/data/models.py`
- Modify: `app/db.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_source_registry.py`

- [ ] **Step 1: Write failing migration and model tests**

```python
# tests/test_migrations.py
from app import db, migrations


def test_migrations_are_idempotent_and_recorded():
    migrations.apply_all()
    migrations.apply_all()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    versions = [row["version"] for row in rows]
    assert versions.count("0001") == 1
    assert versions == sorted(set(versions))


def test_source_registry_schema_exists():
    with db.get_conn() as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "dataset_sources", "source_artifacts", "quarantine_records",
        "administrative_areas", "service_units", "locality_context",
    } <= names
```

```python
# tests/test_source_registry.py
import pytest
from pydantic import ValidationError

from app.data.models import DatasetSource, PublicationClass


def test_unknown_license_is_private_only():
    source = DatasetSource(
        source_id="delegations",
        title="Delegations",
        owner="Unknown",
        retrieved_at="2026-07-30T00:00:00Z",
        checksum_sha256="a" * 64,
        relative_path="docs/data/delegations.geojson",
        format="geojson",
        publication_class=PublicationClass.PRIVATE_RESEARCH,
    )
    assert source.license_id is None
    assert source.publication_class == "private_research"


def test_public_source_requires_license():
    with pytest.raises(ValidationError):
        DatasetSource(
            source_id="delegations",
            title="Delegations",
            owner="Unknown",
            retrieved_at="2026-07-30T00:00:00Z",
            checksum_sha256="a" * 64,
            relative_path="docs/data/delegations.geojson",
            format="geojson",
            publication_class="public",
        )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_migrations.py tests/test_source_registry.py -v
```

Expected: FAIL because migrations and source models do not exist.

- [ ] **Step 3: Add canonical source models**

```python
# app/data/models.py
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class PublicationClass(str, Enum):
    PUBLIC = "public"
    PRIVATE_RESEARCH = "private_research"


class GateOutcome(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    QUARANTINE = "quarantine"


class DatasetSource(BaseModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    title: str
    owner: str
    source_url: str | None = None
    retrieved_at: datetime
    checksum_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    relative_path: str
    format: str
    geographic_coverage: str | None = None
    temporal_coverage: str | None = None
    license_id: str | None = None
    publication_class: PublicationClass
    refresh_policy: str = "manual"

    @model_validator(mode="after")
    def public_requires_license(self):
        if self.publication_class == PublicationClass.PUBLIC and not self.license_id:
            raise ValueError("public source requires license_id")
        return self
```

- [ ] **Step 4: Add migration 0001**

```sql
-- migrations/0001_source_registry.sql
CREATE TABLE IF NOT EXISTS dataset_sources (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    owner TEXT NOT NULL,
    source_url TEXT,
    retrieved_at TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    format TEXT NOT NULL,
    geographic_coverage TEXT,
    temporal_coverage TEXT,
    license_id TEXT,
    publication_class TEXT NOT NULL,
    refresh_policy TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    artifact_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    UNIQUE(source_id, checksum_sha256)
);

CREATE TABLE IF NOT EXISTS quarantine_records (
    quarantine_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    artifact_id TEXT,
    record_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    safe_detail TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS administrative_areas (
    area_id TEXT PRIMARY KEY,
    area_level TEXT NOT NULL,
    name_ar TEXT NOT NULL,
    name_fr TEXT,
    parent_area_id TEXT,
    geometry_wkt TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_units (
    unit_id TEXT PRIMARY KEY,
    unit_type TEXT NOT NULL,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    governorate TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    coordinate_complete INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_record_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locality_context (
    locality TEXT PRIMARY KEY,
    delegation_area_id TEXT,
    service_unit_id TEXT,
    spatial_confidence REAL NOT NULL,
    context_build_id TEXT NOT NULL
);
```

- [ ] **Step 5: Implement ordered migrations**

```python
# app/migrations.py
from pathlib import Path

from . import db

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def apply_all() -> None:
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem.split("_", 1)[0]
            if version in applied:
                continue
            for statement in path.read_text().split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                [version],
            )
```

Modify `db.init_db()`:

```python
def init_db():
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
    from . import migrations
    migrations.apply_all()
```

Add `COPY migrations migrations` after `COPY static static` in `Dockerfile`.

- [ ] **Step 6: Verify**

Run:

```bash
pytest tests/test_migrations.py tests/test_source_registry.py -v
pytest -q
```

Expected: new tests PASS and full suite PASS.

- [ ] **Step 7: Commit**

```bash
git add app/migrations.py app/data migrations/0001_source_registry.sql \
  app/db.py tests/test_migrations.py tests/test_source_registry.py
git commit -m "feat: add source registry migrations"
```

---

### Task 3: Source manifest, checksum validation, and canonical static imports

**Files:**
- Create: `docs/data/sources.yaml`
- Create: `docs/data/README.md`
- Create: `app/data/registry.py`
- Create: `app/data/geography.py`
- Create: `app/data/steg_units.py`
- Create: `scripts/validate_sources.py`
- Create: `scripts/import_canonical_data.py`
- Create: `tests/fixtures/data/delegations.geojson`
- Create: `tests/fixtures/data/steg_units.csv`
- Test: `tests/test_canonical_geography.py`
- Test: `tests/test_steg_units.py`

- [ ] **Step 1: Write failing validation tests**

```python
# tests/test_canonical_geography.py
from pathlib import Path

from app.data.geography import load_delegations


def test_geojson_loads_named_valid_tunisian_features():
    result = load_delegations(
        Path("tests/fixtures/data/delegations.geojson")
    )
    assert result.accepted[0].name_ar == "صفاقس المدينة"
    assert result.accepted[0].geometry.is_valid
    assert result.quarantined == []
```

```python
# tests/test_steg_units.py
from pathlib import Path

from app.data.steg_units import load_service_units


def test_invalid_longitude_is_quarantined_and_missing_pair_is_incomplete():
    result = load_service_units(
        Path("tests/fixtures/data/steg_units.csv")
    )
    assert [q.reason_code for q in result.quarantined] == [
        "coordinate_out_of_bounds"
    ]
    incomplete = next(x for x in result.accepted if x.name == "Kerkennah")
    assert incomplete.latitude is None
    assert incomplete.coordinate_complete is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_canonical_geography.py tests/test_steg_units.py -v
```

Expected: FAIL because loaders do not exist.

- [ ] **Step 3: Implement registry checksum loading**

```python
# app/data/registry.py
import hashlib
from pathlib import Path

import yaml

from .models import DatasetSource


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> list[DatasetSource]:
    payload = yaml.safe_load(path.read_text()) or {}
    return [DatasetSource.model_validate(row) for row in payload["sources"]]


def verify_source(source: DatasetSource, root: Path) -> None:
    artifact = root / source.relative_path
    actual = sha256_file(artifact)
    if actual != source.checksum_sha256:
        raise ValueError(f"checksum_mismatch:{source.source_id}")
```

- [ ] **Step 4: Implement geography and service-unit loaders**

```python
# app/data/geography.py
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from shapely.geometry import shape


class Delegation(BaseModel):
    delegation_id: str
    name_ar: str
    name_fr: str | None = None
    governorate_id: str | None = None
    geometry_wkt: str

    @property
    def geometry(self):
        from shapely import wkt
        return wkt.loads(self.geometry_wkt)


class QuarantinedRecord(BaseModel):
    record_key: str
    reason_code: str


@dataclass
class LoadResult:
    accepted: list
    quarantined: list[QuarantinedRecord]


def load_delegations(path: Path) -> LoadResult:
    payload = json.loads(path.read_text())
    accepted, quarantined = [], []
    for index, feature in enumerate(payload["features"]):
        props = feature["properties"]
        geom = shape(feature["geometry"])
        if geom.is_empty or not geom.is_valid:
            quarantined.append(QuarantinedRecord(
                record_key=str(index),
                reason_code="invalid_geometry",
            ))
            continue
        accepted.append(Delegation(
            delegation_id=str(props["deleg_id"]),
            name_ar=props["deleg_name"],
            name_fr=props.get("deleg_na_1"),
            governorate_id=str(props.get("gov_id") or "") or None,
            geometry_wkt=geom.wkt,
        ))
    return LoadResult(accepted, quarantined)
```

```python
# app/data/steg_units.py
import csv
from pathlib import Path

from pydantic import BaseModel
import xlrd

from .geography import LoadResult, QuarantinedRecord


class ServiceUnit(BaseModel):
    name: str
    unit_type: str
    region: str
    governorate: str
    latitude: float | None = None
    longitude: float | None = None
    coordinate_complete: bool


def _number(value):
    return float(value) if value not in (None, "") else None


def _rows(path: Path):
    if path.suffix.lower() == ".xls":
        sheet = xlrd.open_workbook(path).sheet_by_index(0)
        headers = [str(value).strip() for value in sheet.row_values(0)]
        for index in range(1, sheet.nrows):
            yield dict(zip(headers, sheet.row_values(index)))
        return
    with path.open(newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)


def load_service_units(path: Path) -> LoadResult:
    accepted, quarantined = [], []
    for row in _rows(path):
        lat, lon = _number(row.get("Lat")), _number(row.get("Lon"))
        if (lat is not None and not 30 <= lat <= 38) or (
            lon is not None and not 7 <= lon <= 12
        ):
            quarantined.append(QuarantinedRecord(
                record_key=str(row["District"]),
                reason_code="coordinate_out_of_bounds",
            ))
            continue
        complete = lat is not None and lon is not None
        accepted.append(ServiceUnit(
            name=str(row["District"]),
            unit_type=str(row["Type"]),
            region=str(row["Region"]),
            governorate=str(row["Gouvernorat"]),
            latitude=lat if complete else None,
            longitude=lon if complete else None,
            coordinate_complete=complete,
        ))
    return LoadResult(accepted, quarantined)
```

- [ ] **Step 5: Add manifest and operator documentation**

Create `docs/data/sources.yaml` with these exact local artifacts. Keep every
source private until its original URL and license are independently verified:

```yaml
sources:
  - {source_id: delegations-geojson, title: Tunisia delegations GeoJSON, owner: unverified_local_copy, retrieved_at: 2026-07-30T00:00:00Z, checksum_sha256: 52b3846c35131f108c0b403bd9241b737cd6cf013b8cfe9a036dc253a047afbb, relative_path: docs/data/delegations.geojson, format: geojson, geographic_coverage: Tunisia, publication_class: private_research, refresh_policy: manual}
  - {source_id: delegations-topojson, title: Tunisia delegations TopoJSON, owner: unverified_local_copy, retrieved_at: 2026-07-30T00:00:00Z, checksum_sha256: 79eb5f83d3d210ebfb683a6ed83502d8bca66fb3a291fe535b057a1803328e1f, relative_path: docs/data/delegations.json, format: topojson, geographic_coverage: Tunisia, publication_class: private_research, refresh_policy: manual}
  - {source_id: constituencies-geojson, title: Tunisia electoral constituencies, owner: unverified_local_copy, retrieved_at: 2026-07-30T00:00:00Z, checksum_sha256: 3dbcd110b3a77df24042cf5611b2c5d1db6041ee5613547b92288feb3a9e2829, relative_path: docs/data/tncirconscriptions.geojson, format: geojson, geographic_coverage: Tunisia, publication_class: private_research, refresh_policy: excluded_from_model}
  - {source_id: steg-service-units, title: STEG districts and agencies, owner: STEG, retrieved_at: 2026-07-30T00:00:00Z, checksum_sha256: cd131678b605ddd48c2ba7295aab170af8370e0623f9c0a71f4629f567262ac8, relative_path: docs/data/tnlistedistrictsteg.xls, format: xls, geographic_coverage: Tunisia, temporal_coverage: "2015", publication_class: private_research, refresh_policy: manual}
  - {source_id: blackout-report-2014, title: Independent commission report extract on the 2014 blackout, owner: independent_commission, retrieved_at: 2026-07-30T00:00:00Z, checksum_sha256: 44b8829801505ec2c56188cedc3eb8d94056b80737f0d33b635cdf7d616e5cad, relative_path: docs/data/extraitrapportfinaldelacommissionblackout2014.pdf, format: pdf, geographic_coverage: Tunisia, temporal_coverage: "2014-08-31", publication_class: private_research, refresh_policy: immutable}
  - {source_id: tunisia-boundary, title: Tunisia national boundary, owner: unverified_local_copy, retrieved_at: 2026-07-30T00:00:00Z, checksum_sha256: 7bf3933b892378aaf878c8e8954d084b9f9e655635c67b3fb0002e00c8a76e07, relative_path: docs/data/tunisia.geojson, format: geojson, geographic_coverage: Tunisia, publication_class: private_research, refresh_policy: manual}
  - {source_id: osm-tunisia-20260725, title: OpenStreetMap Tunisia PBF snapshot, owner: OpenStreetMap contributors, retrieved_at: 2026-07-25T00:00:00Z, checksum_sha256: 85a5ddd1faa5e093ae34337b1c1699f7d4713aaaf25b4288c04f2fd23a4007af, relative_path: docs/tunisia-260725.osm.pbf, format: osm-pbf, geographic_coverage: Tunisia, temporal_coverage: "2026-07-25", license_id: ODbL-1.0, publication_class: private_research, refresh_policy: manual}
```

`docs/data/README.md` states that these timestamps are local registration
timestamps, not proven original download dates. It documents hash verification:

```bash
python scripts/validate_sources.py docs/data/sources.yaml
```

The validation script must exit non-zero on missing files, checksum mismatch,
schema failure, or an attempted public classification without `license_id`.

- [ ] **Step 6: Persist canonical rows and spatial context**

`scripts/import_canonical_data.py` loads the verified manifest, then performs
idempotent upserts using stable IDs:

```python
def delegation_id(source_id, row):
    return f"{source_id}:delegation:{row.delegation_id}"


def service_unit_id(source_id, row):
    key = "|".join(
        [row.region, row.governorate, row.unit_type, row.name]
    )
    return f"{source_id}:service:{hashlib.sha256(key.encode()).hexdigest()[:16]}"
```

For each geocoded locality, create a Shapely `Point(lng, lat)`, select the one
delegation polygon that covers it, then select the nearest complete service
unit in the same governorate. `spatial_confidence` is `1.0` for an unambiguous
polygon match, `0.7` when only the nearest same-governorate service unit is
known, and `0.0` when neither can be assigned. Multiple covering polygons or
duplicate stable IDs are quarantined with reason codes
`ambiguous_spatial_join` and `duplicate_source_key`.

- [ ] **Step 7: Verify fixture and real-manifest behavior**

Run:

```bash
pytest tests/test_canonical_geography.py tests/test_steg_units.py -v
python scripts/validate_sources.py docs/data/sources.yaml
pytest -q
```

Expected: tests PASS; manifest validator prints one line per source and exits
0; full suite PASS.

- [ ] **Step 8: Commit only manifests, code, docs, and fixtures**

```bash
git add app/data scripts/validate_sources.py scripts/import_canonical_data.py \
  docs/data/sources.yaml docs/data/README.md tests/fixtures/data \
  tests/test_canonical_geography.py tests/test_steg_units.py
git commit -m "feat: validate canonical source artifacts"
```

Do not stage the PBF, PDF, XLS, or unlicensed raw datasets.

---

### Task 4: Bounded request metrics and protected operations summary

**Files:**
- Create: `app/request_metrics.py`
- Create: `app/api/__init__.py`
- Create: `app/api/ops.py`
- Modify: `app/main.py`
- Modify: `static/ops.html`
- Test: `tests/test_request_metrics.py`
- Modify: `tests/test_ops_api.py`

- [ ] **Step 1: Write failing metrics tests**

```python
# tests/test_request_metrics.py
from app.request_metrics import RequestMetric, RequestMetrics


def test_summary_is_bounded_and_aggregates_by_route():
    metrics = RequestMetrics(max_samples=2)
    metrics.record(RequestMetric("GET", "/api/status", 200, 10.0, 2.0))
    metrics.record(RequestMetric("GET", "/api/status", 500, 30.0, 8.0))
    metrics.record(RequestMetric("GET", "/api/stats", 200, 20.0, 5.0))

    summary = metrics.summary()

    assert summary["sample_count"] == 2
    assert summary["status_counts"] == {"2xx": 1, "4xx": 0, "5xx": 1}
    assert summary["p95_ms"] == 30.0
    assert "headers" not in str(summary).lower()
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_request_metrics.py -v
```

Expected: FAIL because `app.request_metrics` does not exist.

- [ ] **Step 3: Implement a bounded in-memory collector**

```python
# app/request_metrics.py
from collections import Counter, deque
from dataclasses import asdict, dataclass
from math import ceil
from threading import Lock


@dataclass(frozen=True)
class RequestMetric:
    method: str
    route: str
    status: int
    duration_ms: float
    db_duration_ms: float = 0.0
    db_errors: int = 0


class RequestMetrics:
    def __init__(self, max_samples: int = 1000):
        self._samples = deque(maxlen=max_samples)
        self._lock = Lock()

    def record(self, metric: RequestMetric) -> None:
        with self._lock:
            self._samples.append(metric)

    def summary(self) -> dict:
        with self._lock:
            samples = list(self._samples)
        durations = sorted(x.duration_ms for x in samples)
        percentile = lambda p: (
            durations[min(len(durations) - 1, ceil(len(durations) * p) - 1)]
            if durations else 0.0
        )
        status_counts = {
            "2xx": sum(200 <= x.status < 300 for x in samples),
            "4xx": sum(400 <= x.status < 500 for x in samples),
            "5xx": sum(x.status >= 500 for x in samples),
        }
        routes = Counter(x.route for x in samples)
        return {
            "sample_count": len(samples),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "status_counts": status_counts,
            "routes": dict(routes.most_common(10)),
            "recent": [asdict(x) for x in samples[-50:]],
        }


metrics = RequestMetrics()
```

- [ ] **Step 4: Measure libSQL calls without persisting request rows**

Add request-local database counters in `app/request_metrics.py`:

```python
from contextvars import ContextVar

_db_state = ContextVar(
    "db_state", default={"duration_ms": 0.0, "errors": 0}
)


def reset_db_metrics() -> None:
    _db_state.set({"duration_ms": 0.0, "errors": 0})


def record_db_call(duration_ms: float, *, failed: bool) -> None:
    current = _db_state.get()
    _db_state.set({
        "duration_ms": current["duration_ms"] + duration_ms,
        "errors": current["errors"] + int(failed),
    })


def current_db_metrics() -> dict:
    return dict(_db_state.get())
```

Wrap `_Conn.execute()` in `app/db.py`:

```python
def execute(self, sql, params=None):
    from time import perf_counter
    from .request_metrics import record_db_call

    started = perf_counter()
    try:
        return _Result(self._client.execute(sql, list(params or [])))
    except Exception:
        record_db_call(
            (perf_counter() - started) * 1000, failed=True
        )
        raise
    finally:
        # Do not double count failures: the except branch already recorded it.
        if not sys.exc_info()[0]:
            record_db_call(
                (perf_counter() - started) * 1000, failed=False
            )
```

Import `sys` in `app/db.py`. Tests assert that successful and failed fake
client calls update duration/error counts without recording SQL or parameters.

- [ ] **Step 5: Record safe metrics in middleware**

In `app/main.py`, after `route_template` and `duration_ms` are known:

```python
from .request_metrics import (
    RequestMetric,
    current_db_metrics,
    metrics,
    reset_db_metrics,
)

reset_db_metrics()  # place immediately before call_next(request)
duration_ms = round((time.perf_counter() - started) * 1000, 3)
db_metric = current_db_metrics()
metrics.record(RequestMetric(
    method=request.method,
    route=route_template,
    status=response.status_code,
    duration_ms=duration_ms,
    db_duration_ms=db_metric["duration_ms"],
    db_errors=db_metric["errors"],
))
```

Keep the existing structured log. Remove `_secret_debug`, the
`cron_auth_failed` expected/received fingerprints, and the unused `hashlib`
import. Authentication failures log only route, status, and request ID through
the normal middleware.

- [ ] **Step 6: Add protected summary endpoint**

```python
# app/api/ops.py
from fastapi import APIRouter, Depends

from app.request_metrics import metrics


def create_router(verify_ops_secret):
    router = APIRouter(
        prefix="/api/internal/ops",
        dependencies=[Depends(verify_ops_secret)],
    )

    @router.get("/summary")
    def summary():
        return metrics.summary()

    return router
```

Compose this router in `app/main.py` after `verify_ops_secret` is defined.
Extend `static/ops.html` to fetch the endpoint only after an operator enters an
OPS secret held in `sessionStorage`; never place it in the URL or localStorage.

- [ ] **Step 7: Verify**

Run:

```bash
pytest tests/test_request_metrics.py tests/test_ops_api.py \
  tests/test_request_logging.py -v
pytest -q
```

Expected: tests PASS and no log contains secret fingerprints.

- [ ] **Step 8: Commit**

```bash
git add app/request_metrics.py app/api app/main.py static/ops.html \
  tests/test_request_metrics.py tests/test_ops_api.py \
  tests/test_request_logging.py
git commit -m "feat: add protected request metrics"
```

---

### Task 5: Public/internal OpenAPI split and Postman smoke tests

**Files:**
- Create: `app/api/docs.py`
- Create: `scripts/export_openapi.py`
- Create: `package.json`
- Generate: `package-lock.json`
- Create: `postman/environment.example.json`
- Generate: `postman/tunisia-outage-tracker.postman_collection.json`
- Modify: `app/main.py`
- Test: `tests/test_openapi_boundaries.py`

- [ ] **Step 1: Write failing API-boundary tests**

```python
# tests/test_openapi_boundaries.py
from fastapi.testclient import TestClient

from app import main


def test_public_openapi_excludes_internal_routes():
    schema = TestClient(main.app).get("/openapi.json").json()
    assert "/api/status" in schema["paths"]
    assert all(not path.startswith("/api/internal") for path in schema["paths"])


def test_internal_openapi_requires_ops_secret(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "ops-secret")
    client = TestClient(main.app)
    assert client.get("/api/internal/openapi.json").status_code == 401
    response = client.get(
        "/api/internal/openapi.json",
        headers={"X-Ops-Secret": "ops-secret"},
    )
    assert response.status_code == 200
    assert "/api/internal/ops/summary" in response.json()["paths"]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_openapi_boundaries.py -v
```

Expected: FAIL because the default schema exposes internal routes.

- [ ] **Step 3: Implement filtered schemas**

```python
# app/api/docs.py
from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi


def _schema(app: FastAPI, *, internal: bool):
    schema = get_openapi(
        title=app.title,
        version="2.0.0",
        routes=app.routes,
    )
    schema["paths"] = {
        path: value
        for path, value in schema["paths"].items()
        if path.startswith("/api/internal") is internal
    }
    return schema


def install_docs(app: FastAPI, verify_ops_secret):
    @app.get("/openapi.json", include_in_schema=False)
    def public_openapi():
        return _schema(app, internal=False)

    @app.get("/docs", include_in_schema=False)
    def public_docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title=f"{app.title} API"
        )

    @app.get(
        "/api/internal/openapi.json",
        include_in_schema=False,
        dependencies=[Depends(verify_ops_secret)],
    )
    def internal_openapi():
        return _schema(app, internal=True)
```

Initialize FastAPI with default documentation disabled:

```python
app = FastAPI(
    title="Tunisia Outage Tracker",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
```

- [ ] **Step 4: Add reproducible export and smoke environment**

`scripts/export_openapi.py` imports `app.main.app`, calls the public schema
builder, and writes sorted JSON to `build/openapi-public.json`.

```json
{
  "private": true,
  "scripts": {
    "postman:generate": "openapi2postmanv2 -s build/openapi-public.json -o postman/tunisia-outage-tracker.postman_collection.json -p"
  },
  "devDependencies": {
    "openapi-to-postmanv2": "5.0.0"
  }
}
```

Run `npm install` once to create the locked dependency graph. Generate and
check in the collection. The export script sorts OpenAPI keys, and a final
normalization step removes generator timestamps before writing the collection.
Add `node_modules/` and `build/` to `.gitignore`.

```json
{
  "id": "local-tunisia-outage-tracker",
  "name": "Local Docker",
  "values": [
    {"key": "baseUrl", "value": "http://app:8010", "enabled": true},
    {"key": "opsSecret", "value": "local-ops-secret", "enabled": true}
  ]
}
```

The collection must assert 200 responses for `/api/status`,
`/api/model-readiness`, and `/api/stats`, and assert 401 for an internal route
without `X-Ops-Secret`.

- [ ] **Step 5: Verify**

Run:

```bash
pytest tests/test_openapi_boundaries.py -v
python scripts/export_openapi.py
npm ci
npm run postman:generate
docker compose --profile smoke run --rm newman
pytest -q
```

Expected: boundary tests PASS, export has no diff on a second run, Newman has
zero failed assertions, full suite PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/docs.py app/main.py scripts/export_openapi.py postman \
  package.json package-lock.json \
  tests/test_openapi_boundaries.py
git commit -m "feat: separate public and internal API docs"
```

---

### Task 6: Subregion-scoped evidence observations

**Files:**
- Create: `migrations/0002_scoped_observations.sql`
- Create: `app/model/config.py`
- Modify: `app/evidence_models.py`
- Modify: `app/evidence_pipeline.py`
- Modify: `app/db.py`
- Test: `tests/test_scoped_observations.py`
- Modify: `tests/test_model_builds.py`

- [ ] **Step 1: Write failing scope tests**

```python
# tests/test_scoped_observations.py
from app import db, evidence_pipeline
from tests.test_model_builds import _active_notice


def test_different_named_subregions_do_not_get_full_strength_edge():
    _active_notice(
        "n1",
        [
            ("A", "جهة صفاقس"),
            ("B", "جهة صفاقس"),
            ("C", "جهة قرقنة"),
        ],
        "2026-07-20",
    )
    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:00:00Z"
    )
    rows = {
        (r["locality_a"], r["locality_b"]): r
        for r in db.build_scoped_cooccurrences(build_id)
    }
    assert rows[("A", "B")]["scope_kind"] == "subregion"
    assert rows[("A", "B")]["scope_confidence"] == 1.0
    assert ("A", "C") not in rows


def test_headerless_notice_uses_low_confidence_fallback():
    _active_notice(
        "n1", [("A", None), ("B", None)], "2026-07-20"
    )
    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-30T00:00:00Z"
    )
    row = db.build_scoped_cooccurrences(build_id)[0]
    assert row["scope_kind"] == "notice_fallback"
    assert row["scope_confidence"] == 0.35
```

First update `_active_notice()` in `tests/test_model_builds.py` so each item may
be either `"A"` or `("A", "جهة صفاقس")`:

```python
for ordinal, item in enumerate(localities):
    locality, subregion = item if isinstance(item, tuple) else (item, None)
    conn.execute(
        """
        INSERT INTO notice_localities(
            parse_id, ordinal, raw_name, canonical_name, subregion_name
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [parse_id, ordinal, locality, locality, subregion],
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_scoped_observations.py -v
```

Expected: FAIL because scoped observations do not exist.

- [ ] **Step 3: Add versioned model configuration**

```python
# app/model/config.py
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    version: str = "evidence-v2.0"
    subregion_scope_confidence: float = 1.0
    notice_fallback_confidence: float = 0.35
    min_valid_notices: int = 30
    min_distinct_dates: int = 15
    min_localities: int = 10
    min_repeated_pairs: int = 20
    min_active_ok_ratio: float = 0.80
    min_recent_parse_ratio: float = 0.80
    max_largest_notice_share: float = 0.20


CONFIG = ModelConfig()
```

- [ ] **Step 4: Add observation schema**

```sql
-- migrations/0002_scoped_observations.sql
CREATE TABLE IF NOT EXISTS build_pair_observations (
    build_id TEXT NOT NULL,
    notice_id TEXT NOT NULL,
    outage_date TEXT,
    locality_a TEXT NOT NULL,
    locality_b TEXT NOT NULL,
    scope_name TEXT NOT NULL DEFAULT '',
    scope_kind TEXT NOT NULL,
    parse_confidence REAL NOT NULL,
    scope_confidence REAL NOT NULL,
    canonicalization_confidence REAL NOT NULL,
    PRIMARY KEY(
        build_id, notice_id, locality_a, locality_b, scope_kind, scope_name
    )
);

CREATE INDEX IF NOT EXISTS idx_pair_observations_build
ON build_pair_observations(build_id);

CREATE TABLE IF NOT EXISTS quality_gate_results (
    build_id TEXT NOT NULL,
    gate_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    measured_value REAL,
    required_value REAL,
    reason_code TEXT NOT NULL,
    config_version TEXT NOT NULL,
    PRIMARY KEY(build_id, gate_key)
);

CREATE TABLE IF NOT EXISTS publication_decisions (
    product_type TEXT NOT NULL,
    product_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(product_type, product_id)
);
```

- [ ] **Step 5: Populate observations by source scope**

Add this exact `db.populate_scoped_observations(build_id, config)` behavior:

```python
def populate_scoped_observations(build_id, config) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO build_pair_observations(
                build_id, notice_id, outage_date, locality_a, locality_b,
                scope_name, scope_kind, parse_confidence, scope_confidence,
                canonicalization_confidence
            )
            WITH named AS (
                SELECT DISTINCT ns.notice_id, np.notice_date_iso,
                       np.parse_status, nl.canonical_name,
                       TRIM(nl.subregion_name) AS scope_name
                FROM notice_state ns
                JOIN notice_parses np ON np.parse_id = ns.active_parse_id
                JOIN notice_localities nl ON nl.parse_id = ns.active_parse_id
                WHERE nl.subregion_name IS NOT NULL
                  AND TRIM(nl.subregion_name) <> ''
            )
            SELECT ?, a.notice_id, a.notice_date_iso,
                   a.canonical_name, b.canonical_name,
                   a.scope_name, 'subregion',
                   CASE WHEN a.parse_status = 'ok' THEN 1.0 ELSE 0.7 END,
                   ?, 1.0
            FROM named a
            JOIN named b
              ON b.notice_id = a.notice_id
             AND b.scope_name = a.scope_name
             AND a.canonical_name < b.canonical_name
            """,
            [build_id, config.subregion_scope_confidence],
        )
        conn.execute(
            """
            INSERT INTO build_pair_observations(
                build_id, notice_id, outage_date, locality_a, locality_b,
                scope_name, scope_kind, parse_confidence, scope_confidence,
                canonicalization_confidence
            )
            WITH fallback AS (
                SELECT DISTINCT ns.notice_id, np.notice_date_iso,
                       np.parse_status, nl.canonical_name
                FROM notice_state ns
                JOIN notice_parses np ON np.parse_id = ns.active_parse_id
                JOIN notice_localities nl ON nl.parse_id = ns.active_parse_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM notice_localities scoped
                    WHERE scoped.parse_id = ns.active_parse_id
                      AND scoped.subregion_name IS NOT NULL
                      AND TRIM(scoped.subregion_name) <> ''
                )
            )
            SELECT ?, a.notice_id, a.notice_date_iso,
                   a.canonical_name, b.canonical_name,
                   '', 'notice_fallback',
                   CASE WHEN a.parse_status = 'ok' THEN 1.0 ELSE 0.7 END,
                   ?, 1.0
            FROM fallback a
            JOIN fallback b
              ON b.notice_id = a.notice_id
             AND a.canonical_name < b.canonical_name
            """,
            [build_id, config.notice_fallback_confidence],
        )


def build_scoped_cooccurrences(build_id: str) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM build_pair_observations
            WHERE build_id = ?
            ORDER BY locality_a, locality_b, scope_kind, scope_name
            """,
            [build_id],
        ).fetchall()
```

Replace `populate_model_build()` pair aggregation with aggregation from
`build_pair_observations`:

```sql
INSERT INTO build_cooccurrences(
    build_id, locality_a, locality_b, notice_count,
    distinct_date_count, first_observed_on, last_observed_on
)
SELECT build_id, locality_a, locality_b,
       COUNT(DISTINCT notice_id),
       COUNT(DISTINCT outage_date),
       MIN(outage_date), MAX(outage_date)
FROM build_pair_observations
WHERE build_id = ?
GROUP BY build_id, locality_a, locality_b;
```

After build population, evaluate `model_readiness` using values from
`ModelConfig`, write every signal to `quality_gate_results`, and write one
`publication_decisions` row. Decision is `published` only when all model and
operational gates pass, `experimental` when model gates pass but operational
gates do not, and `blocked` when any model gate fails. Activation of the
evidence build remains allowed, but cluster activation requires `published`;
this preserves evidence inspection without publishing an unready model.

- [ ] **Step 6: Verify regression and scope behavior**

Run:

```bash
pytest tests/test_scoped_observations.py tests/test_model_builds.py \
  tests/test_model_readiness.py -v
pytest -q
```

Expected: scope tests PASS; existing model build/readiness tests PASS after
fixtures preserve `subregion_name`.

- [ ] **Step 7: Commit**

```bash
git add migrations/0002_scoped_observations.sql app/model \
  app/evidence_models.py app/evidence_pipeline.py app/db.py \
  tests/test_scoped_observations.py tests/test_model_builds.py
git commit -m "feat: scope outage evidence by STEG subregion"
```

---

### Task 7: Weighted graph V2

**Files:**
- Create: `app/model/graph.py`
- Modify: `app/cluster_inference.py`
- Modify: `app/db.py`
- Test: `tests/test_weighted_graph_v2.py`

- [ ] **Step 1: Write failing hand-calculated graph test**

```python
# tests/test_weighted_graph_v2.py
import pytest

from app.model.graph import EdgeEvidence, build_weighted_graph


def test_weight_combines_ppmi_dates_and_scope_without_geo_only_edges():
    edges = [
        EdgeEvidence(
            locality_a="A",
            locality_b="B",
            notice_count=3,
            distinct_date_count=3,
            mean_scope_confidence=1.0,
            mean_parse_confidence=1.0,
            service_agreement=1.0,
        )
    ]
    graph = build_weighted_graph(
        edges,
        total_notices=10,
        locality_counts={"A": 4, "B": 5},
    )
    data = graph["A"]["B"]
    assert data["ppmi"] == pytest.approx(0.405465, rel=1e-5)
    assert 0 < data["weight"] <= data["ppmi"] * 1.15
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_weighted_graph_v2.py -v
```

Expected: FAIL because `app.model.graph` does not exist.

- [ ] **Step 3: Implement typed graph construction**

```python
# app/model/graph.py
import math

import networkx as nx
from pydantic import BaseModel, Field


class EdgeEvidence(BaseModel):
    locality_a: str
    locality_b: str
    notice_count: int = Field(gt=0)
    distinct_date_count: int = Field(ge=0)
    mean_scope_confidence: float = Field(ge=0, le=1)
    mean_parse_confidence: float = Field(ge=0, le=1)
    service_agreement: float = Field(default=0, ge=0, le=1)


def build_weighted_graph(
    edges: list[EdgeEvidence],
    *,
    total_notices: int,
    locality_counts: dict[str, int],
) -> nx.Graph:
    graph = nx.Graph()
    for edge in edges:
        p_ab = edge.notice_count / total_notices
        p_a = locality_counts[edge.locality_a] / total_notices
        p_b = locality_counts[edge.locality_b] / total_notices
        ppmi = max(0.0, math.log(p_ab / (p_a * p_b)))
        if ppmi == 0:
            continue
        temporal = min(1.0, edge.distinct_date_count / 3)
        evidence = (
            edge.mean_scope_confidence
            * edge.mean_parse_confidence
            * temporal
        )
        regularizer = 1.0 + 0.15 * edge.service_agreement
        graph.add_edge(
            edge.locality_a,
            edge.locality_b,
            weight=ppmi * evidence * regularizer,
            ppmi=ppmi,
            temporal_support=temporal,
            scope_confidence=edge.mean_scope_confidence,
        )
    return graph
```

Geographic/service agreement only multiplies an existing outage edge and is
capped at +15%. It never creates an edge.

- [ ] **Step 4: Add DB projection and compatibility façade**

Add `db.build_edge_evidence(build_id)` to aggregate observation confidence and
join optional canonical service-unit agreement. Change
`cluster_inference.build_ppmi_graph()` into a compatibility wrapper around
`build_weighted_graph()`. Set:

```python
ALGORITHM_VERSION = "evidence-weighted-louvain-v2"
```

- [ ] **Step 5: Verify**

Run:

```bash
pytest tests/test_weighted_graph_v2.py tests/test_cluster_inference.py -v
pytest -q
```

Expected: V2 math tests PASS, compatibility tests PASS, full suite PASS.

- [ ] **Step 6: Commit**

```bash
git add app/model/graph.py app/cluster_inference.py app/db.py \
  tests/test_weighted_graph_v2.py tests/test_cluster_inference.py
git commit -m "feat: build confidence-weighted outage graph"
```

---

### Task 8: Stable cluster identity, lineage, and validation

**Files:**
- Create: `migrations/0003_cluster_lineage.sql`
- Create: `app/model/cluster_ids.py`
- Create: `app/model/validation.py`
- Modify: `app/cluster_inference.py`
- Modify: `app/db.py`
- Test: `tests/test_cluster_lineage.py`
- Test: `tests/test_model_validation.py`

- [ ] **Step 1: Write failing cluster-ID tests**

```python
# tests/test_cluster_lineage.py
from app.model.cluster_ids import match_cluster_ids


def test_split_only_best_child_inherits_previous_id():
    previous = {7: {"A", "B", "C", "D"}}
    current = {0: {"A", "B", "C"}, 1: {"D", "E"}}

    result = match_cluster_ids(previous, current, next_id=8)

    assert result.ids[0] == 7
    assert result.ids[1] == 8
    assert result.lineage[0][0].similarity == 0.75


def test_similarity_below_half_allocates_new_id():
    result = match_cluster_ids(
        {7: {"A", "B", "C"}},
        {0: {"A", "X", "Y"}},
        next_id=8,
    )
    assert result.ids[0] == 8
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_cluster_lineage.py -v
```

Expected: FAIL because matcher does not exist.

- [ ] **Step 3: Implement deterministic Jaccard matching**

```python
# app/model/cluster_ids.py
from dataclasses import dataclass


@dataclass(frozen=True)
class Lineage:
    previous_id: int
    similarity: float


@dataclass(frozen=True)
class MatchResult:
    ids: dict[int, int]
    lineage: dict[int, list[Lineage]]
    next_id: int


def match_cluster_ids(previous, current, *, next_id, threshold=0.50):
    import networkx as nx

    graph = nx.Graph()
    lineage = {}
    for new_id, new_members in sorted(current.items()):
        lineage[new_id] = []
        for old_id, old_members in sorted(previous.items()):
            similarity = len(new_members & old_members) / len(
                new_members | old_members
            )
            if similarity > 0:
                lineage[new_id].append(Lineage(old_id, similarity))
            if similarity >= threshold:
                # The tiny deterministic term resolves exact-weight ties
                # without changing any meaningful Jaccard comparison.
                tie = 1e-9 / (1 + old_id + new_id)
                graph.add_edge(
                    ("old", old_id),
                    ("new", new_id),
                    weight=similarity + tie,
                )
        lineage[new_id].sort(
            key=lambda row: (-row.similarity, row.previous_id)
        )

    ids = {}
    for left, right in nx.max_weight_matching(
        graph, maxcardinality=False, weight="weight"
    ):
        old_node = left if left[0] == "old" else right
        new_node = right if right[0] == "new" else left
        ids[new_node[1]] = old_node[1]

    for new_id in sorted(current):
        if new_id not in ids:
            ids[new_id] = next_id
            next_id += 1
    return MatchResult(ids, lineage, next_id)
```

- [ ] **Step 4: Add lineage schema**

```sql
-- migrations/0003_cluster_lineage.sql
CREATE TABLE IF NOT EXISTS cluster_lineage (
    run_id TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    previous_run_id TEXT NOT NULL,
    previous_cluster_id INTEGER NOT NULL,
    jaccard_similarity REAL NOT NULL,
    inherited_id INTEGER NOT NULL,
    PRIMARY KEY(
        run_id, cluster_id, previous_run_id, previous_cluster_id
    )
);
```

- [ ] **Step 5: Implement bootstrap and temporal validation**

Implement these public validation contracts in `app/model/validation.py`:

```python
class ValidationReport(BaseModel):
    bootstrap_runs: int
    mean_membership_agreement: float
    held_out_edge_recall: float | None
    raw_cooccurrence_baseline: float | None
    geography_baseline: float | None
    service_unit_baseline: float | None
    largest_notice_removed_agreement: float


def temporal_split(dates: list[str]) -> tuple[set[str], set[str]]:
    ordered = sorted(set(dates))
    cut = max(1, int(len(ordered) * 0.8))
    return set(ordered[:cut]), set(ordered[cut:])
```

Tests use fixed small graphs and deterministic random seeds. A run with fewer
than two held-out dates reports `None`, not a fabricated score.

- [ ] **Step 6: Integrate matching and validation**

After Louvain returns temporary community IDs, load the previous active run,
match IDs, persist lineage, compute the validation report, and store it as
versioned JSON metadata on the cluster run. Activation still requires a
completed run for the current active evidence build.

- [ ] **Step 7: Verify**

Run:

```bash
pytest tests/test_cluster_lineage.py tests/test_model_validation.py \
  tests/test_cluster_inference.py tests/test_evidence_atomicity.py -v
pytest -q
```

Expected: lineage/validation tests PASS; activation safety remains PASS.

- [ ] **Step 8: Commit**

```bash
git add migrations/0003_cluster_lineage.sql app/model/cluster_ids.py \
  app/model/validation.py app/cluster_inference.py app/db.py \
  tests/test_cluster_lineage.py tests/test_model_validation.py
git commit -m "feat: preserve cluster identity and validation"
```

---

### Task 9: Private Sfax/Kerkennah topology and candidate ranking

**Files:**
- Create: `migrations/0004_private_candidates.sql`
- Create: `app/topology/__init__.py`
- Create: `app/topology/osm.py`
- Create: `app/model/candidates.py`
- Create: `scripts/extract_sfax_topology.py`
- Test: `tests/fixtures/data/osm_assets.json`
- Test: `tests/test_private_candidates.py`
- Test: `tests/test_topology_osm.py`

- [ ] **Step 1: Write failing candidate tests**

```python
# tests/test_private_candidates.py
from app.model.candidates import CandidateFeatures, rank_candidates


def test_ranking_exposes_components_and_is_not_probability():
    result = rank_candidates([
        CandidateFeatures(
            asset_id="substation-a",
            outage_fit=0.9,
            topology_consistency=0.8,
            service_prior=0.7,
            distance_score=0.8,
            temporal_support=0.7,
            completeness=0.9,
        ),
        CandidateFeatures(
            asset_id="substation-b",
            outage_fit=0.5,
            topology_consistency=0.4,
            service_prior=0.5,
            distance_score=0.6,
            temporal_support=0.4,
            completeness=0.8,
        ),
    ])
    first, second = result.candidates
    assert first.asset_id == "substation-a"
    assert first.score > second.score
    assert first.score_kind == "ranking_index"
    assert first.components["outage_fit"] == 0.9


def test_insufficient_independent_dates_returns_no_ranking():
    result = rank_candidates([], independent_dates=1)
    assert result.status == "insufficient_evidence"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_private_candidates.py tests/test_topology_osm.py -v
```

Expected: FAIL because private topology modules do not exist.

- [ ] **Step 3: Implement bounded OSM extraction**

```python
# app/topology/osm.py
from pathlib import Path

import osmium
from pydantic import BaseModel


class GridAsset(BaseModel):
    asset_id: str
    asset_type: str
    latitude: float | None
    longitude: float | None
    voltage: str | None = None
    source_snapshot_id: str


class GridEdge(BaseModel):
    edge_id: str
    power_type: str
    node_refs: list[int]
    voltage: str | None = None


class TopologySnapshot(BaseModel):
    assets: list[GridAsset]
    edges: list[GridEdge]


class PowerHandler(osmium.SimpleHandler):
    def __init__(self, snapshot_id: str):
        super().__init__()
        self.snapshot_id = snapshot_id
        self.assets = []
        self.edges = []

    def node(self, node):
        power = node.tags.get("power")
        if power not in {"substation", "transformer", "pole", "tower"}:
            return
        self.assets.append(GridAsset(
            asset_id=f"node/{node.id}",
            asset_type=power,
            latitude=node.location.lat if node.location.valid() else None,
            longitude=node.location.lon if node.location.valid() else None,
            voltage=node.tags.get("voltage"),
            source_snapshot_id=self.snapshot_id,
        ))

    def way(self, way):
        power = way.tags.get("power")
        if power in {"line", "minor_line", "cable"}:
            self.edges.append(GridEdge(
                edge_id=f"way/{way.id}",
                power_type=power,
                node_refs=[node.ref for node in way.nodes],
                voltage=way.tags.get("voltage"),
            ))
        if power in {"substation", "transformer"}:
            locations = [
                node.location for node in way.nodes
                if node.location.valid()
            ]
            self.assets.append(GridAsset(
                asset_id=f"way/{way.id}",
                asset_type=power,
                latitude=(
                    sum(point.lat for point in locations) / len(locations)
                    if locations else None
                ),
                longitude=(
                    sum(point.lon for point in locations) / len(locations)
                    if locations else None
                ),
                voltage=way.tags.get("voltage"),
                source_snapshot_id=self.snapshot_id,
            ))


def load_power_assets(path: Path, snapshot_id: str) -> TopologySnapshot:
    handler = PowerHandler(snapshot_id)
    handler.apply_file(str(path), locations=True)
    return TopologySnapshot(assets=handler.assets, edges=handler.edges)
```

The extraction script requires explicit `--pbf`, `--snapshot-id`, and
`--output` arguments. It refuses an unregistered checksum and writes only the
bounded Sfax/Kerkennah derived extract. `tests/test_topology_osm.py` builds a
small `TopologySnapshot` fixture and asserts that a line preserves ordered node
references, voltage, source snapshot ID, and connected-component membership.

- [ ] **Step 4: Implement explainable ranking**

```python
# app/model/candidates.py
from typing import Literal

from pydantic import BaseModel, Field


class CandidateFeatures(BaseModel):
    asset_id: str
    outage_fit: float = Field(ge=0, le=1)
    topology_consistency: float = Field(ge=0, le=1)
    service_prior: float = Field(ge=0, le=1)
    distance_score: float = Field(ge=0, le=1)
    temporal_support: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)


class RankedCandidate(BaseModel):
    asset_id: str
    score: float
    score_kind: Literal["ranking_index"] = "ranking_index"
    components: dict[str, float]


class CandidateRunResult(BaseModel):
    status: Literal["experimental", "insufficient_evidence"]
    candidates: list[RankedCandidate]


WEIGHTS = {
    "outage_fit": 0.35,
    "topology_consistency": 0.25,
    "service_prior": 0.15,
    "distance_score": 0.10,
    "temporal_support": 0.10,
    "completeness": 0.05,
}


def rank_candidates(rows, *, independent_dates=2):
    if independent_dates < 2 or not rows:
        return CandidateRunResult(
            status="insufficient_evidence", candidates=[]
        )
    ranked = []
    for row in rows:
        components = row.model_dump(exclude={"asset_id"})
        score = sum(components[key] * weight for key, weight in WEIGHTS.items())
        ranked.append(RankedCandidate(
            asset_id=row.asset_id,
            score=score,
            components=components,
        ))
    return CandidateRunResult(
        status="experimental",
        candidates=sorted(ranked, key=lambda x: (-x.score, x.asset_id)),
    )
```

Add `weight_sensitivity(rows)` that reruns the ranking after multiplying each
weight by `0.8` and `1.2`, normalizes the changed weights to sum to one, and
returns each candidate's minimum and maximum rank. Persist that exact
`{"min_rank": int, "max_rank": int}` object in `sensitivity_json`.

- [ ] **Step 5: Add private persistence**

```sql
-- migrations/0004_private_candidates.sql
CREATE TABLE IF NOT EXISTS asset_candidate_runs (
    run_id TEXT PRIMARY KEY,
    cluster_run_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    public_error_code TEXT
);

CREATE TABLE IF NOT EXISTS asset_candidate_scores (
    run_id TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    component_json TEXT NOT NULL,
    sensitivity_json TEXT NOT NULL,
    PRIMARY KEY(run_id, cluster_id, asset_id)
);
```

No public endpoint reads these tables.

- [ ] **Step 6: Verify privacy and model behavior**

Run:

```bash
pytest tests/test_private_candidates.py tests/test_topology_osm.py \
  tests/test_openapi_boundaries.py -v
pytest -q
```

Expected: ranking tests PASS; public OpenAPI contains no candidate paths; full
suite PASS.

- [ ] **Step 7: Commit**

```bash
git add migrations/0004_private_candidates.sql app/topology \
  app/model/candidates.py scripts/extract_sfax_topology.py \
  tests/fixtures/data/osm_assets.json tests/test_private_candidates.py \
  tests/test_topology_osm.py
git commit -m "feat: add private Sfax asset candidate pilot"
```

---

### Task 10: Integrated verification, documentation, and pull request

**Files:**
- Modify: `README.md`
- Modify: `DEPLOYMENT.md`
- Modify: `docs/OPERATIONS.md`
- Create: `docs/MODEL_V2.md`
- Create: `docs/PRIVATE_TOPOLOGY.md`
- Modify: `.github/workflows/scrape.yml`
- Test: complete suite and Docker smoke tests

- [ ] **Step 1: Add integration assertions**

Add these integration tests:

```python
def test_failed_v2_build_keeps_last_known_good_build(monkeypatch):
    previous = seed_completed_active_build()
    monkeypatch.setattr(db, "validate_model_build", lambda _: (_ for _ in ()).throw(ValueError("invalid")))
    with pytest.raises(ValueError):
        evidence_pipeline.build_model_evidence(
            created_at="2026-07-30T10:00:00Z"
        )
    assert db.active_build_id() == previous


def test_public_responses_never_contain_private_candidate_fields(client):
    for path in ("/api/status", "/api/model-status", "/api/clusters", "/openapi.json"):
        body = client.get(path).text
        assert "asset_candidate" not in body
        assert "source_snapshot_id" not in body
```

- [ ] **Step 2: Run focused failure tests**

Run:

```bash
pytest tests/test_evidence_atomicity.py tests/test_openapi_boundaries.py \
  tests/test_private_candidates.py -v
```

Expected: all PASS.

- [ ] **Step 3: Document exact operator workflows**

`README.md` must include:

```bash
cp .env.example .env
docker compose up --build
docker compose --profile test run --rm test
python scripts/validate_sources.py docs/data/sources.yaml
```

`docs/MODEL_V2.md` documents the subregion edge rule, confidence components,
stable ID threshold, temporal holdout, baselines, and public disclaimer.

`docs/PRIVATE_TOPOLOGY.md` documents local-only PBF placement, checksum
registration, OSM attribution, extraction command, privacy boundary, and
deletion/rebuild procedure.

`docs/OPERATIONS.md` documents OPS secret usage, metrics retention, OpenAPI
locations, Newman smoke tests, request-ID investigation, and rollback.

- [ ] **Step 4: Update daily automation**

Keep one daily scrape. The workflow sequence becomes:

```yaml
- name: Scrape notices
  run: curl --fail-with-body --max-time 600 -X POST
    "$APP_URL/api/internal/scrape"
    -H "X-Cron-Secret: $CRON_SECRET"

- name: Check readiness
  run: curl --fail-with-body "$APP_URL/api/model-readiness"

- name: Recluster current evidence
  run: curl --fail-with-body --max-time 600 -X POST
    "$APP_URL/api/internal/recluster"
    -H "X-Cron-Secret: $CRON_SECRET"
```

The backfill remains manually triggered. The private candidate pilot is never
triggered by the public GitHub Action.

- [ ] **Step 5: Run complete local verification**

Run:

```bash
pytest -q
docker compose --profile test run --rm test
docker compose up -d --build
docker compose ps
docker compose --profile smoke run --rm newman
python scripts/validate_sources.py docs/data/sources.yaml
git diff --check
git status --short
```

Expected:

- all pytest tests pass twice, host and Docker;
- app is healthy;
- Newman reports zero failed assertions;
- every registered source checksum passes;
- `git diff --check` prints nothing;
- status contains only intended tracked changes plus known untracked raw
  research artifacts.

- [ ] **Step 6: Security and privacy audit**

Run:

```bash
rg -n "CRON_SECRET=.{8}|OPS_SECRET=.{8}|TURSO_AUTH_TOKEN=.{8}" \
  --glob '!*.example*' --glob '!docs/superpowers/**' .
git ls-files | rg 'osm\\.pbf$|\\.env$|tracker\\.db$'
```

Expected: no real secret matches; no PBF, `.env`, or database file is tracked.

- [ ] **Step 7: Commit documentation and automation**

```bash
git add README.md DEPLOYMENT.md docs/OPERATIONS.md docs/MODEL_V2.md \
  docs/PRIVATE_TOPOLOGY.md .github/workflows/scrape.yml
git commit -m "docs: add evidence model v2 operations guide"
```

- [ ] **Step 8: Push branch and open pull request**

```bash
git push -u origin feat/evidence-model-v2
gh pr create \
  --title "feat: evidence model v2" \
  --body-file docs/superpowers/specs/2026-07-30-evidence-model-v2-design.md
```

Before requesting review, replace the PR body with a concise summary that
includes verification output, migration behavior, rollback, private-data
boundary, and known limitations. Do not merge until CI passes and the diff
contains no raw private topology or secrets.
