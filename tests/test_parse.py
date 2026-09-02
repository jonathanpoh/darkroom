import pytest
from darkroom.parse import (
    calibration_filter,
    ota_from_focallen,
    parse_filter,
    parse_exposure,
    parse_temperature,
    parse_datetime,
    parse_panel,
    flat_morning_date,
    reclassify_flat_dark,
    FLAT_DARK_THRESHOLD_SEC,
    KNOWN_OTAS,
)
from datetime import datetime, date


def test_ota_exact():
    assert ota_from_focallen(400) == "FRA400"
    assert ota_from_focallen(180) == "FMA180"


def test_ota_tolerance():
    # ASIAir reports measured focal length, not nominal
    assert ota_from_focallen(402) == "FRA400"
    assert ota_from_focallen(185) == "FMA180"
    assert ota_from_focallen(170) == "FMA180"
    assert ota_from_focallen(190) == "FMA180"
    assert ota_from_focallen(390) == "FRA400"
    assert ota_from_focallen(410) == "FRA400"


def test_ota_reducer():
    assert ota_from_focallen(280) == "FRA400-07x"
    assert ota_from_focallen(270) == "FRA400-07x"
    assert ota_from_focallen(290) == "FRA400-07x"


def test_ota_unknown():
    assert ota_from_focallen(250) == "Unknown"
    assert ota_from_focallen(None) == "Unknown"


def test_ota_canon50mm():
    # A 50mm Canon lens (mosaic panels) has its own tolerance window.
    assert ota_from_focallen(50) == "Canon50mm"
    assert ota_from_focallen(45) == "Canon50mm"
    assert ota_from_focallen(55) == "Canon50mm"


def test_known_otas_includes_canon50mm():
    # KNOWN_OTAS is the ingest_review correction pick-list; must stay in step
    # with parse_ota's tolerance windows or Canon50mm sessions are unfixable.
    assert "Canon50mm" in KNOWN_OTAS


def test_parse_filter_with_filter():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001"
    assert parse_filter(stem) == "L-Pro"


def test_parse_filter_normalises_lextreme():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_LExtreme_0001"
    assert parse_filter(stem) == "L-Extreme"


def test_calibration_filter_filename_then_header_then_none():
    stem = "Flat_1.0s_Bin1_L-Pro_0001"
    assert calibration_filter(stem, "Flat", "L-Extreme") == "L-Pro"
    assert calibration_filter("Flat_1.0s_Bin1_585MC_gain100_20260220-071000_-20.0C_0001", "FlatDark", "L-Pro") == "L-Pro"
    assert calibration_filter("Flat_1.0s_Bin1_585MC_gain100_20260220-071000_-20.0C_0001", "Flat", "") is None
    # Darks and bias frames never carry a filter, whatever the header says.
    assert calibration_filter(stem, "Dark", "L-Pro") is None


def test_parse_filter_no_filter():
    stem = "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001"
    assert parse_filter(stem) is None


def test_parse_filter_f_suffix_no_filter():
    # Files ending in _<seq>_f.fit: parts[-2] is sequence number, not filter
    stem = "Light_M 31_180.0s_Bin1_585MC_gain200_20250915-010333_-10.0C_0001_f"
    assert parse_filter(stem) is None


def test_parse_filter_sequence_number_returns_none():
    # Sequence number at parts[-2] should not be treated as filter
    stem = "Light_IC4604_60.0s_20230715-235142_0001"
    assert parse_filter(stem) is None


def test_parse_filter_old_datetime_returns_none():
    # Old ASIAir: datetime at parts[-2] should not be treated as filter
    stem = "Light_IC4604_60.0s_2023-07-15T23-57-14_0001"
    assert parse_filter(stem) is None


def test_parse_filter_exposure_returns_none():
    stem = "Light_M42_20.00s_0001"
    assert parse_filter(stem) is None


def test_parse_exposure():
    assert parse_exposure("Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001") == "180.0s"
    assert parse_exposure("Flat_130.0ms_Bin1_585MC_gain200_20260221-093939_-20.0C_0001") == "130.0ms"


