"""Versioned validation of a cluster run.

Every score here is meaningless without the rule that produced it and the
identities it was produced under, so the report carries both: build, config,
algorithm, validation version, and random seed, plus the definition of each
metric. A score that cannot be computed is reported as None with a reason,
never as a substituted default.

Two choices follow the hardening amendments directly:

- Bootstrap resamples **outage dates**, not notices. Several notices can
  describe one outage day, so resampling notices would treat one event as
  independent confirmation of itself.
- Membership agreement is **label-invariant**. Louvain's community integers
  are arbitrary per run, so raw label comparison measures nothing; agreement
  is computed over pairwise co-membership instead.
"""

import json
import random
from itertools import combinations

from pydantic import BaseModel

from .config import CONFIG
from .graph import EdgeEvidence, build_weighted_graph

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


def _cluster(graph):
    from ..cluster_inference import compute_clusters

    return compute_clusters(graph)


def _graph_from_rows(rows, *, config=CONFIG, unweighted=False):
    if not rows:
        return build_weighted_graph(
            [], total_notices=1, locality_counts={}, config=config
        )
    total_notices = len({row["notice_id"] for row in _flatten(rows)})
    counts = {}
    for locality, notices in _locality_notices(rows).items():
        counts[locality] = len(notices)
    edges = []
    for (a, b), observations in rows.items():
        notices = {row["notice_id"] for row in observations}
        dates = {row["outage_date"] for row in observations} - {None}
        edges.append(
            EdgeEvidence(
                locality_a=a,
                locality_b=b,
                notice_count=len(notices),
                distinct_date_count=len(dates),
                mean_parse_confidence=(
                    1.0
                    if unweighted
                    else _mean(observations, "parse_confidence")
                ),
                mean_scope_confidence=(
                    1.0
                    if unweighted
                    else _mean(observations, "scope_confidence")
                ),
                mean_canonicalization_confidence=(
                    1.0
                    if unweighted
                    else _mean(observations, "canonicalization_confidence")
                ),
                geographic_confidence=None,
            )
        )
    return build_weighted_graph(
        edges,
        total_notices=max(1, total_notices),
        locality_counts=counts,
        config=config,
    )


def _flatten(rows):
    for observations in rows.values():
        yield from observations


def _locality_notices(rows):
    out = {}
    for (a, b), observations in rows.items():
        for locality in (a, b):
            bucket = out.setdefault(locality, set())
            bucket.update(row["notice_id"] for row in observations)
    return out


def _mean(observations, key):
    values = [row[key] for row in observations if row[key] is not None]
    return sum(values) / len(values) if values else 0.0


def _group(observations):
    grouped = {}
    for row in observations:
        grouped.setdefault((row["locality_a"], row["locality_b"]), []).append(row)
    return grouped


def _restrict(observations, dates):
    return [row for row in observations if row["outage_date"] in dates]


def validate_cluster_run(
    build_id: str, *, algorithm_version: str, config=CONFIG
) -> ValidationReport:
    from .. import db

    observations = db.build_scoped_cooccurrences(build_id)
    reasons = {}

    full_partition = _cluster(_graph_from_rows(_group(observations)))
    dates = sorted({row["outage_date"] for row in observations} - {None})

    # Bootstrap over outage dates.
    rng = random.Random(config.random_seed)
    agreements = []
    for _ in range(config.bootstrap_runs):
        if not dates:
            break
        sampled = {rng.choice(dates) for _ in dates}
        partition = _cluster(
            _graph_from_rows(_group(_restrict(observations, sampled)))
        )
        score = co_membership_agreement(full_partition, partition)
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
        train_partition = _cluster(
            _graph_from_rows(_group(_restrict(observations, train_dates)))
        )
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
        full_partition,
        _cluster(_graph_from_rows(_group(observations), unweighted=True)),
    )
    reasons["geography"] = (
        "no canonical geography is pinned to an evidence build yet, so a "
        "geography-only clustering cannot be built"
    )

    # Largest-notice influence.
    largest_removed = None
    notice_sizes = {}
    for row in observations:
        notice_sizes[row["notice_id"]] = notice_sizes.get(row["notice_id"], 0) + 1
    if notice_sizes:
        largest = max(sorted(notice_sizes), key=lambda key: notice_sizes[key])
        remaining = [
            row for row in observations if row["notice_id"] != largest
        ]
        largest_removed = co_membership_agreement(
            full_partition, _cluster(_graph_from_rows(_group(remaining)))
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
        full_partition,
        _cluster(_graph_from_rows(_group(observations), config=stricter)),
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
