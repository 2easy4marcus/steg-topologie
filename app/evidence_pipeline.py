"""Atomic orchestration for evidence, model-build, and cluster state."""

import hashlib
import json
import logging
from datetime import datetime
from uuid import uuid4

from . import db, locality_dedup, steg_scraper
from .evidence_models import (
    ParsedLocality,
    ParsedNoticeEvidence,
    ParseStatus,
)

logger = logging.getLogger(__name__)


def processing_identity(
    snapshot_id: str, parser_version: str, normalization_version: str
) -> str:
    value = "\0".join(
        [snapshot_id, parser_version, normalization_version]
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def infer_scope_ordinals(subregion_names: list) -> list:
    """Reconstruct cell boundaries from stored subregion names alone.

    Used when a parse is rebuilt from `notice_localities` rows rather than
    from HTML, which is how `reparse_snapshots` migrates parses written before
    scope ordinals existed. Localities keep their emission order, so a change
    of subregion name marks a cell boundary.

    ponytail: two *adjacent* cells that shared the identical heading text
    merge into one scope here -- exactly the case scope ordinals exist to
    separate. It is unavoidable without the HTML, and it only affects parses
    written before this parser version. Upgrade path: re-run the HTML parser
    over `notice_snapshots.raw_html` instead of calling this.
    """
    if all(name is None for name in subregion_names):
        return [None] * len(subregion_names)
    ordinals = []
    scope_ordinal = -1
    previous = object()
    for name in subregion_names:
        if name != previous:
            scope_ordinal += 1
            previous = name
        ordinals.append(scope_ordinal)
    return ordinals


def _normalized_notice_date(raw_date: str | None):
    if not raw_date:
        return None
    try:
        return datetime.strptime(raw_date, "%d/%m/%Y").date()
    except ValueError:
        return None


def evidence_from_notice(
    notice: dict, *, snapshot_id: str
) -> ParsedNoticeEvidence:
    warnings = []
    raw_date = notice.get("notice_date")
    normalized_date = _normalized_notice_date(raw_date)
    if not raw_date:
        warnings.append("missing_notice_date")
    elif normalized_date is None:
        warnings.append("invalid_notice_date")
    if steg_scraper.NOTICE_TITLE_MARKER not in notice.get("title", ""):
        warnings.append("unmatched_notice_title")

    raw_entries = []
    subregions = notice.get("subregions") or []
    if subregions:
        # The cell's position in the table is its scope identity. Cells are
        # numbered even when their heading is missing or duplicated, so
        # neither case can collapse two scopes into one.
        for scope_ordinal, subregion in enumerate(subregions):
            name = subregion.get("name")
            zones = [zone for zone in subregion.get("zones", []) if zone]
            if zones and not name:
                warnings.append("missing_subregion_header")
            raw_entries.extend((zone, name, scope_ordinal) for zone in zones)
    else:
        raw_entries.extend(
            (zone, None, None) for zone in notice.get("zones", []) if zone
        )

    localities = [
        ParsedLocality(
            raw_name=raw_name,
            canonical_name=locality_dedup.resolve_locality(raw_name),
            subregion_name=subregion_name,
            ordinal=ordinal,
            scope_ordinal=scope_ordinal,
        )
        for ordinal, (raw_name, subregion_name, scope_ordinal) in enumerate(
            raw_entries
        )
    ]
    if not localities:
        warnings.append("empty_locality_list")

    warnings = list(dict.fromkeys(warnings))
    return ParsedNoticeEvidence(
        notice_id=notice["id"],
        snapshot_id=snapshot_id,
        source_url=notice["url"],
        title=notice.get("title", ""),
        notice_date_raw=raw_date,
        notice_date_iso=normalized_date,
        parser_version=steg_scraper.PARSER_VERSION,
        normalization_version=locality_dedup.NORMALIZATION_VERSION,
        parse_status=ParseStatus.WARNING if warnings else ParseStatus.OK,
        localities=localities,
        warnings=warnings,
    )


def _raw_snapshot(notice: dict) -> str:
    raw_html = notice.get("raw_html")
    if raw_html:
        return raw_html
    return json.dumps(
        {
            "title": notice.get("title"),
            "notice_date": notice.get("notice_date"),
            "zones": notice.get("zones", []),
            "subregions": notice.get("subregions", []),
            "raw_text": notice.get("raw_text"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def persist_notice(notice: dict, *, fetched_at: str) -> bool:
    """Persist and activate a versioned notice parse.

    Return True only when the notice's active evidence changes.
    """
    raw_html = _raw_snapshot(notice)
    digest = hashlib.sha256(raw_html.encode("utf-8")).hexdigest()
    before = db.get_notice_state(notice["id"])
    previous_parse = before["active_parse_id"] if before else None
    snapshot, _ = db.get_or_create_snapshot(
        notice["id"],
        notice["url"],
        digest,
        raw_html,
        fetched_at,
    )
    db.select_latest_snapshot(notice["id"], snapshot["snapshot_id"], fetched_at)
    evidence = evidence_from_notice(
        notice, snapshot_id=snapshot["snapshot_id"]
    )
    db.save_parse_with_localities(evidence, fetched_at)
    parse_id = processing_identity(
        snapshot["snapshot_id"],
        evidence.parser_version,
        evidence.normalization_version,
    )
    try:
        db.activate_notice_parse(notice["id"], parse_id, fetched_at)
    except ValueError as exc:
        # A refused activation is an expected outcome, not a crash: either the
        # parse no longer belongs to the notice's latest snapshot, or its
        # parse_status makes it ineligible (only 'ok', or 'warning' with 2+
        # distinct localities, may go live). The notice simply keeps whatever
        # parse it already had and this function reports "unchanged", which is
        # correct -- one bad notice must not abort a whole ingestion job.
        #
        # It must NOT, however, be invisible: swallowing this silently is how a
        # production bug that refused EVERY activation went unnoticed for days.
        # There is no job_id at this call site (persist_notice is called from
        # import_official.process_notice, which has none), so this is a log
        # only -- no job event.
        logger.warning(
            "notice parse activation refused: notice_id=%s parse_id=%s "
            "snapshot_id=%s parse_status=%s localities=%d reason=%s",
            notice["id"],
            parse_id,
            snapshot["snapshot_id"],
            evidence.parse_status.value,
            len(evidence.localities),
            exc,
        )
    after = db.get_notice_state(notice["id"])
    return after["active_parse_id"] != previous_parse


def activate_parse(notice_id: str, parse_id: str, activated_at: str) -> None:
    db.activate_notice_parse(notice_id, parse_id, activated_at)


def activate_model_build(build_id: str) -> None:
    db.activate_completed_model_build(build_id)


def activate_cluster_run(run_id: str) -> None:
    db.activate_completed_cluster_run(run_id)


def _as_datetime(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _publication_decision(readiness) -> str:
    """Map readiness to a publication state.

    The evidence build itself still activates regardless -- operators must be
    able to inspect an unready build. It is cluster publication that consumes
    this decision, which is why the decision is stored rather than acted on
    here.
    """
    if not readiness.model_quality.ready:
        return "blocked"
    if not readiness.operational_health.ready:
        return "experimental"
    return "published"


def build_model_evidence(
    *,
    created_at: str,
    activate: bool = True,
    job_id: str | None = None,
) -> str:
    """Create, validate, and atomically activate one immutable build."""
    from . import model_readiness, observability
    from .model.config import CONFIG

    build_id = uuid4().hex
    if job_id:
        observability.record_job_event(
            job_id, "build_started", occurred_at=created_at
        )
    db.create_model_build(build_id, created_at)
    # Pin this build's source population before measuring anything. Every
    # later step reads only the pinned rows, so a parser activation landing
    # mid-build cannot change what this build observed.
    db.pin_build_snapshot(build_id)
    db.populate_scoped_observations(build_id, CONFIG)
    db.populate_model_build(build_id)
    db.validate_model_build(build_id)
    if job_id:
        observability.record_job_event(
            job_id, "build_validated", occurred_at=created_at
        )
    notice_count, locality_count, pair_count = db.model_build_counts(build_id)
    db.complete_model_build(
        build_id,
        created_at,
        notice_count,
        locality_count,
        pair_count,
    )
    readiness = model_readiness.evaluate(
        now=_as_datetime(created_at), build_id=build_id
    )
    db.record_quality_gates(build_id, readiness, CONFIG.version, created_at)
    db.record_publication_decision(
        "evidence_build",
        build_id,
        build_id=build_id,
        decision=_publication_decision(readiness),
        config_version=CONFIG.version,
        decided_at=created_at,
    )
    if activate:
        db.activate_completed_model_build(build_id)
        if job_id:
            observability.record_job_event(
                job_id, "build_activated", occurred_at=created_at
            )
    return build_id
