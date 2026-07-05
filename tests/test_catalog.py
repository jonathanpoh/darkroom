import sqlite3
import subprocess
import sys
import pytest
from pathlib import Path
from darkroom.catalog import (
    query_all_sessions,
    find_darks,
    find_flats,
    find_flat_darks,
)
from darkroom.catalog_client import LocalBackend


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            target TEXT,
            obs_date TEXT,
            ota TEXT,
            camera TEXT,
            filter TEXT,
            gain INTEGER,
            exposure_sec REAL,
            frame_count INTEGER,
            total_integration_sec REAL,
            lights_path TEXT,
            temperature_c REAL,
            ra_deg REAL,
            dec_deg REAL
        );
        CREATE TABLE calibration_sets (
            set_id TEXT PRIMARY KEY,
            frame_type TEXT,
            camera TEXT,
            ota TEXT,
            filter TEXT,
            gain INTEGER,
            exposure_sec REAL,
            temperature_c REAL,
            capture_date TEXT,
            folder_path TEXT,
            frame_count INTEGER,
            is_master INTEGER DEFAULT 0
        );
    """)
    conn.execute("""
        INSERT INTO sessions VALUES
        ('M81_20260219_FRA400_ZWOASI585MCPro_L-Pro', 'M 81', '2026-02-19',
         'FRA400', 'ZWO ASI585MC Pro', 'L-Pro', 200, 180.0, 132, 23760.0,
         '04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights',
         -20.0, 148.89, 69.07)
    """)
    conn.execute("""
        INSERT INTO sessions VALUES
        ('M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme', 'M 81', '2026-02-20',
         'FRA400', 'ZWO ASI585MC Pro', 'L-Extreme', 200, 180.0, 60, 10800.0,
         '04_Deep Sky Objects/M 81/2026-02-20_FRA400_ZWOASI585MCPro_L-Extreme/Lights',
         -20.0, 148.89, 69.07)
    """)
    conn.execute("""
        INSERT INTO calibration_sets VALUES
        ('Dark_ZWOASI585MCPro_180.0s_200g', 'Dark', 'ZWO ASI585MC Pro', NULL, NULL,
         200, 180.0, -20.0, '2026-02-01',
         '00_Calibration/Darks/ZWOASI585MCPro', 30, 0)
    """)
    conn.execute("""
        INSERT INTO calibration_sets VALUES
        ('Flat_FRA400_ZWOASI585MCPro_L-Pro_20260219', 'Flat', 'ZWO ASI585MC Pro',
         'FRA400', 'L-Pro', 200, 1.35, -20.0, '2026-02-19',
         '00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-19', 20, 0)
    """)
    conn.execute("""
        INSERT INTO calibration_sets VALUES
        ('Flat_FRA400_ZWOASI585MCPro_L-Pro_20260220', 'Flat', 'ZWO ASI585MC Pro',
         'FRA400', 'L-Pro', 200, 1.35, -20.0, '2026-02-20',
         '00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20', 20, 0)
    """)
    conn.execute("""
        INSERT INTO calibration_sets VALUES
        ('FlatDark_ZWOASI585MCPro_1.35s_20260220', 'FlatDark', 'ZWO ASI585MC Pro',
         NULL, NULL, 200, 1.35, -20.0, '2026-02-20',
         '00_Calibration/FlatDarks/ZWOASI585MCPro', 20, 0)
    """)
    conn.commit()
    conn.close()
    return db


def test_query_all_sessions(tmp_path):
    db = make_db(tmp_path)
    rows = query_all_sessions(LocalBackend(db))
    assert len(rows) == 2
    assert rows[0]["session_id"] == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    assert rows[0]["target"] == "M 81"
    assert rows[0]["obs_date"] == "2026-02-19"


def test_query_sessions_by_target(tmp_path):
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(target="M 81")
    assert len(rows) == 2


def test_query_sessions_by_target_and_date(tmp_path):
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(target="M 81", obs_date="2026-02-19")
    assert len(rows) == 1
    assert rows[0]["filter"] == "L-Pro"


def test_query_sessions_by_session_id(tmp_path):
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(session_id="M81_20260219_FRA400_ZWOASI585MCPro_L-Pro")
    assert len(rows) == 1
    assert rows[0]["ota"] == "FRA400"


def test_query_sessions_no_match(tmp_path):
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(target="NGC 1234")
    assert rows == []


def test_query_sessions_target_missing_space(tmp_path):
    # 'M81' (no space) should still match the stored 'M 81'
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(target="M81")
    assert len(rows) == 2


def test_query_sessions_target_wrong_case(tmp_path):
    # 'm 81' (lowercase prefix) should match 'M 81'
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(target="m 81")
    assert len(rows) == 2


def test_query_sessions_target_messy(tmp_path):
    # both wrong: no space and lowercase
    db = make_db(tmp_path)
    rows = LocalBackend(db).query_sessions(target="m81")
    assert len(rows) == 2


def test_query_sessions_sharpless_normalised(tmp_path):
    db = make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sessions (session_id, target, obs_date) VALUES "
        "('Sh2-103_20260219_FRA400_ZWOASI585MCPro_L-Extreme', 'Sh2-103', '2026-02-19')"
    )
    conn.commit()
    conn.close()
    # any of these user spellings should resolve to the stored 'Sh2-103'
    backend = LocalBackend(db)
    for spelling in ("SH2-103", "Sh 2-103", "sh2-103", "Sh2-103"):
        rows = backend.query_sessions(target=spelling)
        assert len(rows) == 1, f"{spelling!r} failed to match"


def test_find_darks(tmp_path):
    db = make_db(tmp_path)
    rows = find_darks(LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0)
    assert len(rows) == 1
    assert rows[0]["folder_path"] == "00_Calibration/Darks/ZWOASI585MCPro"


def test_find_darks_no_match(tmp_path):
    db = make_db(tmp_path)
    rows = find_darks(LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=60.0)
    assert rows == []


def test_find_flats_one_match(tmp_path):
    # narrow window: only the adjacent 02-19 flat is within ±1 of 02-18
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Pro", obs_date="2026-02-18", window_days=1)
    assert len(rows) == 1
    assert rows[0]["capture_date"] == "2026-02-19"


def test_find_flats_two_matches(tmp_path):
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Pro", obs_date="2026-02-19")
    assert len(rows) == 2
    assert rows[0]["capture_date"] == "2026-02-19"
    assert rows[1]["capture_date"] == "2026-02-20"


def test_find_flats_default_window_three_days(tmp_path):
    # flats are on 02-19 and 02-20; a session on 02-22 is 2-3 days away.
    # The default ±3 window catches them; the old ±1 window would not.
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Pro", obs_date="2026-02-22")
    assert len(rows) == 2
    # closest first
    assert rows[0]["capture_date"] == "2026-02-20"


def test_find_flats_narrow_window(tmp_path):
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Pro", obs_date="2026-02-22", window_days=1)
    assert rows == []


def test_find_flats_filter_mismatch(tmp_path):
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Extreme", obs_date="2026-02-19")
    assert rows == []


def test_find_flat_darks(tmp_path):
    db = make_db(tmp_path)
    rows = find_flat_darks(LocalBackend(db), camera="ZWO ASI585MC Pro",
                           flat_exposure_sec=1.35, flat_capture_date="2026-02-20")
    assert len(rows) == 1
    assert rows[0]["frame_type"] == "FlatDark"


def test_find_flat_darks_exposure_tolerance(tmp_path):
    db = make_db(tmp_path)
    rows = find_flat_darks(LocalBackend(db), camera="ZWO ASI585MC Pro",
                           flat_exposure_sec=1.40, flat_capture_date="2026-02-20")
    assert len(rows) == 1


def test_find_flat_darks_date_plus_one(tmp_path):
    db = make_db(tmp_path)
    # FlatDark is on 2026-02-20; passing flat_capture_date=2026-02-19 (flat_date+1 fallback)
    rows = find_flat_darks(LocalBackend(db), camera="ZWO ASI585MC Pro",
                           flat_exposure_sec=1.35, flat_capture_date="2026-02-19")
    assert len(rows) == 1
    assert rows[0]["capture_date"] == "2026-02-20"


def test_find_flat_darks_no_match(tmp_path):
    db = make_db(tmp_path)
    rows = find_flat_darks(LocalBackend(db), camera="ZWO ASI585MC Pro",
                           flat_exposure_sec=1.35, flat_capture_date="2026-02-15")
    assert rows == []


def test_importing_catalog_does_not_pull_in_astropy():
    """darkroom.catalog is the read layer for the future web UI — it must
    not pay astropy's import cost. Run in a subprocess for a clean
    sys.modules (other test files in this session import astropy-heavy
    darkroom.cataloger first, which would otherwise pollute the check)."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import darkroom.catalog, sys; "
         "assert 'astropy' not in sys.modules, sorted(k for k in sys.modules if 'astropy' in k)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
