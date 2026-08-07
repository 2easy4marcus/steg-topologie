"""Derive the bounded Sfax/Kerkennah power-topology extract from an OSM .pbf.

Private and experimental. The .pbf itself is a registered source artifact that
is not in the repository; this script refuses to read one whose checksum is
not registered in the manifest, because an unregistered file has no
provenance and a snapshot derived from it could never be reproduced.

Every input is explicit -- there is no "current" .pbf, no default snapshot id,
and no default output path -- and only the bounded Sfax/Kerkennah extract is
ever written.

Usage:
    python scripts/extract_sfax_topology.py \\
        --pbf docs/data/tunisia-latest.osm.pbf \\
        --snapshot-id sfax-2026-07-30 \\
        --output docs/data/derived/sfax-topology-2026-07-30.json
"""

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/extract_sfax_topology.py` from the repo
# root without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import registry  # noqa: E402
from app.topology import osm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "data" / "sources.yaml"


def registered_artifact_id(pbf: Path, manifest: Path) -> str:
    """The manifest artifact whose checksum matches `pbf`, or a hard error."""
    checksum = registry.sha256_file(pbf)
    for artifact in registry.load_manifest(manifest).artifacts:
        if artifact.checksum_sha256 == checksum:
            return artifact.artifact_id
    raise SystemExit(
        f"unregistered_source:{pbf}:sha256={checksum}\n"
        f"Register this artifact in {manifest} before deriving a snapshot."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)

    artifact_id = registered_artifact_id(args.pbf, args.manifest)
    snapshot = osm.load_topology(
        args.pbf,
        snapshot_id=args.snapshot_id,
        bbox=osm.SFAX_KERKENNAH_BBOX,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(snapshot.model_dump_json(indent=2), "utf-8")

    quarantined: dict = {}
    for row in snapshot.quarantined_relations:
        quarantined[row.reason_code] = quarantined.get(row.reason_code, 0) + 1
    print(
        f"snapshot={args.snapshot_id} artifact={artifact_id} "
        f"assets={len(snapshot.assets)} edges={len(snapshot.edges)} "
        f"nodes={len(snapshot.nodes)} relations={len(snapshot.relations)} "
        f"quarantined={quarantined or '{}'} -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
