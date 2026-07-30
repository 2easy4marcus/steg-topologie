"""Load and validate the source manifest (docs/data/sources.yaml).

The manifest has two top-level collections, mirroring the two Pydantic
contracts in ``app.data.models``:

- ``sources``: logical ``DatasetSource`` records (identity, ownership,
  publication/license policy, refresh policy).
- ``artifacts``: immutable ``SourceArtifact`` records (path, checksum, size,
  retrieval timestamp), each pointing back at a ``source_id``.

See ``.superpowers/sdd/2026-07-30-evidence-model-v2/task-3-brief.md``'s
"Task 3 authoritative correction" for why this is two collections rather
than the single flat list the file's older illustrative examples show.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import DatasetSource, SourceArtifact


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Manifest:
    sources: list[DatasetSource]
    artifacts: list[SourceArtifact]


def load_manifest(path: Path) -> Manifest:
    """Parse and validate `path` against the two registry contracts.

    Raises pydantic.ValidationError (schema failure, including a public
    source missing license_id -- that rule lives on DatasetSource itself)
    or ValueError (an artifact referencing an unknown source_id).
    """
    payload = yaml.safe_load(path.read_text()) or {}
    sources = [
        DatasetSource.model_validate(row) for row in payload.get("sources", [])
    ]
    artifacts = [
        SourceArtifact.model_validate(row)
        for row in payload.get("artifacts", [])
    ]
    known_source_ids = {source.source_id for source in sources}
    unknown = {a.source_id for a in artifacts} - known_source_ids
    if unknown:
        raise ValueError(
            f"artifacts reference unknown source_id(s): {sorted(unknown)}"
        )
    return Manifest(sources=sources, artifacts=artifacts)


def verify_artifact(artifact: SourceArtifact, root: Path) -> None:
    """Verify one artifact's file exists under `root` with a matching hash.

    `root` is the explicit source root every relative_path must resolve
    beneath. Path.resolve() follows symlinks, so comparing the resolved
    artifact path against the resolved root rejects an absolute path, a
    `..` traversal, and a symlink escape in one check.
    """
    relative = Path(artifact.relative_path)
    if relative.is_absolute():
        raise ValueError(f"absolute relative_path: {artifact.relative_path}")
    resolved_root = root.resolve()
    resolved_path = (root / relative).resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError(
            f"relative_path escapes source root: {artifact.relative_path}"
        )
    if not resolved_path.is_file():
        raise FileNotFoundError(
            f"missing_file:{artifact.artifact_id}:{resolved_path}"
        )
    actual_size = resolved_path.stat().st_size
    if actual_size != artifact.byte_size:
        raise ValueError(
            f"size_mismatch:{artifact.artifact_id}:"
            f"expected={artifact.byte_size}:actual={actual_size}"
        )
    actual_checksum = sha256_file(resolved_path)
    if actual_checksum != artifact.checksum_sha256:
        raise ValueError(f"checksum_mismatch:{artifact.artifact_id}")
