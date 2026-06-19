import pytest
from darkroom.parse import (
    ota_from_focallen,
    parse_filter,
    parse_exposure,
    parse_datetime,
    flat_morning_date,
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


def test_parse_filter_with_filter():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001"
    assert parse_filter(stem) == "L-Pro"


def test_parse_filter_normalises_lextreme():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_LExtreme_0001"
    assert parse_filter(stem) == "L-Extreme"


def test_parse_filter_no_filter():
    stem = "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001"
    assert parse_filter(stem) is None


def test_parse_exposure():
    assert parse_exposure("Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001") == "180.0s"
    assert parse_exposure("Flat_130.0ms_Bin1_585MC_gain200_20260221-093939_-20.0C_0001") == "130.0ms"


def test_parse_datetime():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001"
    dt = parse_datetime(stem)
    assert dt == datetime(2026, 2, 19, 22, 0, 0)


def test_flat_morning_date_post_midnight():
    # Session ends at 04:00 local → flats taken same morning
    end_dt = datetime(2026, 2, 20, 4, 0, 0)
    assert flat_morning_date(end_dt) == date(2026, 2, 20)


def test_flat_morning_date_evening():
    # Session ends at 22:00 → flats taken next morning
    end_dt = datetime(2026, 2, 19, 22, 0, 0)
    assert flat_morning_date(end_dt) == date(2026, 2, 20)
