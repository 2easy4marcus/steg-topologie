from app import db, evidence_pipeline


def _scoped(item):
    """Accept "A", ("A", subregion), or ("A", subregion, scope_ordinal)."""
    if not isinstance(item, tuple):
        return item, None, None
    if len(item) == 2:
        return item[0], item[1], None
    return item


def _active_notice(
    notice_id: str,
    localities: list,
    notice_date: str | None,
    parse_status: str = "ok",
):
    snapshot_id = f"snapshot-{notice_id}"
    parse_id = f"parse-{notice_id}"
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notice_snapshots(
                snapshot_id, notice_id, source_url, content_hash,
                raw_html, first_fetched_at
            ) VALUES (?, ?, ?, ?, '<html/>', '2026-07-26T10:00:00Z')
            """,
            [
                snapshot_id,
                notice_id,
                f"https://example.test/{notice_id}",
                f"hash-{notice_id}",
            ],
        )
        conn.execute(
            """
            INSERT INTO notice_parses(
                parse_id, snapshot_id, notice_id, title, notice_date_iso,
                parser_version, normalization_version, parse_status,
                parse_warnings, parsed_at
            ) VALUES (?, ?, ?, ?, ?, '3', '1', ?, '[]',
                      '2026-07-26T10:01:00Z')
            """,
            [
                parse_id,
                snapshot_id,
                notice_id,
                notice_id,
                notice_date,
                parse_status,
            ],
        )
        parsed = [_scoped(item) for item in localities]
        subregions = [subregion for _, subregion, _ in parsed]
        inferred = evidence_pipeline.infer_scope_ordinals(subregions)
        for ordinal, (item, fallback) in enumerate(zip(parsed, inferred)):
            locality, subregion, scope_ordinal = item
            conn.execute(
                """
                INSERT INTO notice_localities(
                    parse_id, ordinal, raw_name, canonical_name,
                    subregion_name, scope_ordinal
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    parse_id,
                    ordinal,
                    locality,
                    locality,
                    subregion,
                    fallback if scope_ordinal is None else scope_ordinal,
                ],
            )
        conn.execute(
            """
            INSERT INTO notice_state(
                notice_id, latest_snapshot_id, active_parse_id, updated_at
            ) VALUES (?, ?, ?, '2026-07-26T10:02:00Z')
            """,
            [notice_id, snapshot_id, parse_id],
        )


def test_build_counts_each_notice_locality_and_pair_once():
    _active_notice("n1", ["A", "A", "B"], "2026-07-20")
    _active_notice("n2", ["A", "B", "C"], "2026-07-22")

    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-26T10:00:00Z"
    )

    assert db.active_build_id() == build_id
    assert db.build_locality_counts(build_id) == {"A": 2, "B": 2, "C": 1}
    pairs = {
        (row["locality_a"], row["locality_b"]): row
        for row in db.build_cooccurrences(build_id)
    }
    assert pairs[("A", "B")]["notice_count"] == 2
    assert pairs[("A", "B")]["distinct_date_count"] == 2
    assert pairs[("A", "B")]["first_observed_on"] == "2026-07-20"
    assert pairs[("A", "B")]["last_observed_on"] == "2026-07-22"
    assert ("A", "A") not in pairs


def test_missing_outage_dates_remain_null():
    _active_notice("n1", ["A", "B"], None)

    build_id = evidence_pipeline.build_model_evidence(
        created_at="2026-07-26T10:00:00Z"
    )
    pair = db.build_cooccurrences(build_id)[0]

    assert pair["distinct_date_count"] == 0
    assert pair["first_observed_on"] is None
    assert pair["last_observed_on"] is None


def test_failed_activation_preserves_previous_build(monkeypatch):
    _active_notice("n1", ["A", "B"], "2026-07-20")
    previous = evidence_pipeline.build_model_evidence(
        created_at="2026-07-26T10:00:00Z"
    )

    def fail_activation(build_id):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(db, "activate_completed_model_build", fail_activation)

    try:
        evidence_pipeline.build_model_evidence(
            created_at="2026-07-26T11:00:00Z"
        )
    except RuntimeError:
        pass

    assert db.active_build_id() == previous

