"""Versioned validation of a cluster run.

Every score here is meaningless without the rule that produced it and the
identities it was produced under, so the report carries both: build, config,
algorithm, validation version, and random seed, plus the definition of each
metric. A score that cannot be computed is reported as None with a reason,
never as a substituted default -- except ``mean_membership_agreement``, whose
field is non-optional and so reports 0.0 with a reason instead. Read that one
against ``unmeasured_reasons`` before treating a zero as a measurement.

Two choices follow the hardening amendments directly:

- Bootstrap resamples **outage dates**, not notices. Several notices can
  describe one outage day, so resampling notices would treat one event as
  independent confirmation of itself.
- Membership agreement is **label-invariant**. Louvain's community integers
  are arbitrary per run, so raw label comparison measures nothing; agreement
  is computed over pairwise co-membership instead.

A third rule is what makes any of it mean something: every graph scored here
comes out of `graph.build_graph_for_build`, the same builder production runs,
restricted to the subset the metric needs. Validation owns no graph builder of
its own. It did once, and the two drifted -- a score is only about the served
model for as long as it is computed from the served model's inputs.
"""

import json
import random
from itertools import combinations

from pydantic import BaseModel

from .config import CONFIG

METRIC_DEFINITIONS = {
    "membership_agreement": (
        "Share of locality pairs whose co-membership (same cluster or not) "
        "agrees between two partitions. Label-invariant: cluster numbering "
        "is ignored. Computed only over localities present in both."
    ),
    "bootstrap_sampling_unit": "outage_date",
    "bootstrap_procedure": (
        "Resample the distinct outage dates with replacement, rebuild edge "
        "evidence restricted to the sampled dates, re-cluster, and compare "
        "membership against the full-data partition."
    ),
    "prediction_rule": (
        "Two localities are predicted to share infrastructure when the "
        "training-period clustering places them in the same cluster."
    ),
    "recall_denominator": (
        "Held-out pairs whose two localities both appear in the training "
        "period. Pairs involving an unseen locality are excluded."
    ),
    "unseen_locality_treatment": (
        "A locality absent from the training period is excluded from recall "
        "rather than counted as a miss, because the model was never given "
        "the chance to place it."
    ),
    "baselines": (
        "raw_cooccurrence: clustering the unweighted co-occurrence graph. "
        "geography / service_unit: clustering by canonical geography alone. "
        "Each is scored with the same membership agreement metric."
    ),
    "largest_notice_influence": (
        "Membership agreement between the full partition and one recomputed "
        "with the single highest-contributing notice removed."
    ),
    "config_sensitivity": (
        "Membership agreement between the full partition and one recomputed "
        "with min_edge_distinct_dates raised by one."
    ),
}


class ValidationReport(BaseModel):
    build_id: str
    config_version: str
    algorithm_version: str
    validation_version: str
    random_seed: int
    bootstrap_runs: int

    mean_membership_agreement: float
    held_out_edge_recall: float | None = None
    raw_cooccurrence_baseline: float | None = None
    geography_baseline: float | None = None
    service_unit_baseline: float | None = None
    largest_notice_removed_agreement: float | None = None
    config_sensitivity_agreement: float | None = None

    unmeasured_reasons: dict = {}
    metric_definitions: dict = {}


def co_membership_agreement(left: dict, right: dict):
    """Label-invariant partition agreement over the shared locality set.

    Returns None below two shared localities: there is no pair to agree on,
    and returning 1.0 would report perfect stability for no evidence.
    """
    shared = sorted(set(left) & set(right))
    if len(shared) < 2:
        return None
    agree = 0
    total = 0
    for a, b in combinations(shared, 2):
        total += 1
        if (left[a] == left[b]) == (right[a] == right[b]):
            agree += 1
    return agree / total


def temporal_split(dates: list):
    """Split distinct outage dates chronologically into train and holdout.

    Splitting on dates rather than notices is what keeps every notice from a
    single outage day on one side of the split.
    """
    ordered = sorted(set(dates))
    if len(ordered) < 2:
        return set(ordered), set()
    cut = min(
        len(ordered) - 1,
        max(1, int(len(ordered) * CONFIG.temporal_holdout_train_fraction)),
    )
    return set(ordered[:cut]), set(ordered[cut:])


def _cluster(build_id, **restriction):
    """Cluster one build's graph, through the production builder every time.

    Validation used to rebuild its own graph from the raw observation rows.
    That parallel builder drifted: it never applied the geographic bonus, it
    averaged confidence flat over observation rows instead of per notice, and
    it derived marginals from pair rows only. The scores therefore described a
    model that was never served. There is now one builder, and validation
    reaches the subsets it needs by restricting its inputs, not by
    reimplementing it.
    """
    from ..cluster_inference import compute_clusters
    from .graph import build_graph_for_build

    return compute_clusters(build_graph_for_build(build_id, **restriction))


