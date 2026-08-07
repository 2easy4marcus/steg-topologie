# Evidence model V2

What the public model actually computes, and what it deliberately refuses to
claim. Every threshold named here lives in `app/model/config.py` under
`CONFIG.version` — currently **`evidence-v2.1`** — and that version string is
stamped onto every stored quality gate result, publication decision, cluster
run, and validation run. Changing a value without bumping the version makes
stored decisions incomparable, so don't.

## The pinned build

An evidence build is immutable. At creation it pins two things and then reads
nothing else:

- its **source population** — the notices and parses in scope, copied into
  `build_notice_parses` / `build_locality_observations`, so a parser
  activation landing mid-build cannot change what the build observed;
- its **canonical geography** — `model_builds.canonical_build_id`, the
  canonical import that was active at build creation. Nullable: a build
  created before migration 0004 has no pin.

`app/evidence_pipeline.py:build_model_evidence` creates, pins, populates,
validates, records gates and a publication decision, and only then activates.
A failure at any earlier step leaves the previously active build active.

## The subregion edge rule

Two localities are paired **only when they appear in the same scope of the
same notice** — same `notice_id`, same `scope_kind`, same `scope_ordinal`
(`app/db.py:populate_scoped_observations`). A STEG table cell is an observed
boundary, so co-occurrence inside one cell is directly attested. Localities in
two different cells of one notice are not paired at all.

Two scope kinds exist:

| `scope_kind` | Meaning | `scope_confidence` |
|---|---|---|
| `subregion` | One STEG table cell, identified by its ordinal | 1.0 |
| whole-notice fallback | The notice had no cell structure to read | 0.35 |

The fallback pairs across what would have been cell boundaries, exactly as
parser version 1 did. It is admitted at low confidence rather than discarded.
**Parses written by parser version 2 have no cell ordinals**, so every one of
them is treated as a single whole-notice scope until reparsed — see the deploy
ordering in [OPERATIONS.md](OPERATIONS.md).

## Confidence components

Four components are measured and stored **side by side, never blended**, on
`build_pair_observations`:

| Component | Values | Source |
|---|---|---|
| `parse_confidence` | 1.0 if the parse status is `ok`, else 0.7 | parse status |
| `scope_confidence` | 1.0 subregion / 0.35 whole-notice fallback | scope kind |
| `canonicalization_confidence` | 1.0 | `CONFIG` |
| `geographic_confidence` | NULL / 0.0 / (0, 1] — see below | pinned canonical geography |

Per-edge means are computed **two-level** — within a notice, then across
notices (`app/db.py:build_edge_evidence`) — so one wide multi-cell notice is
weighted as one notice rather than by its cell count.

A fifth column, `temporal_confidence`, records only whether the observation
carried an outage date. The graph does not read it; the weight's temporal term
is a recurrence measure computed from the edge's distinct outage dates.

### Geographic confidence has three outcomes

Resolved only through the build's pinned canonical build, never through
"whichever canonical import is active right now":

- **NULL** — geography was never measured: either locality has no service unit
  under that pin, or the build has no pin at all. The graph applies **no
  bonus**. This is not the same as disagreement.
- **0.0** — both localities were measured and sit in **different** service
  units. Measured disagreement. Also no bonus.
- **positive** — both sit in the same service unit; the value is the weaker of
  the two spatial confidences.

Geography **never creates an edge**. It only scales one that outage evidence
already justified.

## The edge weight

From `app/model/graph.py`:

```
temporal    = min(1, distinct_date_count / recurrence_saturation_dates)   # 3
reliability = parse * scope * canonicalization * temporal
weight      = ppmi * reliability * (1 + max_geographic_bonus * geo)       # 0.15
```

`ppmi` is ordinary positive pointwise mutual information over the build's own
marginals and notice total — never the global counts, so a graph cannot mix
one build's pairs with another build's totals. Reliability is a regularizer on
a real PPMI value; the PPMI is never stored or described as a fractional
count. Every component is kept as an edge attribute so a reader can see which
one pulled a weight down.

Two ways a pair produces no edge, both counted on the surviving nodes rather
than disappearing:

- **`gated_pairs`** — fewer than `min_edge_distinct_dates` (2) distinct outage
  dates. One outage date is one event, not a repeated relationship. The
  observation stays in `build_pair_observations` for diagnostics.
