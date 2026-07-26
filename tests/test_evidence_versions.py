from datetime import date

from app import evidence_pipeline, locality_dedup, steg_scraper
from app.evidence_models import ParseStatus


def _notice(**overrides):
    notice = {
        "id": "notice-1",
        "title": "إشعار بانقطاع الكهرباء - جهة الشمال - 11:00 26/07/2026",
        "url": "https://www.steg.com.tn/fr/news/notice-1",
        "notice_date": "26/07/2026",
        "zones": ["قليبية", "حمام لغزاز"],
        "subregions": [],
        "raw_html": "<html>قليبية حمام لغزاز</html>",
    }
    notice.update(overrides)
    return notice


def test_parser_and_normalizer_have_explicit_versions():
    assert steg_scraper.PARSER_VERSION == "2"
    assert locality_dedup.NORMALIZATION_VERSION == "1"


def test_notice_conversion_preserves_raw_date_and_normalizes_iso():
    evidence = evidence_pipeline.evidence_from_notice(
        _notice(), snapshot_id="snapshot-1"
    )

    assert evidence.notice_date_raw == "26/07/2026"
    assert evidence.notice_date_iso == date(2026, 7, 26)
    assert evidence.parser_version == "2"
    assert evidence.normalization_version == "1"
    assert evidence.parse_status is ParseStatus.OK


def test_invalid_date_is_a_stable_warning():
    evidence = evidence_pipeline.evidence_from_notice(
        _notice(notice_date="not-a-date"), snapshot_id="snapshot-1"
    )

    assert evidence.notice_date_iso is None
    assert "invalid_notice_date" in evidence.warnings
    assert evidence.parse_status is ParseStatus.WARNING


def test_headerless_table_localities_are_preserved_with_warning():
    evidence = evidence_pipeline.evidence_from_notice(
        _notice(
            zones=[],
            subregions=[{"name": None, "zones": ["قليبية", "حمام لغزاز"]}],
        ),
        snapshot_id="snapshot-1",
    )

    assert [item.raw_name for item in evidence.localities] == [
        "قليبية",
        "حمام لغزاز",
    ]
    assert "missing_subregion_header" in evidence.warnings


def test_empty_locality_list_is_warning_and_cannot_look_successful():
    evidence = evidence_pipeline.evidence_from_notice(
        _notice(zones=[], subregions=[]), snapshot_id="snapshot-1"
    )

    assert evidence.localities == []
    assert evidence.parse_status is ParseStatus.WARNING
    assert "empty_locality_list" in evidence.warnings


def test_processing_identity_changes_with_parser_or_normalizer_version():
    base = evidence_pipeline.processing_identity("snapshot-1", "2", "1")

    assert base != evidence_pipeline.processing_identity("snapshot-1", "3", "1")
    assert base != evidence_pipeline.processing_identity("snapshot-1", "2", "2")

