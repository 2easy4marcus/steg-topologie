"""Atomic orchestration for evidence, model-build, and cluster state."""

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from . import db, locality_dedup, steg_scraper
from .evidence_models import (
    ParsedLocality,
    ParsedNoticeEvidence,
    ParseStatus,
)


def processing_identity(
    snapshot_id: str, parser_version: str, normalization_version: str
) -> str:
    value = "\0".join(
        [snapshot_id, parser_version, normalization_version]
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


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
        for subregion in subregions:
            name = subregion.get("name")
            zones = [zone for zone in subregion.get("zones", []) if zone]
            if zones and not name:
                warnings.append("missing_subregion_header")
            raw_entries.extend((zone, name) for zone in zones)
    else:
        raw_entries.extend(
            (zone, None) for zone in notice.get("zones", []) if zone
        )

    localities = [
        ParsedLocality(
            raw_name=raw_name,
            canonical_name=locality_dedup.resolve_locality(raw_name),
            subregion_name=subregion_name,
            ordinal=ordinal,
        )
        for ordinal, (raw_name, subregion_name) in enumerate(raw_entries)
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
    except ValueError:
        pass
    after = db.get_notice_state(notice["id"])
    return after["active_parse_id"] != previous_parse


def activate_parse(notice_id: str, parse_id: str, activated_at: str) -> None:
    db.activate_notice_parse(notice_id, parse_id, activated_at)


def activate_model_build(build_id: str) -> None:
    db.activate_completed_model_build(build_id)


def activate_cluster_run(run_id: str) -> None:
    db.activate_completed_cluster_run(run_id)


def build_model_evidence(*, created_at: str, activate: bool = True) -> str:
    """Create, validate, and atomically activate one immutable build."""
    build_id = uuid4().hex
    db.create_model_build(build_id, created_at)
    db.populate_model_build(build_id)
    db.validate_model_build(build_id)
    notice_count, locality_count, pair_count = db.model_build_counts(build_id)
    db.complete_model_build(
        build_id,
        created_at,
        notice_count,
        locality_count,
        pair_count,
    )
    if activate:
        db.activate_completed_model_build(build_id)
    return build_id
