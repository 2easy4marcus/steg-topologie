"""Atomic orchestration for evidence, model-build, and cluster state."""

import hashlib
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


def activate_parse(notice_id: str, parse_id: str, activated_at: str) -> None:
    db.activate_notice_parse(notice_id, parse_id, activated_at)


def activate_model_build(build_id: str) -> None:
    db.activate_completed_model_build(build_id)


def activate_cluster_run(run_id: str) -> None:
    db.activate_completed_cluster_run(run_id)


def build_model_evidence(*, created_at: str) -> str:
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
    db.activate_completed_model_build(build_id)
    return build_id
