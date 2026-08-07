"""Versioned gates and weights for the evidence model.

Thresholds are configuration with a versioned rationale, not permanent
constants (design spec, "Thresholds are configuration with versioned
rationale"). Changing any value here requires bumping ``version`` so that
``quality_gate_results.config_version`` and ``publication_decisions``
identify which configuration produced a stored decision.

Rationale for the v2.0 values:

- ``subregion_scope_confidence`` 1.0 / ``notice_fallback_confidence`` 0.35 --
  a STEG table cell is an observed boundary, so co-occurrence inside one cell
  is directly attested. Whole-notice fallback pairing is an inference we make
  only when the notice has no cell structure at all, so it is admitted at low
  confidence rather than discarded.
- ``ok_parse_confidence`` 1.0 / ``warning_parse_confidence`` 0.7 -- a warning
  parse is usable but has a known defect (missing heading, unparseable date),
  so its observations are down-weighted rather than dropped. A cell with no
  heading is still a full-confidence *scope*; the missing heading is already
  penalised once, through the parse status.
- The six readiness minimums and ``max_largest_notice_share`` are carried
  over unchanged from the v1 activation thresholds.
- ``min_edge_distinct_dates`` 2 -- a pair seen on a single outage date is one
  event, not a repeated relationship, so it stays visible diagnostically but
  cannot become a clustering edge.
- ``recurrence_saturation_dates`` 3 -- the date on which repeated confirmation
  stops adding weight. Distinct from the gate above: the gate decides whether
  an edge exists at all, this only shapes how fast confidence saturates.
- ``max_geographic_bonus`` 0.15 -- ceiling on the confirmation bonus for two
  localities sharing a service unit. Geography can only scale an edge outage
  evidence already justified; it never creates one. A pair whose geography was
  never measured gets zero bonus rather than an assumed agreement.
"""

from pydantic import BaseModel


class ModelConfig(BaseModel):
    version: str = "evidence-v2.0"

    # Scope and parse confidence components.
    subregion_scope_confidence: float = 1.0
    notice_fallback_confidence: float = 0.35
    ok_parse_confidence: float = 1.0
    warning_parse_confidence: float = 0.7
    canonicalization_confidence: float = 1.0

    # Model-quality gates.
    min_valid_notices: int = 30
    min_distinct_dates: int = 15
    min_localities: int = 10
    min_repeated_pairs: int = 20
    min_active_ok_ratio: float = 0.80
    max_largest_notice_share: float = 0.20

    # Operational-health gates.
    min_recent_parse_ratio: float = 0.80
    recent_parse_window_days: int = 30
    max_scrape_age_hours: int = 48

    # Graph gates and weights.
    min_edge_distinct_dates: int = 2
    recurrence_saturation_dates: int = 3
    max_geographic_bonus: float = 0.15


CONFIG = ModelConfig()
