#!/usr/bin/env python3
"""
Tunisia outage tracker -- FastAPI backend.

Serves:
  - /api/*      JSON API (official STEG notices + crowdsourced reports)
  - /           the static frontend (static/index.html)

Run locally:
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8010

Then open http://127.0.0.1:8010 in a browser.
"""

import hmac
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backfill_official
from . import cluster_inference
from . import db
from . import import_official
from . import model_readiness
from . import observability
from .governorates import GOVERNORATE_NAMES, GOVERNORATES
from .request_metrics import (
    RequestMetric,
    current_db_metrics,
    metrics,
    reset_db_metrics,
)

app = FastAPI(
    title="Tunisia Outage Tracker",
    version="2.0.0",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@app.middleware("http")
async def request_metadata(request: Request, call_next):
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    reset_db_metrics()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    response.headers["X-Request-ID"] = request_id
    route = request.scope.get("route")
    route_template = getattr(route, "path", request.url.path)
    db_metric = current_db_metrics()
    metrics.record(RequestMetric(
        method=request.method,
        route=route_template,
        status=response.status_code,
        duration_ms=duration_ms,
        db_duration_ms=db_metric["duration_ms"],
        db_errors=db_metric["errors"],
    ))
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "method": request.method,
        "route": route_template,
        "status": response.status_code,
        "duration_ms": duration_ms,
    }
    for header, key in (
        ("X-Job-ID", "job_id"),
        ("X-Build-ID", "build_id"),
        ("X-Cluster-Run-ID", "cluster_run_id"),
    ):
        if response.headers.get(header):
            record[key] = response.headers[header]
    print(json.dumps(record, ensure_ascii=False))
    return response


@app.on_event("startup")
def _startup():
    db.init_db()


# ---------------------------------------------------------------- schemas --

class ReportIn(BaseModel):
    utility: Literal["electricity", "water"]
    status: Literal["active", "restored"]
    governorate: str
    delegation: Optional[str] = None
    zone_text: Optional[str] = None
    comment: str = ""
    started_at: Optional[str] = None  # ISO8601, optional
    ended_at: Optional[str] = None    # ISO8601, required if status == restored (soft-checked)

    @property
    def normalized_governorate(self) -> str:
        return self.governorate.strip()


CRON_SECRET = os.environ.get("CRON_SECRET")
OPS_SECRET = os.environ.get("OPS_SECRET")


def verify_cron_secret(x_cron_secret: str = Header(None)):
    if not CRON_SECRET or not hmac.compare_digest(
        x_cron_secret or "", CRON_SECRET
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Cron-Secret",)


def verify_ops_secret(x_ops_secret: str = Header(None)):
    if not OPS_SECRET:
        raise HTTPException(
            status_code=503, detail="Operations diagnostics unavailable"
        )
    if not hmac.compare_digest(x_ops_secret or "", OPS_SECRET):
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-Ops-Secret"
        )


def _decode_cursor(cursor: str | None):
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        return json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


def _encode_cursor(value: dict | None):
    if value is None:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


class ScrapeResult(BaseModel):
    notices_processed: int
    total_in_db: int


class BackfillStatus(BaseModel):
    running: bool
    page: int
    new_links_this_page: int
    imported: int
    total_in_db: int
    started_at: str | None
    finished_at: str | None
    error: str | None


class RecheckResult(BaseModel):
    status: str
    run_date: str
    notices_so_far: Optional[int] = None
    needed: Optional[int] = None
    localities_clustered: Optional[int] = None
    cluster_count: Optional[int] = None
    cluster_run_id: Optional[str] = None
    build_id: Optional[str] = None
    algorithm_version: Optional[str] = None
    readiness: Optional[dict] = None


class ClusterPoint(BaseModel):
    locality: str
    cluster_id: int
    stability: float
    lat: float
    lng: float


class ClustersResponse(BaseModel):
    data: list[ClusterPoint]
    clusters: list[ClusterPoint] = []
    insufficient_data: bool
    notices_so_far: int
    needed: int = cluster_inference.MIN_NOTICES
    cluster_run_id: str | None = None
    build_id: str | None = None
    active_build_id: str | None = None
    algorithm_version: str | None = None
    is_current: bool = False


class ModelStatusResponse(BaseModel):
    notices_so_far: int
    notices_needed: int
    localities_so_far: int
    localities_needed: int
    data_floor_met: bool
    cluster_count: int
    average_stability: float
    last_run_date: Optional[str] = None
    days_of_history: int
    model_quality: dict | None = None
    operational_health: dict | None = None


