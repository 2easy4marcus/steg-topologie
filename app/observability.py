"""Safe job coordination and operational metadata."""

from dataclasses import dataclass

from . import db


@dataclass(frozen=True)
class LockResult:
    acquired: bool
    owner_job_id: str


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
