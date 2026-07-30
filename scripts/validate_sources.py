"""Validate docs/data/sources.yaml against the registry contracts.

Schema validation (every `sources`/`artifacts` row parses as a
DatasetSource/SourceArtifact, including "public requires license_id") always
runs. Real-file checksum verification only runs when STEG_SOURCE_ROOT is
set -- otherwise each source reports a clean skip, so this script (and the
tests that exercise it) never depend on the unlicensed raw files actually
being present on disk.

Usage:
    python scripts/validate_sources.py docs/data/sources.yaml
    STEG_SOURCE_ROOT=. python scripts/validate_sources.py docs/data/sources.yaml
"""

import argparse
import os
import sys
from pathlib import Path

# Allow running as `python scripts/validate_sources.py` from the repo root
# without an install step: put the repo root (this file's parent's parent)
# on sys.path so `import app...` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import registry  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)

    try:
        manifest = registry.load_manifest(args.manifest)
    except Exception as exc:
        print(f"FAIL manifest schema: {exc}")
        return 1

    artifacts_by_source: dict[str, list] = {}
    for artifact in manifest.artifacts:
        artifacts_by_source.setdefault(artifact.source_id, []).append(artifact)

    source_root = os.environ.get("STEG_SOURCE_ROOT")
    exit_code = 0
    for source in manifest.sources:
        artifacts = artifacts_by_source.get(source.source_id, [])
        if not source_root:
            print(
                f"SKIP {source.source_id}: STEG_SOURCE_ROOT not set "
                f"({len(artifacts)} artifact(s) registered)"
            )
            continue
        root = Path(source_root)
        for artifact in artifacts:
            try:
                registry.verify_artifact(artifact, root)
                print(f"OK {source.source_id}:{artifact.artifact_id}")
            except Exception as exc:
                print(f"FAIL {source.source_id}:{artifact.artifact_id}: {exc}")
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
