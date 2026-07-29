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
    dark_temp_ties,
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


def insert_dark(
    db: Path,
    set_id: str,
    *,
    camera: str = "ZWO ASI585MC Pro",
    gain: int = 200,
    exposure_sec: float = 180.0,
    temperature_c: float | None,
    is_master: int = 0,
    capture_date: str = "2026-02-01",
    folder_path: str = "00_Calibration/Darks/ZWOASI585MCPro",
    frame_count: int = 30,
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO calibration_sets VALUES (?, 'Dark', ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
        (set_id, camera, gain, exposure_sec, temperature_c, capture_date, folder_path, frame_count, is_master),
    )
    conn.commit()
    conn.close()


def test_find_darks(tmp_path):
    db = make_db(tmp_path)
    rows = find_darks(LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0)
    assert len(rows) == 1
    assert rows[0]["folder_path"] == "00_Calibration/Darks/ZWOASI585MCPro"


def test_find_darks_no_match(tmp_path):
    db = make_db(tmp_path)
    rows = find_darks(LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=60.0)
    assert rows == []


def test_find_darks_temperature_prefers_exact_setpoint(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_master_-20", temperature_c=-20.0, is_master=1)
    insert_dark(db, "Dark_master_-10", temperature_c=-10.0, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0,
        temperature_c=-20.0,
    )
    assert rows[0]["set_id"] == "Dark_master_-20"
    # B11: the -10C master must be gone, not merely outranked — the bug was
    # that every master in the ladder got symlinked alongside the right one.
    assert "Dark_master_-10" not in {r["set_id"] for r in rows}


def test_find_darks_temperature_uncooled_ladder_nearest_wins(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_15", temperature_c=15.0, is_master=1)
    insert_dark(db, "Dark_20", temperature_c=20.0, is_master=1)
    insert_dark(db, "Dark_25", temperature_c=25.0, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0,
        temperature_c=22.0,
    )
    assert rows[0]["temperature_c"] == 20.0


def test_find_darks_temperature_outside_tolerance_returns_empty(tmp_path):
    # distinct exposure_sec keeps the base fixture's -20.0C row (exposure_sec=180.0)
    # from matching the query itself
    db = make_db(tmp_path)
    insert_dark(db, "Dark_15", exposure_sec=75.0, temperature_c=15.0, is_master=1)
    insert_dark(db, "Dark_20", exposure_sec=75.0, temperature_c=20.0, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=75.0,
        temperature_c=-20.0,
    )
    assert rows == []


def test_find_darks_temperature_explicit_tolerance_narrows_results(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_18", temperature_c=18.0, is_master=1)
    insert_dark(db, "Dark_21", temperature_c=21.0, is_master=1)
    rows_default = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0,
        temperature_c=18.0,
    )
    assert len(rows_default) == 2
    rows_narrow = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0,
        temperature_c=18.0, temp_tolerance=2.0,
    )
    assert len(rows_narrow) == 1
    assert rows_narrow[0]["temperature_c"] == 18.0


def test_find_darks_null_temperature_is_fallback_not_a_miss(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_null", temperature_c=None, is_master=1)
    insert_dark(db, "Dark_22", temperature_c=22.0, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=180.0,
        temperature_c=20.0,
    )
    assert len(rows) == 2
    assert rows[0]["temperature_c"] == 22.0
    assert any(r["temperature_c"] is None for r in rows)


def test_find_darks_null_temperature_matches_regardless_of_tolerance(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_null_60s", exposure_sec=60.0, temperature_c=None, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=60.0,
        temperature_c=-99.0,
    )
    assert len(rows) == 1
    assert rows[0]["temperature_c"] is None


def test_find_darks_masters_rank_before_nearer_raw(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_raw_exact", exposure_sec=90.0, temperature_c=-20.0, is_master=0)
    insert_dark(db, "Dark_master_off", exposure_sec=90.0, temperature_c=-18.0, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=90.0,
        temperature_c=-20.0,
    )
    assert rows[0]["is_master"]


def test_find_darks_temperature_none_skips_matching(tmp_path):
    db = make_db(tmp_path)
    insert_dark(db, "Dark_a", exposure_sec=45.0, temperature_c=-20.0, is_master=1)
    insert_dark(db, "Dark_b", exposure_sec=45.0, temperature_c=15.0, is_master=1)
    insert_dark(db, "Dark_c", exposure_sec=45.0, temperature_c=40.0, is_master=1)
    rows = find_darks(
        LocalBackend(db), camera="ZWO ASI585MC Pro", gain=200, exposure_sec=45.0,
    )
    assert len(rows) == 3


def test_dark_temp_ties_ambiguous_between_two_setpoints():
    rows = [{"temperature_c": 20.0}, {"temperature_c": 25.0}]
    result = dark_temp_ties(rows, 22.5)
    assert len(result) == 2


def test_dark_temp_ties_clear_nearest_returns_empty():
    rows = [{"temperature_c": 20.0}, {"temperature_c": 25.0}]
    result = dark_temp_ties(rows, 21.0)
    assert result == []


def test_dark_temp_ties_same_temperature_is_not_ambiguous():
    rows = [{"temperature_c": -20.0}, {"temperature_c": -20.0}]
    result = dark_temp_ties(rows, -20.0)
    assert result == []


def test_find_flats_one_match(tmp_path):
    # narrow window: only the adjacent 02-19 flat is within ±1 of 02-18
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Pro", obs_date="2026-02-18", window_days=1)
    assert len(rows) == 1
    assert rows[0]["capture_date"] == "2026-02-19"


def test_find_flats_two_matches_prefers_the_morning_after(tmp_path):
    """Both sets are from this run; the morning-after one is the habitual case.

    Flats on the session's own evening (offset 0) happen when filters are
    changed mid-session; flats the following morning (offset +1) are the normal
    workflow. Both rank above anything outside the run, and +1 wins between them.
    """
    db = make_db(tmp_path)
    rows = find_flats(LocalBackend(db), camera="ZWO ASI585MC Pro", ota="FRA400",
                      filter_="L-Pro", obs_date="2026-02-19")
    assert len(rows) == 2
    assert rows[0]["capture_date"] == "2026-02-20"   # +1, morning after
    assert rows[1]["capture_date"] == "2026-02-19"   # 0, same evening


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


# ── flat-morning ordering (directional rule) ─────────────────────────────────

from darkroom.catalog import flat_offset_days, flat_sort_key


def test_flat_offset_days():
    assert flat_offset_days("2026-07-29", "2026-07-28") == 1
    assert flat_offset_days("2026-07-28", "2026-07-28") == 0
    assert flat_offset_days("2026-07-27", "2026-07-28") == -1


def test_flat_sort_key_prefers_this_run_over_anything_closer_in_the_past():
    """The regression this rule exists for: -1 day used to tie with +1 and win.

    A session on night N shot flats on the morning of N+1. A previous night's
    flats (N-1) are equidistant, and used to be picked by whatever order the
    backend returned — despite being a different run with a different sky.
    """
    obs = "2026-07-28"
    assert flat_sort_key("2026-07-29", obs) < flat_sort_key("2026-07-27", obs)


def test_flat_sort_key_morning_after_beats_same_evening():
    obs = "2026-07-28"
    assert flat_sort_key("2026-07-29", obs) < flat_sort_key("2026-07-28", obs)


def test_flat_sort_key_in_run_beats_every_fallback():
    obs = "2026-07-28"
    in_run = [flat_sort_key(d, obs) for d in ("2026-07-28", "2026-07-29")]
    fallback = [flat_sort_key(d, obs) for d in ("2026-07-27", "2026-07-30",
                                                "2026-07-25", "2026-07-31")]
    assert max(in_run) < min(fallback)


def test_flat_sort_key_fallbacks_ranked_by_proximity_then_later():
    obs = "2026-07-28"
    ordered = sorted(["2026-07-25", "2026-07-31", "2026-07-27", "2026-07-30"],
                     key=lambda d: flat_sort_key(d, obs))
    # -1 and +2 are nearest; on the -3/+3 tie the later date wins
    assert ordered == ["2026-07-27", "2026-07-30", "2026-07-31", "2026-07-25"]


def test_find_flats_picks_morning_after_over_previous_night(tmp_path):
    """End-to-end shape of the NGC 281 case seen on real data."""
    from darkroom.cataloger import init_db, upsert_calibration_set

    db = tmp_path / "cat.db"
    init_db(db)
    for capture_date, exposure in (("2026-07-27", 0.31), ("2026-07-29", 0.05)):
        upsert_calibration_set(db, {
            "set_id": f"Flat_{capture_date}", "frame_type": "Flat",
            "camera": "ZWOASI585MCPro", "ota": "FRA400", "filter": "L-Synergy",
            "gain": 200, "exposure_sec": exposure, "temperature_c": -10.0,
            "frame_count": 30, "capture_date": capture_date, "folder_path": "p",
        })

    rows = find_flats(LocalBackend(db), camera="ZWOASI585MCPro", ota="FRA400",
                      filter_="L-Synergy", obs_date="2026-07-28")
    assert [r["capture_date"] for r in rows] == ["2026-07-29", "2026-07-27"]
