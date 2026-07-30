from pathlib import Path

from app.data.steg_units import load_service_units


def test_invalid_longitude_is_quarantined_and_missing_pair_is_incomplete():
    result = load_service_units(Path("tests/fixtures/data/steg_units.csv"))
    assert [q.reason_code for q in result.quarantined] == [
        "coordinate_out_of_bounds"
    ]
    incomplete = next(x for x in result.accepted if x.name == "Kerkennah")
    assert incomplete.latitude is None
    assert incomplete.longitude is None
    assert incomplete.coordinate_complete is False


def test_complete_row_keeps_coordinates():
    result = load_service_units(Path("tests/fixtures/data/steg_units.csv"))
    complete = next(x for x in result.accepted if x.name == "Kairouan")
    assert complete.coordinate_complete is True
    assert complete.latitude == 35.669447
    assert complete.longitude == 10.100963
    assert complete.governorate == "Kairouan"
