"""Evidence-quality and operational-health readiness evaluation."""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from . import db
from .model.config import CONFIG


class ReadinessSignal(BaseModel):
    key: str
    current: float | None
    required: float
    operator: str
    passed: bool
    explanation: str


class ReadinessSection(BaseModel):
    ready: bool
    signals: list[ReadinessSignal]


class ReadinessReport(BaseModel):
    build_id: str | None
    model_quality: ReadinessSection
    operational_health: ReadinessSection


def _minimum_signal(key, current, required, explanation):
    return ReadinessSignal(
        key=key,
        current=float(current),
        required=float(required),
        operator=">=",
        passed=current >= required,
        explanation=explanation,
    )


def _maximum_signal(key, current, required, explanation):
    return ReadinessSignal(
        key=key,
        current=float(current),
        required=float(required),
        operator="<=",
        passed=current <= required,
        explanation=explanation,
    )


def empty_readiness() -> ReadinessReport:
    return evaluate(build_id=None)


def evaluate(
    *,
    now: datetime | None = None,
    build_id: str | None = None,
) -> ReadinessReport:
    now = now or datetime.now(timezone.utc)
    if build_id is None:
        build_id = db.active_build_id()
    metrics = db.model_readiness_metrics(build_id)
    quality_signals = [
        _minimum_signal(
            "valid_notices",
            metrics["valid_notices"],
            CONFIG.min_valid_notices,
            "Notices contributing at least one scoped locality pair.",
        ),
        _minimum_signal(
            "distinct_outage_dates",
            metrics["distinct_outage_dates"],
            CONFIG.min_distinct_dates,
            "Distinct source outage dates represented by valid notices.",
        ),
        _minimum_signal(
            "unique_localities",
            metrics["unique_localities"],
            CONFIG.min_localities,
            "Distinct canonical localities in the active evidence build.",
        ),
        _minimum_signal(
            "repeated_pairs",
            metrics["repeated_pairs"],
            CONFIG.min_repeated_pairs,
            "Pairs supported by at least two distinct outage dates.",
        ),
        _minimum_signal(
            "active_ok_ratio",
            metrics["active_ok_ratio"],
            CONFIG.min_active_ok_ratio,
            "Share of active valid parses without warnings.",
        ),
        _maximum_signal(
            "largest_notice_pair_share",
            metrics["largest_notice_pair_share"],
            CONFIG.max_largest_notice_share,
            "Largest single-notice share of all scoped pair observations.",
        ),
    ]

    cutoff = now - timedelta(days=CONFIG.recent_parse_window_days)
    operational = db.operational_health_metrics(cutoff.isoformat())
    last_scrape = operational["last_successful_scrape_at"]
    scrape_age = None
    if last_scrape:
        parsed = datetime.fromisoformat(last_scrape.replace("Z", "+00:00"))
        scrape_age = max(0.0, (now - parsed).total_seconds() / 3600)
    operational_signals = [
        _minimum_signal(
            "recent_parse_success_ratio",
            operational["recent_parse_success_ratio"],
            CONFIG.min_recent_parse_ratio,
            "Latest parse attempts for recently selected snapshots.",
        ),
        ReadinessSignal(
            key="scrape_freshness",
            current=scrape_age,
            required=CONFIG.max_scrape_age_hours,
            operator="<=",
            passed=scrape_age is not None
            and scrape_age <= CONFIG.max_scrape_age_hours,
            explanation="Hours since the last successful scheduled scrape.",
        ),
    ]

    return ReadinessReport(
        build_id=build_id,
        model_quality=ReadinessSection(
            ready=all(signal.passed for signal in quality_signals),
            signals=quality_signals,
        ),
        operational_health=ReadinessSection(
            ready=all(signal.passed for signal in operational_signals),
            signals=operational_signals,
        ),
    )