def _restrict(observations, dates):
    return [row for row in observations if row["outage_date"] in dates]


def validate_cluster_run(
    build_id: str, *, algorithm_version: str, partition=None, config=CONFIG
) -> ValidationReport:
    """Score a cluster run against the partition it actually published.

    Pass `partition` -- the run's stored memberships -- so every score is
    stated about the served model rather than about a re-derivation of it.
    Omitted, the build's full graph is clustered instead; that reproduces the
    same partition today, but only as long as nothing between here and
    `run_recluster` diverges, which is the failure this parameter removes.
    """
    from .. import db

    observations = db.build_scoped_cooccurrences(build_id)
    reasons = {}

    full_partition = (
        partition if partition is not None else _cluster(build_id, config=config)
    )
    dates = sorted({row["outage_date"] for row in observations} - {None})

    # Bootstrap over outage dates.
    rng = random.Random(config.random_seed)
    agreements = []
    for _ in range(config.bootstrap_runs):
        if not dates:
            break
        sampled = {rng.choice(dates) for _ in dates}
        replicate = _cluster(build_id, dates=sampled, config=config)
        score = co_membership_agreement(full_partition, replicate)
        if score is not None:
            agreements.append(score)
    mean_agreement = (
        sum(agreements) / len(agreements) if agreements else 0.0
    )
    if not agreements:
        reasons["mean_membership_agreement"] = (
            "no bootstrap replicate shared two localities with the full run"
        )

    # Temporal holdout.
    recall = None
    train_dates, holdout_dates = temporal_split(dates)
    if len(holdout_dates) < 1 or len(train_dates) < 1:
        reasons["held_out_edge_recall"] = (
            "fewer than two distinct outage dates, so no holdout exists"
        )
    else:
        train_partition = _cluster(build_id, dates=train_dates, config=config)
        held_pairs = {
            (row["locality_a"], row["locality_b"])
            for row in _restrict(observations, holdout_dates)
        }
        scorable = [
            pair
            for pair in held_pairs
            if pair[0] in train_partition and pair[1] in train_partition
        ]
        if not scorable:
            reasons["held_out_edge_recall"] = (
                "no held-out pair had both localities present in training"
            )
        else:
            hits = sum(
                1
                for a, b in scorable
                if train_partition[a] == train_partition[b]
            )
            recall = hits / len(scorable)

    # Baselines.
    raw_baseline = co_membership_agreement(
        full_partition, _cluster(build_id, unweighted=True, config=config)
    )
    reasons["geography"] = (
        "canonical geography is pinned to this build, but clustering by "
        "geography alone is not implemented, so the geography and "
        "service-unit baselines are not scored"
        if db.build_canonical_build_id(build_id)
        else "no canonical geography is pinned to this evidence build, so a "
        "geography-only clustering cannot be built"
    )

    # Largest-notice influence.
    largest_removed = None
    notice_sizes = {}
    for row in observations:
        notice_sizes[row["notice_id"]] = notice_sizes.get(row["notice_id"], 0) + 1
    if notice_sizes:
        largest = max(sorted(notice_sizes), key=lambda key: notice_sizes[key])
        largest_removed = co_membership_agreement(
            full_partition,
            _cluster(build_id, exclude_notice_id=largest, config=config),
        )
    if largest_removed is None:
        reasons["largest_notice_removed_agreement"] = (
            "removing the largest notice left fewer than two localities"
        )

    # Config sensitivity.
    stricter = config.model_copy(
        update={
            "min_edge_distinct_dates": config.min_edge_distinct_dates + 1
        }
    )
    sensitivity = co_membership_agreement(
        full_partition, _cluster(build_id, config=stricter)
    )
    if sensitivity is None:
        reasons["config_sensitivity_agreement"] = (
            "the stricter configuration left fewer than two localities"
        )

    return ValidationReport(
        build_id=build_id,
        config_version=config.version,
        algorithm_version=algorithm_version,
        validation_version=config.validation_version,
        random_seed=config.random_seed,
        bootstrap_runs=config.bootstrap_runs,
        mean_membership_agreement=mean_agreement,
        held_out_edge_recall=recall,
        raw_cooccurrence_baseline=raw_baseline,
        geography_baseline=None,
        service_unit_baseline=None,
        largest_notice_removed_agreement=largest_removed,
        config_sensitivity_agreement=sensitivity,
        unmeasured_reasons=reasons,
        metric_definitions=METRIC_DEFINITIONS,
    )


def report_json(report: ValidationReport) -> str:
    return json.dumps(report.model_dump(), sort_keys=True, ensure_ascii=False)
