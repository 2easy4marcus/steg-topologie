"""Typed contracts shared by evidence ingestion, clustering, and APIs."""

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class ParseStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"


class BuildStatus(str, Enum):
    BUILDING = "building"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ParsedLocality(BaseModel):
    raw_name: str
    canonical_name: str
    subregion_name: str | None = None
    ordinal: int = Field(ge=0)


class ParsedNoticeEvidence(BaseModel):
    notice_id: str
    snapshot_id: str
    source_url: str
    title: str
    notice_date_raw: str | None = None
    notice_date_iso: date | None = None
    parser_version: str
    normalization_version: str
    parse_status: ParseStatus
    localities: list[ParsedLocality]
    warnings: list[str]


class PublicJobError(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ClusterRunMetadata(BaseModel):
    cluster_run_id: str
    build_id: str
    active_build_id: str
    algorithm_version: str
    is_current: bool
    completed_at: datetime
