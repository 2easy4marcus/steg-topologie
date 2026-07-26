from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.evidence_models import (
    BuildStatus,
    ClusterRunMetadata,
    JobStatus,
    ParsedLocality,
    ParsedNoticeEvidence,
    ParseStatus,
)


def test_evidence_contract_preserves_raw_and_normalized_values():
    evidence = ParsedNoticeEvidence(
        notice_id="notice-123",
        snapshot_id="snapshot-123",
        source_url="https://www.steg.com.tn/fr/news/example",
        title="إشعار بانقطاع الكهرباء",
        notice_date_raw="26/07/2026",
        notice_date_iso=date(2026, 7, 26),
        parser_version="2",
        normalization_version="1",
        parse_status=ParseStatus.OK,
        localities=[
            ParsedLocality(
                raw_name="  قليبية ",
                canonical_name="قليبية",
                subregion_name="جهة الوطن القبلي",
                ordinal=0,
            )
        ],
        warnings=[],
    )

    assert evidence.notice_date_raw == "26/07/2026"
    assert evidence.notice_date_iso.isoformat() == "2026-07-26"
    assert evidence.localities[0].raw_name == "  قليبية "


@pytest.mark.parametrize(
    ("model", "value"),
    [
        (ParseStatus, "partial"),
        (BuildStatus, "ready"),
        (JobStatus, "done"),
    ],
)
def test_status_enums_reject_unknown_values(model, value):
    with pytest.raises(ValueError):
        model(value)


def test_locality_rejects_negative_ordinal():
    with pytest.raises(ValidationError):
        ParsedLocality(raw_name="A", canonical_name="A", ordinal=-1)


def test_evidence_rejects_non_iso_normalized_date():
    with pytest.raises(ValidationError):
        ParsedNoticeEvidence(
            notice_id="notice-123",
            snapshot_id="snapshot-123",
            source_url="https://example.test",
            title="Notice",
            notice_date_iso="26/07/2026",
            parser_version="2",
            normalization_version="1",
            parse_status="ok",
            localities=[],
            warnings=[],
        )


def test_cluster_metadata_requires_build_identity():
    with pytest.raises(ValidationError):
        ClusterRunMetadata(
            cluster_run_id="run-123",
            algorithm_version="ppmi-louvain-v1",
            is_current=True,
            completed_at=datetime.now(timezone.utc),
        )

