from app import db, rebuild_evidence


def test_rebuild_defaults_to_dry_run(capsys):
    db.create_model_build("build-old", "2026-07-26T10:00:00Z")
    db.complete_model_build("build-old", "2026-07-26T10:01:00Z", 0, 0, 0)
    db.activate_completed_model_build("build-old")

    assert rebuild_evidence.main([]) == 0

    assert db.active_build_id() == "build-old"
    assert "dry-run" in capsys.readouterr().out.lower()


def test_rebuild_apply_activates_new_idempotent_build():
    assert rebuild_evidence.main(["--apply"]) == 0
    first = db.active_build_id()
    assert first

    assert rebuild_evidence.main(["--apply"]) == 0
    second = db.active_build_id()
    assert db.build_locality_counts(first) == db.build_locality_counts(second)
    assert db.build_cooccurrences(first) == db.build_cooccurrences(second)