class CooccurrenceEdge(BaseModel):
    locality_a: str
    locality_b: str
    notice_count: int
    distinct_date_count: int = 0
    first_observed_on: str | None = None
    last_observed_on: str | None = None


class CooccurrencesResponse(BaseModel):
    edges: list[CooccurrenceEdge]


# -------------------------------------------------------------- endpoints --

@app.get("/api/governorates")
def get_governorates():
    return GOVERNORATES


@app.get("/api/official")
def get_official(region: Optional[str] = None,
                  limit: int = Query(100, le=500),
                  offset: int = 0):
    return {"notices": db.list_official_notices(region=region, limit=limit, offset=offset)}


@app.get("/api/reports")
def get_reports(utility: Optional[Literal["electricity", "water"]] = None,
                 status: Optional[Literal["active", "restored"]] = None,
                 governorate: Optional[str] = None,
                 limit: int = Query(200, le=500),
                 offset: int = 0):
    return {"reports": db.list_user_reports(
        utility=utility, status=status, governorate=governorate,
        limit=limit, offset=offset,
    )}


@app.post("/api/reports")
def post_report(report: ReportIn):
    gov = report.governorate.strip()
    if gov not in GOVERNORATE_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown governorate '{gov}'. Must be one of: {', '.join(GOVERNORATE_NAMES)}",
        )
    if report.status == "restored" and not report.ended_at:
        raise HTTPException(status_code=422, detail="ended_at is required when status is 'restored'")

    record = {
        "utility": report.utility,
        "status": report.status,
        "governorate": gov,
        "delegation": report.delegation,
        "zone_text": report.zone_text,
        "comment": report.comment or "",
        "started_at": report.started_at,
        "ended_at": report.ended_at,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    new_id = db.create_user_report(record)
    return {"id": new_id, **record}


@app.get("/api/stats")
def get_stats():
    overall = db.overall_stats()
    by_gov = db.stats_by_governorate()
    return {**overall, "by_governorate": by_gov}


@app.get("/api/status")
def get_public_status():
    return {
        "status": "ok",
        "active_build_id": db.active_build_id(),
        "active_cluster_run_id": db.active_cluster_run_id(),
        "stale_active_parse_count": db.stale_active_parse_count(),
    }


@app.get("/api/status/ingestion")
def get_public_ingestion_status():
    return {
        "scrape": db.latest_ingestion_run("scrape"),
        "backfill": db.latest_ingestion_run("backfill"),
    }


@app.get("/api/model-readiness")
def get_model_readiness():
    return model_readiness.evaluate()


@app.get("/api/edge-evidence")
def get_edge_evidence(locality_a: str, locality_b: str):
    if locality_a == locality_b:
        raise HTTPException(
            status_code=422, detail="locality names must be different"
        )
    build_id = db.active_build_id()
    if not build_id:
        raise HTTPException(status_code=404, detail="edge not found")
    evidence = db.edge_evidence(build_id, locality_a, locality_b)
    if evidence is None:
        raise HTTPException(status_code=404, detail="edge not found")
    return evidence


@app.get(
    "/api/internal/ops/jobs",
    dependencies=[Depends(verify_ops_secret)],
)
def get_ops_jobs(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
):
    rows = db.list_ingestion_runs(limit + 1, _decode_cursor(cursor))
    has_more = len(rows) > limit
    items = rows[:limit]
    last = items[-1] if has_more and items else None
    return {
        "items": items,
        "next_cursor": _encode_cursor(
            {"started_at": last["started_at"], "id": last["id"]}
            if last
            else None
        ),
    }


@app.get(
    "/api/internal/ops/jobs/{job_id}",
    dependencies=[Depends(verify_ops_secret)],
)
def get_ops_job(job_id: str):
    job = db.get_ingestion_run_public(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get(
    "/api/internal/ops/jobs/{job_id}/events",
    dependencies=[Depends(verify_ops_secret)],
)
def get_ops_job_events(
    job_id: str,
    limit: int = Query(200, ge=1, le=500),
    cursor: str | None = None,
):
    if db.get_ingestion_run_public(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    rows = db.list_job_events_page(
        job_id, limit + 1, _decode_cursor(cursor)
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    last = items[-1] if has_more and items else None
    return {
        "items": items,
        "next_cursor": _encode_cursor(
            {"occurred_at": last["occurred_at"], "id": last["id"]}
            if last
            else None
        ),
    }


@app.post("/api/internal/scrape", response_model=ScrapeResult, dependencies=[Depends(verify_cron_secret)])
def internal_scrape():
    try:
        count = import_official.run(verbose=False)
    except import_official.steg_scraper.FetchError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except observability.JobAlreadyRunning as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "job_already_running",
                "owner_job_id": e.owner_job_id,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape job failed: {e}")
    return ScrapeResult(notices_processed=count, total_in_db=db.count_official_notices())


@app.post("/api/internal/backfill", dependencies=[Depends(verify_cron_secret)])
def internal_backfill(background_tasks: BackgroundTasks):
    if backfill_official.get_status()["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(backfill_official.run_backfill_and_track_status)
    return {"status": "started"}


@app.get("/api/internal/backfill/status", response_model=BackfillStatus)
def internal_backfill_status():
    return BackfillStatus(**backfill_official.get_status())


@app.post("/api/internal/recluster", response_model=RecheckResult, dependencies=[Depends(verify_cron_secret)])
def internal_recluster(response: Response):
    try:
        result = cluster_inference.run_recluster()
        if result.get("build_id"):
            response.headers["X-Build-ID"] = result["build_id"]
        if result.get("cluster_run_id"):
            response.headers["X-Cluster-Run-ID"] = result["cluster_run_id"]
        return RecheckResult(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Recluster job failed: {e}")


@app.get("/api/clusters", response_model=ClustersResponse)
def get_clusters():
    notices_so_far = db.total_notice_count()
    versioned = db.active_cluster_run()
    if versioned is not None:
        points = [
            ClusterPoint(
                locality=row["locality"],
                cluster_id=row["cluster_id"],
                stability=row["stability"],
                lat=row["lat"],
                lng=row["lng"],
            )
            for row in versioned["rows"]
            if row["lat"] is not None and row["lng"] is not None
        ]
        active_build = db.active_build_id()
        return ClustersResponse(
            data=points,
            clusters=points,
            insufficient_data=False,
            notices_so_far=notices_so_far,
            cluster_run_id=versioned["run_id"],
            build_id=versioned["build_id"],
            active_build_id=active_build,
            algorithm_version=versioned["algorithm_version"],
            is_current=versioned["build_id"] == active_build,
        )
    latest = db.latest_cluster_run()
    if latest is None:
        return ClustersResponse(data=[], insufficient_data=True, notices_so_far=notices_so_far)
    points = [
        ClusterPoint(locality=r["locality"], cluster_id=r["cluster_id"],
                     stability=r["stability"], lat=r["lat"], lng=r["lng"])
        for r in latest["rows"] if r["lat"] is not None and r["lng"] is not None
    ]
    return ClustersResponse(data=points, insufficient_data=False, notices_so_far=notices_so_far)


@app.get("/api/model-status", response_model=ModelStatusResponse)
def get_model_status():
    notices_so_far = db.total_notice_count()
    localities_so_far = db.distinct_locality_count()
    latest = db.latest_cluster_run()

    cluster_count = 0
    average_stability = 0.0
    last_run_date = None
    if latest is not None:
        rows = latest["rows"]
        cluster_count = len({r["cluster_id"] for r in rows})
        if rows:
            average_stability = round(sum(r["stability"] for r in rows) / len(rows), 3)
        last_run_date = latest["run_date"]

    readiness = model_readiness.evaluate()
    return ModelStatusResponse(
        notices_so_far=notices_so_far,
        notices_needed=cluster_inference.MIN_NOTICES,
        localities_so_far=localities_so_far,
        localities_needed=cluster_inference.MIN_LOCALITIES,
        data_floor_met=(
            notices_so_far >= cluster_inference.MIN_NOTICES
            and localities_so_far >= cluster_inference.MIN_LOCALITIES
        ),
        cluster_count=cluster_count,
        average_stability=average_stability,
        last_run_date=last_run_date,
        days_of_history=db.count_cluster_run_dates(),
        model_quality=readiness.model_quality.model_dump(),
        operational_health=readiness.operational_health.model_dump(),
    )


@app.get("/api/cooccurrences", response_model=CooccurrencesResponse)
def get_cooccurrences():
    edges = [
        CooccurrenceEdge(
            locality_a=r["locality_a"],
            locality_b=r["locality_b"],
            notice_count=r["notice_count"],
            distinct_date_count=r.get("distinct_date_count", 0),
            first_observed_on=r.get("first_observed_on"),
            last_observed_on=r.get("last_observed_on"),
        )
        for r in db.list_cooccurrences()
    ]
    return CooccurrencesResponse(edges=edges)


from .api import docs as docs_api  # noqa: E402
from .api import ops as ops_api  # noqa: E402

app.include_router(ops_api.create_router(verify_ops_secret))
docs_api.install(app, verify_ops_secret)


# ------------------------------------------------------------ static site --

app.mount("/", StaticFiles(directory="static", html=True), name="static")