def test_parse_datetime():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001"
    dt = parse_datetime(stem)
    assert dt == datetime(2026, 2, 19, 22, 0, 0)


def test_parse_temperature_negative():
    stem = "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_-20.0C_0001"
    assert parse_temperature(stem) == -20.0


def test_parse_temperature_positive():
    stem = "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_15.0C_0001"
    assert parse_temperature(stem) == 15.0


def test_parse_temperature_no_match():
    # "585MC" and "180.0s" must not false-match the temperature pattern
    stem = "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_0001"
    assert parse_temperature(stem) is None


def test_flat_morning_date_post_midnight():
    # Session ends at 04:00 local → flats taken same morning
    end_dt = datetime(2026, 2, 20, 4, 0, 0)
    assert flat_morning_date(end_dt) == date(2026, 2, 20)


def test_flat_morning_date_evening():
    # Session ends at 22:00 → flats taken next morning
    end_dt = datetime(2026, 2, 19, 22, 0, 0)
    assert flat_morning_date(end_dt) == date(2026, 2, 20)


# ── R1: single source of truth for the Dark/FlatDark boundary, shared by
# cataloger.CalibrationCataloger.scan, scanner._scan_calibration, and
# triage/suggest.suggest_calibration_dest. Pin the exact boundary here so a
# future change to the constant or the comparison can't silently drift one
# call path away from the others (each path also has its own boundary test
# exercising its full code path — see test_cataloger.py, test_scanner.py,
# and tests/triage/test_suggest.py).

def test_flat_dark_threshold_is_ten_seconds():
    assert FLAT_DARK_THRESHOLD_SEC == 10.0


def test_reclassify_just_under_threshold_becomes_flatdark():
    assert reclassify_flat_dark("Dark", 9.99) == "FlatDark"


def test_reclassify_exactly_at_threshold_stays_dark():
    # Comparison is strict "<", so a dark timed exactly at the threshold is
    # still a science dark, not a flat dark.
    assert reclassify_flat_dark("Dark", FLAT_DARK_THRESHOLD_SEC) == "Dark"


def test_reclassify_just_over_threshold_stays_dark():
    assert reclassify_flat_dark("Dark", 10.01) == "Dark"


def test_reclassify_non_dark_frame_types_untouched():
    assert reclassify_flat_dark("Flat", 1.5) == "Flat"
    assert reclassify_flat_dark("Bias", 0.0) == "Bias"
    assert reclassify_flat_dark("FlatDark", 1.5) == "FlatDark"


def test_reclassify_none_exposure_is_safe():
    assert reclassify_flat_dark("Dark", None) == "Dark"


# ── M1: parse_panel splits a trailing mosaic panel label off an ASIAir
# object/folder name (e.g. "IC4604_1-1"), not a full filename stem.

@pytest.mark.parametrize(
    "name, expected",
    [
        ("IC4604_1-1", ("IC4604", "1-1")),
        ("IC4604_2-2", ("IC4604", "2-2")),
        ("M8_1-8", ("M8", "1-8")),
        ("IC 4604 1-1", ("IC 4604", "1-1")),
        ("IC4604", ("IC4604", None)),
        ("M 8", ("M 8", None)),
        ("SH2-101", ("SH2-101", None)),  # must not match; no separator
        ("SH2-101_1-2", ("SH2-101", "1-2")),
        ("LDN 1235", ("LDN 1235", None)),
        ("NGC 7000", ("NGC 7000", None)),
        ("B33", ("B33", None)),
        ("", ("", None)),
        ("1-1", ("1-1", None)),  # no base to split from
        ("IC4604_1-1_extra", ("IC4604_1-1_extra", None)),  # label must be trailing
    ],
)
def test_parse_panel(name, expected):
    assert parse_panel(name) == expected


def test_parse_panel_bounded_digit_runs():
    # Digit runs are bounded to 1-2 digits each so a catalogue designation
    # like "NGC 7000-7001" can't be mistaken for a panel label.
    assert parse_panel("NGC 7000-7001") == ("NGC 7000-7001", None)
