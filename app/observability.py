"""Safe job coordination and operational metadata."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import db

EVENT_MESSAGES = {
    "job_started": ("info", "Job started."),
    "page_started": ("info", "Archive page started."),
    "page_completed": ("info", "Archive page completed."),
    "notice_imported": ("info", "Notice imported."),
    "notice_unchanged": ("info", "Notice unchanged."),
    "notice_skipped": ("info", "Notice skipped."),
    "notice_failed": ("warning", "Notice processing failed."),
    "parse_failed": ("warning", "Notice parsing failed."),
    "build_started": ("info", "Evidence build started."),
    "build_validated": ("info", "Evidence build validated."),
    "build_activated": ("info", "Evidence build activated."),
    "cluster_started": ("info", "Cluster run started."),
    "cluster_activated": ("info", "Cluster run activated."),
    "job_completed": ("info", "Job completed."),
    "job_failed": ("error", "Job failed."),
}


@dataclass(frozen=True)
class LockResult:
    acquired: bool
    owner_job_id: str


class JobAlreadyRunning(RuntimeError):
    def __init__(self, owner_job_id: str):
        super().__init__(f"job already running: {owner_job_id}")
        self.owner_job_id = owner_job_id


def acquire_evidence_job_lock(
    *, ttl_minutes: int
) -> tuple[str, LockResult]:
    now = datetime.now(timezone.utc)
    owner_job_id = uuid4().hex
    result = acquire_job_lock(
        "evidence-pipeline",
        owner_job_id,
        acquired_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(),
    )
    if not result.acquired:
        raise JobAlreadyRunning(result.owner_job_id)
    return owner_job_id, result


def acquire_job_lock(
    lock_name: str,
    owner_job_id: str,
    *,
    acquired_at: str,
    expires_at: str,
) -> LockResult:
    acquired, owner = db.acquire_lock(
        lock_name, owner_job_id, acquired_at, expires_at
    )
    return LockResult(acquired=acquired, owner_job_id=owner)


def heartbeat_job_lock(
    lock_name: str,
    owner_job_id: str,
    *,
    heartbeat_at: str,
    expires_at: str,
) -> bool:
    return db.heartbeat_lock(
        lock_name, owner_job_id, heartbeat_at, expires_at
    )


def release_job_lock(lock_name: str, owner_job_id: str) -> bool:
    return db.release_lock(lock_name, owner_job_id)


def record_job_event(
    job_id: str,
    event_type: str,
    *,
    occurred_at: str,
    current_page: int | None = None,
    request_id: str | None = None,
) -> None:
    if event_type not in EVENT_MESSAGES:
        raise ValueError("unsupported job event type")
    level, message = EVENT_MESSAGES[event_type]
    db.insert_job_event(
        uuid4().hex,
        job_id,
        occurred_at,
        level,
        event_type,
        message,
        current_page,
        request_id,
    )
