# tests/test_locality_dedup.py
from app import db, locality_dedup


def test_normalize_strips_diacritics_and_unifies_alef():
    assert locality_dedup.normalize_locality("  دقّة  ") == locality_dedup.normalize_locality("دقة") == "دقه"
    assert locality_dedup.normalize_locality("أريانة") == locality_dedup.normalize_locality("اريانة") == "اريانه"


def test_resolve_locality_creates_new_when_no_match():
    canonical = locality_dedup.resolve_locality("Dekka")
    assert canonical == "Dekka"
    assert db.get_locality("Dekka") is not None


def test_resolve_locality_exact_normalized_match_creates_alias():
    locality_dedup.resolve_locality("دقة")
    canonical = locality_dedup.resolve_locality("دقـة")  # extra tatweel/spacing variant, same normalized form after our rules
    assert canonical == "دقة"
    assert db.resolve_alias("دقـة") == "دقة"


def test_resolve_locality_fuzzy_match_above_threshold():
    locality_dedup.resolve_locality("Bou Argoub")
    canonical = locality_dedup.resolve_locality("Bou Argob")  # missing one letter, still >=90% token_sort_ratio
    assert canonical == "Bou Argoub"


def test_resolve_locality_below_threshold_creates_distinct():
    locality_dedup.resolve_locality("Tozeur")
    canonical = locality_dedup.resolve_locality("Kebili")
    assert canonical == "Kebili"
    assert canonical != "Tozeur"