- **`unweighted_pairs`** — the weight came out non-positive, because PPMI was
  0 (the pair is no more likely than chance) or a confidence component was 0.

In both cases **both localities stay in the graph as nodes**, so a locality
gated out of every pair clusters as a singleton instead of vanishing. A
locality with no pair at all is added as a node too.

A build whose pair counts violate `0 < pair_count <= marginal <= N` raises
`InconsistentBuildError` rather than being filtered. That inequality cannot be
false for an intact snapshot, so violating it means the snapshot is corrupt.

## Stable cluster identity

Louvain's community integers are arbitrary per run, so identity is
re-established every run by matching this run's memberships against the
previous active run's (`app/model/cluster_ids.py`):

- similarity is **Jaccard overlap**, compared as an integer scaled by 1e9
  (networkx documents that float weights can return a slightly suboptimal
  matching);
- a cluster inherits a predecessor id only at or above
  **`min_id_inheritance_jaccard` = 0.50** — more than half its membership;
- assignment is **maximum-weight one-to-one matching**, not greedy, so no two
  clusters claim one predecessor and no cluster is stranded because its only
  good partner was taken first;
- new ids come from a persistent monotonic allocator
  (`db.reserve_cluster_ids`), never from `MAX(cluster_id)` — retention deletes
  rows, and a recomputed maximum would reissue ids that were already used.
  Unused reservations are burned.

At the default threshold the matching and a greedy pass agree: clusters within
one run are disjoint, so two eligible edges sharing an endpoint can both clear
0.50 only by being exactly 0.50 each. Above 0.50 the matcher buys one-to-one
enforcement and deterministic tie-breaking, not a better total. Below it
(0.30, say) strict divergence returns, and the threshold is configuration.

**Lineage keeps every eligible predecessor relationship**, not just the
matched one, with a role of `inherited`, `split`, `merged`, or `related` — a
split or merge is exactly the case where the single inherited id loses
information.

## Validation

`app/model/validation.py` runs on every cluster run, and
`db.activate_completed_cluster_run` refuses to activate a run that has no
completed validation run and a stored `published` decision.

Every score travels with the identities that produced it — build, config,
algorithm, validation version, random seed, bootstrap budget — plus
`METRIC_DEFINITIONS`, the definition of each metric, in the stored report. A
score computed under a different seed or budget is not comparable to another.

- **Stability (`mean_membership_agreement`)** — bootstrap over
  `bootstrap_runs` (50) replicates at `random_seed` (0). Resampling is over
  **outage dates, not notices**: several notices can describe one outage day,
  and resampling notices would treat one event as independent confirmation of
  itself. Agreement is **label-invariant** — share of locality pairs whose
  co-membership (same cluster or not) matches — because Louvain's labels mean
  nothing across runs.
- **Temporal holdout (`held_out_edge_recall`)** — distinct outage dates split
  chronologically at `temporal_holdout_train_fraction` (0.80). Dates, not
  notices, so no outage day appears on both sides. A locality absent from
  training is **excluded from recall rather than counted as a miss** — the
  model was never given the chance to place it.
- **Baselines** — `raw_cooccurrence_baseline` clusters the same pairs with all
  confidences forced to 1.0, which is what the weighting has to beat to be
  worth anything.
- **Largest-notice influence** — agreement after removing the single
  highest-contributing notice.
- **Config sensitivity** — agreement after raising `min_edge_distinct_dates`
  by one.

### Unmeasurable scores report `None` with a reason

Not a substituted default. `unmeasured_reasons` in the stored report names why,
e.g. "fewer than two distinct outage dates, so no holdout exists".

**Known limitation, shipped:** `geography_baseline` and
`service_unit_baseline` are **always `None` today**. Clustering by canonical
geography alone is not implemented; the report records the reason under the
key `geography`. `service_unit_baseline` is currently `None` with no reason
recorded against its own key.

## The public disclaimer

Clusters and edges are **experimental statistical relationships inferred from
the co-occurrence of localities in STEG outage notices**. They do not
represent confirmed transformers, feeders, substations, or any physical grid
topology. A cluster means "these localities tend to lose power in the same
notice", nothing more. Nothing in the public model is validated against
STEG's real network, because no public description of it exists.

`/api/clusters`, `/api/model-status`, and `/api/edge-evidence` all serve this
model. The private asset-candidate pilot is a separate thing and is never
public — see [PRIVATE_TOPOLOGY.md](PRIVATE_TOPOLOGY.md).
