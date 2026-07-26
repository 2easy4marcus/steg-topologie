import pytest

from app import db, observability


def test_job_events_are_ordered_and_use_approved_messages():
    observability.record_job_event(
        "job-1",
        "job_started",
        occurred_at="2026-07-26T10:00:00Z",
        request_id="request-1",
    )
    observability.record_job_event(
        "job-1",
        "page_completed",
        occurred_at="2026-07-26T10:01:00Z",
        current_page=3,
        request_id="request-1",
    )

    events = db.list_job_events("job-1", limit=20)

    assert [event["event_type"] for event in events] == [
        "job_started",
        "page_completed",
    ]
    assert events[1]["public_message"] == "Archive page completed."
    assert events[1]["current_page"] == 3


def test_unapproved_event_type_is_rejected():
    with pytest.raises(ValueError, match="event type"):
        observability.record_job_event(
            "job-1",
            "sql_executed",
            occurred_at="2026-07-26T10:00:00Z",
        )


def test_arbitrary_exception_text_cannot_be_persisted_as_public_message():
    with pytest.raises(TypeError):
        observability.record_job_event(
            "job-1",
            "job_failed",
            occurred_at="2026-07-26T10:00:00Z",
            message="token=secret",
        )

