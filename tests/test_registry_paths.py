"""verify_artifact's path-escape guard (app/data/registry.py).

verify_artifact resolves an artifact's relative_path beneath an explicit root
and must reject an absolute path, a `..` traversal, and a symlink that escapes
the root -- all three in the single Path.resolve() comparison -- while
accepting a real file that lives under the root with a matching size + hash.
These exercise the real filesystem (a real symlink pointing outside root), not
a mock of the thing under test.
"""

import pytest

from app.data import registry
from app.data.models import SourceArtifact


def _artifact(relative_path, *, byte_size=0, checksum="a" * 64):
    return SourceArtifact(
        artifact_id="art-1",
        source_id="src-1",
        relative_path=relative_path,
        checksum_sha256=checksum,
        byte_size=byte_size,
        retrieved_at="2026-07-30T00:00:00Z",
        registered_at="2026-07-30T00:00:00Z",
        media_type="text/plain",
        schema_version="1",
    )


def test_absolute_path_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="absolute relative_path"):
        registry.verify_artifact(_artifact("/etc/passwd"), tmp_path)


def test_dotdot_traversal_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    # A real file outside the root, addressed via `..`; the escape must be
    # caught before the file is ever read.
    (tmp_path / "outside.txt").write_text("secret")
    with pytest.raises(ValueError, match="escapes source root"):
        registry.verify_artifact(_artifact("../outside.txt"), root)


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = root / "link.txt"
    link.symlink_to(outside)  # real symlink escaping the root
    with pytest.raises(ValueError, match="escapes source root"):
        registry.verify_artifact(_artifact("link.txt"), root)


def test_valid_file_under_root_is_accepted(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "data.txt"
    target.write_bytes(b"hello world")
    artifact = _artifact(
        "data.txt",
        byte_size=target.stat().st_size,
        checksum=registry.sha256_file(target),
    )
    # Returns None on success; a bad size/hash below proves the happy path
    # is not vacuously passing.
    assert registry.verify_artifact(artifact, root) is None
    with pytest.raises(ValueError, match="size_mismatch"):
        registry.verify_artifact(_artifact("data.txt", byte_size=999), root)
