"""Dry-run-first parser and normalizer migration for stored snapshots."""

import argparse
import json
from datetime import date, datetime, timezone

from . import db, evidence_pipeline, locality_dedup, steg_scraper
from .evidence_models import (
    ParsedLocality,
    ParsedNoticeEvidence,
    ParseStatus,
)


def _evidence(row):
    parsed_date = (
        date.fromisoformat(row["notice_date_iso"])
        if row["notice_date_iso"]
        else None
    )
    # Parses written before scope ordinals existed have none stored, so cell
    # boundaries are reconstructed from the stored subregion names. See
    # evidence_pipeline.infer_scope_ordinals for the known limit.
    scope_ordinals = [
        item["scope_ordinal"] for item in row["localities"]
    ]
    if any(value is None for value in scope_ordinals):
        scope_ordinals = evidence_pipeline.infer_scope_ordinals(
            [item["subregion_name"] for item in row["localities"]]
        )
    return ParsedNoticeEvidence(
        notice_id=row["notice_id"],
        snapshot_id=row["latest_snapshot_id"],
        source_url=row["source_url"],
        title=row["title"],
        notice_date_raw=row["notice_date_raw"],
        notice_date_iso=parsed_date,
        parser_version=steg_scraper.PARSER_VERSION,
        normalization_version=locality_dedup.NORMALIZATION_VERSION,
        parse_status=ParseStatus(row["parse_status"]),
        localities=[
            ParsedLocality(
                raw_name=item["raw_name"],
                canonical_name=locality_dedup.resolve_locality(
                    item["raw_name"]
                ),
                subregion_name=item["subregion_name"],
                ordinal=item["ordinal"],
                scope_ordinal=scope_ordinal,
            )
            for item, scope_ordinal in zip(row["localities"], scope_ordinals)
        ],
        warnings=json.loads(row["parse_warnings"] or "[]"),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    candidates = db.snapshots_missing_current_parse(
        steg_scraper.PARSER_VERSION,
        locality_dedup.NORMALIZATION_VERSION,
    )
    if not args.apply:
        print(f"DRY-RUN: {len(candidates)} snapshots require reparse")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    for row in candidates:
        evidence = _evidence(row)
        db.save_parse_with_localities(evidence, now)
        parse_id = db.processing_parse_id(
            evidence.snapshot_id,
            evidence.parser_version,
            evidence.normalization_version,
        )
        db.activate_notice_parse(evidence.notice_id, parse_id, now)
    print(f"Applied {len(candidates)} reparses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
