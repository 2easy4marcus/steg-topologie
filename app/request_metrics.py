"""Bounded in-memory request metrics and a per-request libSQL accumulator.

Nothing here persists to the DB or logs SQL/params -- only timings and counts.
"""
from collections import Counter, deque
from contextvars import ContextVar
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


# -- Per-request libSQL accumulator ---------------------------------------
#
# FastAPI sync handlers run in an anyio worker thread with a COPIED contextvars
# context, so we cannot use _db_state.set() from inside a handler and read it
# back in the async middleware -- the replacement would be invisible across the
# thread boundary. Instead reset_db_metrics() installs a FRESH mutable dict per
# request in the event-loop context; that same object is shared by reference
# into the worker-thread copy, and record_db_call() mutates it in place under a
# lock so accumulation is visible on both sides.
_db_state: ContextVar[dict] = ContextVar("db_state", default={"duration_ms": 0.0, "errors": 0})
_lock = Lock()


def reset_db_metrics() -> None:
    _db_state.set({"duration_ms": 0.0, "errors": 0})


def record_db_call(duration_ms: float, *, failed: bool) -> None:
    state = _db_state.get()
    with _lock:  # ponytail: single global lock; per-request state so contention is nil
        state["duration_ms"] += duration_ms
        state["errors"] += int(failed)


def current_db_metrics() -> dict:
    return dict(_db_state.get())
