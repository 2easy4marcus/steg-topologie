"""Atomic orchestration for evidence, model-build, and cluster state."""

from . import db


def activate_parse(notice_id: str, parse_id: str, activated_at: str) -> None:
    db.activate_notice_parse(notice_id, parse_id, activated_at)


def activate_model_build(build_id: str) -> None:
    db.activate_completed_model_build(build_id)


def activate_cluster_run(run_id: str) -> None:
    db.activate_completed_cluster_run(run_id)
