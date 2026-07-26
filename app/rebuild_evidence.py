"""Dry-run-first command for rebuilding aggregate evidence."""

import argparse
from datetime import datetime, timezone

from . import db, evidence_pipeline


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    active = db.active_build_id()
    if not args.apply:
        metrics = db.model_readiness_metrics(active)
        print(f"DRY-RUN: active_build={active or 'none'} metrics={metrics}")
        return 0
    build_id = evidence_pipeline.build_model_evidence(
        created_at=datetime.now(timezone.utc).isoformat()
    )
    print(f"Applied evidence build {build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
