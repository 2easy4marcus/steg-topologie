from app import observability


def test_lock_is_atomic_and_reports_current_owner():
    first = observability.acquire_job_lock(
        "evidence-pipeline",
        "job-1",
        acquired_at="2026-07-26T10:00:00Z",
        expires_at="2026-07-26T10:15:00Z",
    )
    second = observability.acquire_job_lock(
        "evidence-pipeline",
        "job-2",
        acquired_at="2026-07-26T10:01:00Z",
        expires_at="2026-07-26T10:16:00Z",
    )

    assert first.acquired is True
    assert second.acquired is False
    assert second.owner_job_id == "job-1"


def test_expired_lock_can_be_recovered():
    observability.acquire_job_lock(
        "evidence-pipeline",
        "job-1",
        acquired_at="2026-07-26T10:00:00Z",
        expires_at="2026-07-26T10:15:00Z",
    )

    result = observability.acquire_job_lock(
        "evidence-pipeline",
        "job-2",
        acquired_at="2026-07-26T10:16:00Z",
        expires_at="2026-07-26T10:31:00Z",
    )

    assert result.acquired is True
    assert result.owner_job_id == "job-2"


def test_only_owner_can_heartbeat_or_release_lock():
    observability.acquire_job_lock(
        "evidence-pipeline",
        "job-1",
        acquired_at="2026-07-26T10:00:00Z",
        expires_at="2026-07-26T10:15:00Z",
    )

    assert observability.heartbeat_job_lock(
        "evidence-pipeline",
        "job-2",
        heartbeat_at="2026-07-26T10:01:00Z",
        expires_at="2026-07-26T10:16:00Z",
    ) is False
    assert observability.release_job_lock("evidence-pipeline", "job-2") is False
    assert observability.release_job_lock("evidence-pipeline", "job-1") is True

