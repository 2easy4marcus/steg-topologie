# docs/data — source manifest and raw artifacts

`sources.yaml` in this directory is the source-provenance manifest for
Evidence Model V2. It has two top-level collections:

- `sources`: logical dataset identity, ownership, and publication/license
  policy (`app.data.models.DatasetSource`).
- `artifacts`: one immutable row per file, with its relative path, checksum,
  byte size, and retrieval timestamp (`app.data.models.SourceArtifact`).

## Timestamps are registration dates, not download proof

`retrieved_at` and `registered_at` on every artifact are **local
registration timestamps** — the date this manifest entry was written. They
are not, and must not be read as, a cryptographically or otherwise proven
original download date. Nothing here attests to when or how the underlying
file first left its original publisher.

## Verifying checksums

```bash
python scripts/validate_sources.py docs/data/sources.yaml
```

Without `STEG_SOURCE_ROOT` set, the script validates the manifest's schema
only and prints a clean skip line per source (no filesystem access). Set
`STEG_SOURCE_ROOT` (typically the repo root, so relative paths like
`docs/data/delegations.geojson` resolve) to also verify that every
artifact's file exists, is the registered size, and hashes to the
registered `checksum_sha256`:

```bash
STEG_SOURCE_ROOT=. python scripts/validate_sources.py docs/data/sources.yaml
```

The script exits non-zero on a missing file, a checksum/size mismatch, a
manifest schema failure, or a `public` source declared without a
`license_id`.

## The raw files here are not committed

`docs/data/*.geojson`, `*.json`, `*.xls`, and `*.pdf` are gitignored (see
`.gitignore`) — none of their licenses have been independently verified, so
they stay local-only. Only `sources.yaml` and this README are committed.
Anyone who needs the actual data re-acquires it locally and registers it
here; the manifest and checksum verification are how a re-acquired copy is
proven to be the exact bytes the rest of the pipeline was built against.

## The 272-vs-271 delegation count disagreement

`delegations.geojson` (272 features) and `delegations.json`, its TopoJSON
sibling (271 objects), disagree on the delegation count. The importer
(`scripts/import_canonical_data.py`) treats the **GeoJSON as canonical**:
it is the only one actually parsed and staged into `administrative_areas`.
The TopoJSON is registered as a second artifact under the same
`tunisia-delegations` source for provenance, but the two are never merged,
averaged, or silently reconciled — the row-count disagreement is a known,
recorded fact about this pair of files, not a bug to paper over.

## Electoral constituencies are registration-only

`tncirconscriptions.geojson` (electoral constituencies) is registered in
the manifest with `refresh_policy: excluded_from_model`. It is not read by
any loader or the importer in this task — it exists in the manifest purely
so its provenance is on record.
