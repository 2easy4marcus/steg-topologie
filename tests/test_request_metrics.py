import contextvars
import threading

from app.request_metrics import (
    RequestMetric,
    RequestMetrics,
    current_db_metrics,
    record_db_call,
    reset_db_metrics,
)


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


def test_db_metrics_cross_thread_boundary():
    """Starlette runs sync handlers in a worker thread with a COPIED
    contextvars context. record_db_call must mutate the shared dict in place
    so the accumulation is visible back in the calling (event-loop) context.
    A .set()-replacement implementation makes this test fail."""
    reset_db_metrics()

    ctx = contextvars.copy_context()

    def worker():
        record_db_call(12.5, failed=False)
        record_db_call(7.5, failed=True)

    thread = threading.Thread(target=lambda: ctx.run(worker))
    thread.start()
    thread.join()

    seen = current_db_metrics()
    assert seen["duration_ms"] == 20.0
    assert seen["errors"] == 1


def test_reset_isolates_requests():
    reset_db_metrics()
    record_db_call(5.0, failed=False)
    assert current_db_metrics()["duration_ms"] == 5.0
    reset_db_metrics()
    assert current_db_metrics() == {"duration_ms": 0.0, "errors": 0}


class _FakeClient:
    def __init__(self, raise_on_execute=False):
        self.raise_on_execute = raise_on_execute
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.raise_on_execute:
            raise RuntimeError("db down")

        class _RS:
            rows = []
            columns = []
            last_insert_rowid = 0
            rows_affected = 0

        return _RS()


def test_execute_records_success_without_sql_or_params():
    from app.db import _Conn

    reset_db_metrics()
    conn = _Conn(_FakeClient())
    conn.execute("SELECT secret_col FROM secrets", ["secret-param"])

    seen = current_db_metrics()
    assert seen["errors"] == 0
    assert seen["duration_ms"] >= 0.0
    assert "secret_col" not in str(seen)
    assert "secret-param" not in str(seen)


def test_execute_records_failure():
    from app.db import _Conn

    reset_db_metrics()
    conn = _Conn(_FakeClient(raise_on_execute=True))
    try:
        conn.execute("SELECT 1", None)
    except RuntimeError:
        pass

    seen = current_db_metrics()
    assert seen["errors"] == 1
    assert seen["duration_ms"] >= 0.0
