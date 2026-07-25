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
import os
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import cluster_inference
from . import db
from . import import_official
from .governorates import GOVERNORATE_NAMES, GOVERNORATES

app = FastAPI(title="Tunisia Outage Tracker")


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


def verify_cron_secret(x_cron_secret: str = Header(None)):
    if not CRON_SECRET or not hmac.compare_digest(x_cron_secret or "", CRON_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Cron-Secret")


class ScrapeResult(BaseModel):
    notices_processed: int
    total_in_db: int


class RecheckResult(BaseModel):
    status: str
    run_date: str
    notices_so_far: Optional[int] = None
    needed: Optional[int] = None
    localities_clustered: Optional[int] = None
    cluster_count: Optional[int] = None


class ClusterPoint(BaseModel):
    locality: str
    cluster_id: int
    stability: float
    lat: float
    lng: float


class ClustersResponse(BaseModel):
    data: list[ClusterPoint]
    insufficient_data: bool
    notices_so_far: int
    needed: int = cluster_inference.MIN_NOTICES


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


class CooccurrenceEdge(BaseModel):
    locality_a: str
    locality_b: str
    notice_count: int


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


@app.post("/api/internal/scrape", response_model=ScrapeResult, dependencies=[Depends(verify_cron_secret)])
def internal_scrape():
    try:
        count = import_official.run(verbose=False)
    except import_official.steg_scraper.FetchError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return ScrapeResult(notices_processed=count, total_in_db=db.count_official_notices())


@app.post("/api/internal/recluster", response_model=RecheckResult, dependencies=[Depends(verify_cron_secret)])
def internal_recluster():
    try:
        return RecheckResult(**cluster_inference.run_recluster())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Recluster job failed: {e}")


@app.get("/api/clusters", response_model=ClustersResponse)
def get_clusters():
    notices_so_far = db.total_notice_count()
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
    )


@app.get("/api/cooccurrences", response_model=CooccurrencesResponse)
def get_cooccurrences():
    edges = [
        CooccurrenceEdge(locality_a=r["locality_a"], locality_b=r["locality_b"], notice_count=r["notice_count"])
        for r in db.list_cooccurrences()
    ]
    return CooccurrencesResponse(edges=edges)


# ------------------------------------------------------------ static site --

app.mount("/", StaticFiles(directory="static", html=True), name="static")
