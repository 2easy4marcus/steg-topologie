"""Safe job coordination and operational metadata."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import db


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
