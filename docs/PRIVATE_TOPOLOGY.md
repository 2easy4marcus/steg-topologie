# Private topology and the Sfax asset-candidate pilot

Private and experimental. Nothing described here is public, nothing here is
deployed, and nothing here runs in CI or in the daily GitHub Action.

## Attribution

The topology input is an OpenStreetMap extract.

> © OpenStreetMap contributors, available under the
> [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

Any derived snapshot inherits ODbL. That is one of the reasons derived
snapshots stay local: publishing one is a distribution decision with licence
consequences, not just a privacy one.

## Local-only PBF placement

| | |
|---|---|
| Path | `docs/tunisia-260725.osm.pbf` |
| Size | 83,880,659 bytes (~84 MB) |
| SHA-256 | `85a5ddd1faa5e093ae34337b1c1699f7d4713aaaf25b4288c04f2fd23a4007af` |
| Manifest artifact | `osm-tunisia-20260725-pbf` (source `osm-tunisia-20260725`) |
| Licence | ODbL-1.0 |
| Git | ignored by `docs/tunisia-*.osm.pbf`, **never committed** |

The file is not in the repository and is not fetched by anything. An operator
who does not have it cannot run the extraction, and that is intended — the
topology logic is tested against a small JSON fixture instead, so nothing in
the suite depends on the PBF being present.

## Checksum registration

`scripts/extract_sfax_topology.py` **refuses to read a PBF whose SHA-256 is
not registered** in `docs/data/sources.yaml`. An unregistered file has no
provenance and a snapshot derived from it could never be reproduced. To
register a new extract, add an `artifacts:` entry with its real checksum and
byte size, then verify:

```bash
python scripts/validate_sources.py docs/data/sources.yaml
STEG_SOURCE_ROOT=. python scripts/validate_sources.py docs/data/sources.yaml
```

Without `STEG_SOURCE_ROOT` the script validates the manifest schema only and
reports a clean `SKIP` per source; with it, every artifact's checksum is
verified against the file on disk.

## Extraction

```bash
python scripts/extract_sfax_topology.py \
  --pbf docs/tunisia-260725.osm.pbf \
  --snapshot-id sfax-2026-07-30 \
  --output docs/data/derived/sfax-topology-2026-07-30.json
```

Every input is explicit: no "current" PBF, no default snapshot id, no default
output path. The bounding box is fixed in code
(`osm.SFAX_KERKENNAH_BBOX`, `34.20, 9.95 → 35.25, 11.40` — Sfax governorate
plus the Kerkennah islands) and nothing outside it is ever written.

`docs/data/derived/` is git-ignored. **Write derived snapshots there.** If you
write one somewhere else, ignore it there too before your next `git add`.

### Reading the quarantine counts

The command prints a per-reason tally of relations it refused to interpret.
Three reason codes exist, and one of them is loud by design:

| Reason code | Meaning | Expected volume |
|---|---|---|
| `no_resolvable_members` | None of the relation's members are in the extract | **Hundreds.** Almost always a relation that is simply outside the Sfax box, which a Tunisia-wide PBF has a lot of. Not breakage. |
| `unsupported_relation_type` | Power relation that is not `site`/`multipolygon` | Single digits |
| `nested_relation_member` | A member is itself a relation | Single digits |

`no_resolvable_members` is deliberately not silent even though it is mostly
benign: it is also what a broken member-type mapping looks like, and a silent
skip is indistinguishable from "there was nothing there". Watch the two
structural codes; treat a *sudden* change in the first as a signal, not its
absolute size.

## Running the pilot

`app/model/candidates.py:run_candidate_pilot` is a **library function with no
CLI and no HTTP route**, called from a Python shell against a build and
cluster run that already exist. It must never get a route: see the comment at
the top of `.github/workflows/scrape.yml`.

`source_registered=True` is the operator's own statement that the snapshot
came from a checksum-verified artifact. It has no default, on purpose.

## The privacy boundary

- No public endpoint reads `asset_candidate_runs` or
  `asset_candidate_scores`. `tests/test_openapi_boundaries.py` enforces this
  against the published contract, the generated artifacts, and live public
  response bodies.
- Private asset IDs never appear in public output.
- The published OpenAPI contract describes exactly three paths and forbids the
  strings `asset_id` and `candidate` anywhere in it or in the generated
  Postman collections.

## The score is a ranking index, not a probability

`score_kind` is the literal `"ranking_index"`, and the stored version is
`SCORING_VERSION = "sfax-candidate-ranking-v1"`. The components are
hand-weighted and **none of them is calibrated against a known outcome** —
there is no ground truth about which asset failed. A score of 0.8 does not
mean 80% anything. It means this asset sorted above one scoring 0.7 under
these weights.

Ordering is enforced: geography bounds the candidate set first, then topology
and service features score what survives. An asset no cluster locality is near
is not a low-scoring candidate; it is not a candidate.

Six features, all in [0, 1], all measured. **Every share below is measured
against the *accepted* localities** — the cluster members that have a measured
geography (`spatial_confidence > 0`) — not against all cluster members. A
cluster member whose geography was never measured is not in any denominator:

| Feature | Weight | Definition |
|---|---|---|
| `outage_fit` | 0.35 | Share of the accepted localities within `radius_km` of the asset |
| `topology_consistency` | 0.25 | Share of the accepted localities reached by the asset's whole connected component. Zero when the component contains no line — an isolated point has no topology |
| `service_prior` | 0.15 | Purity of the STEG service units of the covered localities. Zero when none has a measured unit |
| `distance_score` | 0.10 | `1 − mean distance / radius` over covered localities |
| `temporal_support` | 0.10 | Cluster's independent outage dates, saturating at `CONFIG.recurrence_saturation_dates`. Cluster-level, so identical for every candidate in a run |
| `completeness` | 0.05 | Share of three inputs actually present: a voltage tag, a line in the component, a service unit on every covered locality. Below 0.5 the candidate is dropped |

**Two gates drop candidates before ranking, not one.** Besides the
completeness floor in the table, an asset with no topology *and* no service
signal (`topology_consistency <= 0 and service_prior <= 0`) is dropped
outright — the amendment requires at least one topology or service signal, and
an asset with neither is a coordinate, not evidence.

Every run stores its `build_id`, `cluster_run_id`, `source_snapshot_id`,
`config_version`, `scoring_version`, and the `radius_km` it actually scored
with. Each score row stores its components and a `min_rank`/`max_rank`
sensitivity band over a fixed, fully enumerated set of weight perturbations
(each weight scaled by 0.8 and 1.2 in turn, renormalized).

## Known limitations

These are real and an operator should not have to discover them alone.

1. **The feature definitions are the implementer's, not the plan's.** The plan
   named the six features and their weights and defined none of them. The
   definitions in the table above and `CANDIDATE_RADIUS_KM = 8.0` were
   invented to be deterministic and documented, not derived from data. The
   whole point of the pilot is to recalibrate them, which is why `radius_km`
   is stored per run rather than only versioned.
2. **`outage_fit` and `topology_consistency` are correlated by construction.**
   They share `radius_km`: coverage feeds both. Their combined 0.60 weight is
   therefore not 0.60 of independent signal.
3. **The weights are not in `CONFIG`, deliberately.** `WEIGHTS`,
   `CANDIDATE_RADIUS_KM`, and `MIN_CANDIDATE_COMPLETENESS` live in
   `candidates.py` under `SCORING_VERSION`. Bumping `evidence-v2.1` because a
   private pilot retuned a weight would invalidate the comparability of
   unrelated stored *public* decisions. Retune the pilot freely; bump
   `SCORING_VERSION` when you do.
4. **`rank_candidates` does not enforce the completeness gate.** Only
   `generate_candidates` does. A caller that builds features by hand and calls
   `rank_candidates` directly bypasses it.
5. **A nonexistent `cluster_id` records a normal-looking
   `insufficient_evidence` run** rather than using the `failed` status and
   `public_error_code` column that exist for exactly that case.

## Deletion and rebuild

Deleting all of it:

```bash
rm -f docs/tunisia-260725.osm.pbf
rm -rf docs/data/derived/
```

```sql
DELETE FROM asset_candidate_scores;
DELETE FROM asset_candidate_runs;
```

`PRAGMA foreign_keys` is 0 on this deployment and the guards are `BEFORE
INSERT` triggers, so nothing enforces the delete order — but delete the scores
first anyway, so a partial failure leaves orphaned parents rather than
orphaned scores. Nothing public depends on either table, so this is safe at
any time. Leave the `sources.yaml` entry in place — it is the provenance record for a
file you may re-acquire, and removing it only makes the next extraction refuse
to run.

Rebuilding:

1. Re-acquire a Tunisia PBF.
2. Compare its SHA-256 against `sources.yaml`. If it differs (it will, for a
   newer date), register it as a **new** artifact with a new `source_id` and
   date rather than editing the old checksum — the old snapshot's provenance
   must stay readable.
3. Re-run the extraction above with a fresh `--snapshot-id`.
4. Re-run `run_candidate_pilot` against a current build and cluster run. The
   new `source_snapshot_id` on the run row is what distinguishes the results
   from the old ones.
