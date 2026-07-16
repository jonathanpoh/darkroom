import os
import sys
import types
import pytest
from pathlib import Path

from astropy.io import fits

sys.path.insert(0, str(Path(__file__).parent.parent))

from darkroom.cataloger import (
    parse_filter,
    parse_ota,
    make_session_id,
    find_lights_folders,
    compute_imaging_night,
    SessionAnalyzer,
    FITSHeaderExtractor,
    _parse_site_deg,
    init_db,
    upsert_session,
    upsert_calibration_set,
    mark_processed,
    mark_processed_command,
    set_processed_state,
    _find_latest_processed_date,
    mark_processed_by_target,
    finish_command,
)
from darkroom.catalog_db import add_site


class TestParseFilter:
    def test_l_pro(self):
        assert parse_filter("Light_M 81_180.0s_Bin1_0C_20260219_L-Pro_0186") == "L-Pro"

    def test_lextreme_normalized(self):
        assert parse_filter("Light_NGC7380_300.0s_Bin1_-20C_20251001_LExtreme_0001") == "L-Extreme"

    def test_no_filter(self):
        assert parse_filter("Light_M81_180.0s_Bin1_0C_20260219_NoFilter_0001") == "NoFilter"

    def test_temperature_at_parts_minus2_returns_none(self):
        # parts[-2] looks like a temperature → no filter in filename
        assert parse_filter("Light_M81_300.0s_Bin1_-20C_0001") is None

    def test_too_few_parts_returns_none(self):
        assert parse_filter("single") is None

    def test_negative_temp_with_decimal(self):
        # -20.5C should be recognised as temp, not filter
        assert parse_filter("Light_M81_300.0s_Bin1_-20.5C_0001") is None


class TestParseOta:
    def test_180_is_fma180(self):
        assert parse_ota(180) == "FMA180"

    def test_400_is_fra400(self):
        assert parse_ota(400) == "FRA400"

    def test_none_is_unknown(self):
        assert parse_ota(None) == "Unknown"

    def test_other_value_is_unknown(self):
        assert parse_ota(300) == "Unknown"

    def test_string_coerced(self):
        # FITS headers sometimes return strings
        assert parse_ota("180") == "FMA180"
        assert parse_ota("400") == "FRA400"

    def test_tolerance_fra400(self):
        # ASIAir reports measured focal length — FRA400 often shows as 402
        assert parse_ota(402) == "FRA400"
        assert parse_ota(390) == "FRA400"
        assert parse_ota(410) == "FRA400"

    def test_tolerance_fma180(self):
        assert parse_ota(178) == "FMA180"
        assert parse_ota(170) == "FMA180"
        assert parse_ota(190) == "FMA180"

    def test_280_is_fra400_reducer(self):
        assert parse_ota(280) == "FRA400-07x"

    def test_tolerance_fra400_reducer(self):
        assert parse_ota(270) == "FRA400-07x"
        assert parse_ota(290) == "FRA400-07x"

    def test_outside_tolerance_is_unknown(self):
        assert parse_ota(250) == "Unknown"
        assert parse_ota(411) == "Unknown"
        assert parse_ota(169) == "Unknown"


class TestMakeSessionId:
    def test_canonical(self):
        assert make_session_id("M 81", "2026-02-19", "FRA400", "ASI585MC", "L-Pro") == \
            "M81_20260219_FRA400_ASI585MC_L-Pro"

    def test_spaces_stripped_from_target(self):
        assert make_session_id("NGC 7380", "2025-10-01", "FRA400", "Canon6D", "L-Extreme") == \
            "NGC7380_20251001_FRA400_Canon6D_L-Extreme"

    def test_empty_filter_becomes_unknownfilter(self):
        assert make_session_id("M 45", "2024-09-19", "FRA400", "Canon6D", "") == \
            "M45_20240919_FRA400_Canon6D_UnknownFilter"

    def test_none_filter_becomes_unknownfilter(self):
        assert make_session_id("M 45", "2024-09-19", "FRA400", "Canon6D", None) == \
            "M45_20240919_FRA400_Canon6D_UnknownFilter"

    def test_target_with_multiple_spaces(self):
        assert make_session_id("IC 1805", "2025-11-01", "FMA180", "ASI585MC", "L-Extreme") == \
            "IC1805_20251101_FMA180_ASI585MC_L-Extreme"


class TestFindLightsFolders:
    def test_canonical_structure(self, tmp_path):
        # Old canonical: M81/2026-02-19_FRA400_ASI585MC_L-Pro/Lights/frame.fit
        lights = tmp_path / "M81" / "2026-02-19_FRA400_ASI585MC_L-Pro" / "Lights"
        lights.mkdir(parents=True)
        (lights / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert lights in result

    def test_new_canonical_structure(self, tmp_path):
        # New canonical: M81/2026-02-19_FRA400_ASI585MC/Lights/L-Pro/frame.fit
        filter_dir = tmp_path / "M81" / "2026-02-19_FRA400_ASI585MC" / "Lights" / "L-Pro"
        filter_dir.mkdir(parents=True)
        (filter_dir / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert filter_dir in result

    def test_old_structure_no_lights_subfolder(self, tmp_path):
        # IC 1805/Lights - Rasa 8"/frame.fit  (no Lights subfolder)
        folder = tmp_path / "IC 1805" / "Lights - Rasa 8\""
        folder.mkdir(parents=True)
        (folder / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert folder in result

    def test_fits_extension(self, tmp_path):
        lights = tmp_path / "M45" / "session" / "Lights"
        lights.mkdir(parents=True)
        (lights / "frame001.fits").touch()
        result = find_lights_folders(tmp_path)
        assert lights in result

    def test_skips_eadir(self, tmp_path):
        eadir = tmp_path / "@eaDir" / "Lights"
        eadir.mkdir(parents=True)
        (eadir / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert eadir not in result

    def test_skips_eadir_nested(self, tmp_path):
        eadir = tmp_path / "M81" / "@eaDir" / "Lights"
        eadir.mkdir(parents=True)
        (eadir / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert eadir not in result

    def test_skips_processed_folder(self, tmp_path):
        processed = tmp_path / "M81" / "_Processed" / "2026-05-15"
        processed.mkdir(parents=True)
        (processed / "master_Light.fit").touch()
        result = find_lights_folders(tmp_path)
        assert processed not in result

    def test_skips_reject_folder(self, tmp_path):
        lights = tmp_path / "M81" / "session" / "Lights"
        lights.mkdir(parents=True)
        (lights / "frame001.fit").touch()
        reject = lights.parent / "reject"
        reject.mkdir()
        (reject / "bad001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert lights in result
        assert reject not in result

    def test_skips_bad_folder(self, tmp_path):
        bad = tmp_path / "M81" / "session" / "bad"
        bad.mkdir(parents=True)
        (bad / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert bad not in result

    def test_empty_root(self, tmp_path):
        assert find_lights_folders(tmp_path) == []

    def test_ignores_non_fits_files(self, tmp_path):
        folder = tmp_path / "session" / "Lights"
        folder.mkdir(parents=True)
        (folder / "readme.txt").touch()
        assert find_lights_folders(tmp_path) == []


class TestComputeImagingNight:
    def test_evening_before_midnight_is_same_local_date(self):
        # 22:00 UTC in February = 22:00 local (Lisbon is UTC+0 in winter)
        assert compute_imaging_night("2026-02-19T22:00:00") == "2026-02-19"

    def test_early_morning_after_midnight_maps_to_previous_day(self):
        # 01:00 UTC Feb 20 = 01:00 local (UTC+0) → pre-noon → night of Feb 19
        assert compute_imaging_night("2026-02-20T01:00:00") == "2026-02-19"

    def test_session_crossing_midnight_is_one_night(self):
        # Both a 23:30 frame and a 03:00 frame the next UTC day belong to the same night
        night_start = compute_imaging_night("2026-03-14T23:30:00")
        after_midnight = compute_imaging_night("2026-03-15T03:00:00")
        assert night_start == after_midnight == "2026-03-14"

    def test_afternoon_frame_starts_new_night(self):
        # 13:00 local is post-noon → belongs to that day's night (not previous)
        # In March, Lisbon is still UTC+0 before DST
        assert compute_imaging_night("2026-03-14T13:00:00") == "2026-03-14"

    def test_dst_boundary_no_exception(self):
        # Europe/Lisbon springs forward last Sunday in March at 01:00 UTC
        # 2026-03-29T01:30:00 UTC is during the DST transition — should not raise
        result = compute_imaging_night("2026-03-29T01:30:00")
        assert result is not None
        assert len(result) == 10  # YYYY-MM-DD

    def test_empty_string_returns_none(self):
        assert compute_imaging_night("") is None

    def test_none_equivalent_returns_none(self):
        assert compute_imaging_night(None) is None

    def test_malformed_returns_none(self):
        assert compute_imaging_night("not-a-date") is None


class TestAnalyzeSessions:
    def _make_meta(self, **overrides):
        base = {
            "filename_stem": "Light_M81_180.0s_Bin1_0C_20260219_L-Pro_0001",
            "file_path": "/fake/path/frame.fit",
            "date_obs": "2026-02-19T22:00:00",  # 22:00 UTC Feb 19 = local evening Feb 19
            "exposure": 180.0,
            "camera": "ZWO ASI585MC",
            "gain": 200,
            "temperature": -10.0,
            "focallen": 400,
            "filter_header": None,
            "object": "M 81",
            "ra_deg": None,
            "dec_deg": None,
        }
        base.update(overrides)
        return base

    def test_single_night_returns_one_session(self, tmp_path):
        lights = tmp_path / "M81" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [
            self._make_meta(),
            self._make_meta(filename_stem="Light_M81_180.0s_Bin1_0C_20260219_L-Pro_0002"),
        ]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert len(result) == 1
        s = result[0]
        assert s["target"] == "M 81"
        assert s["obs_date"] == "2026-02-19"
        assert s["ota"] == "FRA400"
        assert s["camera"] == "ZWO ASI585MC"
        assert s["filter"] == "L-Pro"
        assert s["gain"] == 200
        assert s["frame_count"] == 2
        assert s["total_integration_sec"] == 360
        assert s["exposure_sec"] == 180.0
        assert s["lights_path"] == str(lights)

    def test_multi_night_returns_multiple_sessions(self, tmp_path):
        lights = tmp_path / "M81" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [
            self._make_meta(date_obs="2026-02-19T22:00:00"),  # night of Feb 19
            self._make_meta(date_obs="2026-02-20T23:00:00"),  # night of Feb 20
            self._make_meta(date_obs="2026-02-20T01:00:00"),  # also night of Feb 19 (post-midnight)
        ]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert len(result) == 2
        assert result[0]["obs_date"] == "2026-02-19"
        assert result[0]["frame_count"] == 2
        assert result[0]["total_integration_sec"] == 360
        assert result[1]["obs_date"] == "2026-02-20"
        assert result[1]["frame_count"] == 1

    def test_midnight_crossing_is_one_session(self, tmp_path):
        lights = tmp_path / "M81" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [
            self._make_meta(date_obs="2026-03-14T23:30:00"),  # local 23:30 → night Mar 14
            self._make_meta(date_obs="2026-03-15T01:00:00"),  # local 01:00 → still Mar 14
            self._make_meta(date_obs="2026-03-15T03:45:00"),  # local 03:45 → still Mar 14
        ]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert len(result) == 1
        assert result[0]["obs_date"] == "2026-03-14"
        assert result[0]["frame_count"] == 3

    def test_filter_from_filename_overrides_header(self, tmp_path):
        lights = tmp_path / "session" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [self._make_meta(filter_header="L-Quad")]  # header says different filter
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert result[0]["filter"] == "L-Pro"  # filename wins

    def test_filter_falls_back_to_header(self, tmp_path):
        lights = tmp_path / "session" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [self._make_meta(
            filename_stem="Light_M81_300.0s_Bin1_-20C_0001",
            filter_header="L-eXtreme",
        )]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert result[0]["filter"] == "L-eXtreme"

    def test_empty_metadata_returns_empty_list(self, tmp_path):
        lights = tmp_path / "session" / "Lights"
        lights.mkdir(parents=True)
        assert SessionAnalyzer.analyze_sessions([], lights) == []

    def test_ota_from_focallen(self, tmp_path):
        lights = tmp_path / "session" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [self._make_meta(focallen=180)]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert result[0]["ota"] == "FMA180"

    def test_frame_missing_date_obs_is_skipped(self, tmp_path, capsys):
        lights = tmp_path / "session" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [
            self._make_meta(date_obs=""),  # no date — should be skipped
            self._make_meta(date_obs="2026-02-19T22:00:00"),
        ]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert len(result) == 1
        captured = capsys.readouterr()
        assert "no resolvable DATE-OBS" in captured.err


import sqlite3


class TestMarkProcessed:
    def _insert_session(self, db):
        init_db(db)
        upsert_session(db, {
            "session_id": "M81_20260219_FRA400_ASI585MC_L-Pro",
            "target": "M 81",
            "obs_date": "2026-02-19",
            "ota": "FRA400",
            "camera": "ZWO ASI585MC",
            "filter": "L-Pro",
            "gain": 200,
            "temperature_c": -10.0,
            "exposure_sec": 180.0,
            "focal_length": 400.0,
            "frame_count": 10,
            "total_integration_sec": 1800,
            "ra_deg": None,
            "dec_deg": None,
            "lights_path": "/Volumes/test",
            "processed_status": "",
            "notes": "",
        })

    def test_mark_processed_updates_status(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert_session(db)
        mark_processed(db, "M81_20260219_FRA400_ASI585MC_L-Pro", "2026-03-01")
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT processed_status FROM sessions WHERE session_id = ?",
                ("M81_20260219_FRA400_ASI585MC_L-Pro",)
            ).fetchone()
        assert row["processed_status"] == "2026-03-01"

    def test_mark_processed_unknown_id_returns_false(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert_session(db)
        result = mark_processed(db, "NonExistent_ID", "2026-03-01")
        assert result is False

    def test_mark_processed_known_id_returns_true(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert_session(db)
        result = mark_processed(db, "M81_20260219_FRA400_ASI585MC_L-Pro", "2026-03-01")
        assert result is True


class TestSQLiteCatalog:
    def _sample_session(self):
        return {
            "session_id": "M81_20260219_FRA400_ASI585MC_L-Pro",
            "target": "M 81",
            "obs_date": "2026-02-19",
            "ota": "FRA400",
            "camera": "ZWO ASI585MC",
            "filter": "L-Pro",
            "gain": 200,
            "temperature_c": -10.0,
            "exposure_sec": 180.0,
            "focal_length": 400.0,
            "frame_count": 10,
            "total_integration_sec": 1800,
            "ra_deg": None,
            "dec_deg": None,
            "lights_path": "/Volumes/Astrophotography/04_Deep Sky Objects/M 81/Lights",
            "processed_status": "",
            "notes": "",
        }

    def _sample_cal_set(self):
        return {
            "set_id": "Dark_ZWOASi585MC_180s_200g_-10C_20260219",
            "frame_type": "Dark",
            "camera": "ZWO ASI585MC",
            "ota": "FRA400",
            "filter": None,
            "gain": 200,
            "exposure_sec": 180.0,
            "temperature_c": -10.0,
            "frame_count": 30,
            "capture_date": "2026-02-19",
            "folder_path": "/Volumes/Astrophotography/00_Calibration/Darks",
        }

    def test_init_db_creates_tables(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "sessions" in tables
        assert "calibration_sets" in tables

    def test_init_db_enables_wal(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        with sqlite3.connect(db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_init_db_creates_indexes(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        with sqlite3.connect(db) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
        assert "idx_sessions_target" in names
        assert "idx_sessions_obs_date" in names

    def test_upsert_session_insert(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        upsert_session(db, self._sample_session())
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                ("M81_20260219_FRA400_ASI585MC_L-Pro",)
            ).fetchone()
        assert row is not None
        assert row["target"] == "M 81"
        assert row["frame_count"] == 10

    def test_upsert_session_does_not_overwrite_processed_status(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        session = self._sample_session()
        upsert_session(db, session)
        # manually set processed_status
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE sessions SET processed_status = '2026-03-01' WHERE session_id = ?",
                (session["session_id"],)
            )
        # re-upsert (simulating a re-scan)
        upsert_session(db, session)
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT processed_status FROM sessions WHERE session_id = ?",
                (session["session_id"],)
            ).fetchone()
        assert row["processed_status"] == "2026-03-01"  # NOT overwritten

    def test_upsert_calibration_set(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        upsert_calibration_set(db, self._sample_cal_set())
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM calibration_sets WHERE set_id = ?",
                ("Dark_ZWOASi585MC_180s_200g_-10C_20260219",)
            ).fetchone()
        assert row is not None
        assert row["frame_count"] == 30

    def test_init_db_adds_timestamp_columns(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        with sqlite3.connect(db) as conn:
            session_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            cal_cols = {r[1] for r in conn.execute("PRAGMA table_info(calibration_sets)")}
        assert {"created_at", "updated_at"} <= session_cols
        assert {"created_at", "updated_at"} <= cal_cols

    def test_init_db_backfills_timestamps_on_existing_rows(self, tmp_path):
        # Simulate a pre-migration DB: create the old schema (no timestamp
        # columns) with one row already in it, then run init_db and confirm
        # the migration backfills rather than crashing or leaving NULLs.
        db = tmp_path / "test.db"
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    obs_date TEXT NOT NULL
                );
                CREATE TABLE calibration_sets (
                    set_id TEXT PRIMARY KEY,
                    frame_type TEXT NOT NULL
                );
            """)
            conn.execute(
                "INSERT INTO sessions (session_id, target, obs_date) VALUES (?, ?, ?)",
                ("M81_20260219_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-19"),
            )
            conn.execute(
                "INSERT INTO calibration_sets (set_id, frame_type) VALUES (?, ?)",
                ("Dark_ASI585MC_200g_180s", "Dark"),
            )

        init_db(db)  # must not raise, must backfill

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT created_at, updated_at FROM sessions WHERE session_id = ?",
                ("M81_20260219_FRA400_ASI585MC_L-Pro",),
            ).fetchone()
            cal_row = conn.execute(
                "SELECT created_at, updated_at FROM calibration_sets WHERE set_id = ?",
                ("Dark_ASI585MC_200g_180s",),
            ).fetchone()
        assert row[0] is not None and row[1] is not None
        assert cal_row[0] is not None and cal_row[1] is not None

    def test_upsert_session_sets_created_and_updated_at(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        session = self._sample_session()
        upsert_session(db, session)
        with sqlite3.connect(db) as conn:
            created1, updated1 = conn.execute(
                "SELECT created_at, updated_at FROM sessions WHERE session_id = ?",
                (session["session_id"],),
            ).fetchone()
        assert created1 is not None
        assert updated1 is not None

        upsert_session(db, session)  # re-scan: created_at preserved, updated_at refreshed
        with sqlite3.connect(db) as conn:
            created2, updated2 = conn.execute(
                "SELECT created_at, updated_at FROM sessions WHERE session_id = ?",
                (session["session_id"],),
            ).fetchone()
        assert created2 == created1


class TestFindLatestProcessedDate:
    def test_single_date_dir_returned(self, tmp_path):
        (tmp_path / "2026-05-15").mkdir()
        assert _find_latest_processed_date(tmp_path) == "2026-05-15"

    def test_multiple_dirs_returns_most_recent(self, tmp_path):
        (tmp_path / "2026-03-01").mkdir()
        (tmp_path / "2026-05-15").mkdir()
        assert _find_latest_processed_date(tmp_path) == "2026-05-15"

    def test_multiple_dirs_prints_notice(self, tmp_path, capsys):
        (tmp_path / "2026-03-01").mkdir()
        (tmp_path / "2026-05-15").mkdir()
        _find_latest_processed_date(tmp_path)
        assert "Multiple processed dates" in capsys.readouterr().out

    def test_non_date_dirs_ignored(self, tmp_path):
        (tmp_path / "2026-05-15").mkdir()
        (tmp_path / "master").mkdir()
        (tmp_path / "processed").mkdir()
        assert _find_latest_processed_date(tmp_path) == "2026-05-15"

    def test_missing_root_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _find_latest_processed_date(tmp_path / "nonexistent")

    def test_no_date_dirs_exits(self, tmp_path):
        (tmp_path / "master").mkdir()
        with pytest.raises(SystemExit):
            _find_latest_processed_date(tmp_path)


class TestMarkProcessedByTarget:
    def _populate(self, db):
        init_db(db)

        def row(sid, target, obs_date, filt):
            return {
                "session_id": sid, "target": target, "obs_date": obs_date,
                "ota": "FRA400", "camera": "ZWO ASI585MC", "filter": filt,
                "gain": 200, "temperature_c": -10.0, "exposure_sec": 180.0,
                "focal_length": 400.0,
                "frame_count": 10, "total_integration_sec": 1800,
                "ra_deg": None, "dec_deg": None, "lights_path": "/fake",
                "processed_status": "", "notes": "",
            }

        upsert_session(db, row("M81_20260219_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-19", "L-Pro"))
        upsert_session(db, row("M81_20260220_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-20", "L-Pro"))
        upsert_session(db, row("NGC7380_20251001_FRA400_ASI585MC_L-Extreme", "NGC 7380", "2025-10-01", "L-Extreme"))

    def test_marks_all_sessions_for_target(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        count = mark_processed_by_target(db, "M 81", "2026-05-15")
        assert count == 2
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT processed_state, processed_date FROM sessions WHERE target = 'M 81'"
            ).fetchall()
        assert all(r == ("processed", "2026-05-15") for r in rows)

    def test_case_insensitive_match(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        count = mark_processed_by_target(db, "m 81", "2026-05-15")
        assert count == 2

    def test_missing_space_match(self, tmp_path):
        # 'M81' (no space) should match the stored 'M 81'
        db = tmp_path / "test.db"
        self._populate(db)
        count = mark_processed_by_target(db, "M81", "2026-05-15")
        assert count == 2

    def test_does_not_affect_other_targets(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        mark_processed_by_target(db, "M 81", "2026-05-15")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state FROM sessions WHERE target = 'NGC 7380'"
            ).fetchone()
        assert row[0] == "unprocessed"

    def test_no_match_returns_zero(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        count = mark_processed_by_target(db, "Unknown Target", "2026-05-15")
        assert count == 0


class TestFinishCommand:
    def _populate(self, db):
        init_db(db)

        def row(sid, target, obs_date, filt):
            return {
                "session_id": sid, "target": target, "obs_date": obs_date,
                "ota": "FRA400", "camera": "ZWO ASI585MC", "filter": filt,
                "gain": 200, "temperature_c": -10.0, "exposure_sec": 180.0,
                "focal_length": 400.0,
                "frame_count": 10, "total_integration_sec": 1800,
                "ra_deg": None, "dec_deg": None, "lights_path": "/fake",
                "processed_status": "", "notes": "",
            }

        upsert_session(db, row("M81_20260219_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-19", "L-Pro"))
        upsert_session(db, row("M81_20260220_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-20", "L-Pro"))

    def _args(self, **kwargs):
        defaults = {"db": None, "target": "M 81", "archive": None, "date": None, "session": None}
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_date_flag_marks_all_sessions_for_target(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        finish_command(self._args(db=str(db), date="2026-05-15"))
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT processed_state, processed_date FROM sessions WHERE target = 'M 81'"
            ).fetchall()
        assert all(r == ("processed", "2026-05-15") for r in rows)

    def test_archive_flag_detects_date_from_processed_dir(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        processed_dir = tmp_path / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-15"
        processed_dir.mkdir(parents=True)
        finish_command(self._args(db=str(db), archive=str(tmp_path)))
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT processed_state, processed_date FROM sessions WHERE target = 'M 81'"
            ).fetchall()
        assert all(r == ("processed", "2026-05-15") for r in rows)

    def test_session_flag_only_updates_specified_sessions(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        finish_command(self._args(
            db=str(db),
            date="2026-05-15",
            session=["M81_20260219_FRA400_ASI585MC_L-Pro"],
        ))
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            rows = {
                r["session_id"]: r["processed_status"]
                for r in conn.execute(
                    "SELECT session_id, processed_status FROM sessions WHERE target = 'M 81'"
                ).fetchall()
            }
        assert rows["M81_20260219_FRA400_ASI585MC_L-Pro"] == "2026-05-15"
        assert rows["M81_20260220_FRA400_ASI585MC_L-Pro"] == ""

    def test_unknown_session_id_warns(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        self._populate(db)
        with pytest.raises(SystemExit) as exc_info:
            finish_command(self._args(
                db=str(db),
                date="2026-05-15",
                session=["NoSuchSession_ID"],
            ))
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_missing_db_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            finish_command(self._args(db=str(tmp_path / "missing.db"), date="2026-05-15"))

    def test_target_not_found_warns(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        self._populate(db)
        finish_command(self._args(db=str(db), date="2026-05-15", target="Unknown Target"))
        assert "no sessions found" in capsys.readouterr().err


# ── W1/W2/W3: structured processed status, filter NULL sentinel, id PK ──────

def _create_old_schema_db(db, rows):
    """Build a pre-W1/W2/W3 sessions table (session_id PK, free-text
    processed_status, filter='' sentinel) and insert the given rows.

    Each row is a dict with at minimum session_id/target/obs_date; any of
    filter/processed_status/notes may be included.
    """
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                obs_date TEXT NOT NULL,
                ota TEXT, camera TEXT, filter TEXT, gain INTEGER,
                temperature_c REAL, exposure_sec REAL, focal_length REAL,
                frame_count INTEGER, total_integration_sec INTEGER,
                total_integration_hours REAL GENERATED ALWAYS AS (total_integration_sec / 3600.0) VIRTUAL,
                ra_deg REAL, dec_deg REAL, lights_path TEXT,
                processed_status TEXT, notes TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE calibration_sets (set_id TEXT PRIMARY KEY, frame_type TEXT NOT NULL);
            CREATE INDEX idx_sessions_target ON sessions(target);
            CREATE INDEX idx_sessions_obs_date ON sessions(obs_date);
        """)
        for row in rows:
            row = {
                "ota": None, "camera": None, "filter": None, "gain": None,
                "temperature_c": None, "exposure_sec": None, "focal_length": None,
                "frame_count": None, "total_integration_sec": None,
                "ra_deg": None, "dec_deg": None, "lights_path": None,
                "processed_status": None, "notes": None,
                **row,
            }
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO sessions ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )


class TestSchemaMigration:
    def test_id_column_becomes_integer_primary_key(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19"},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            info = {r[1]: r for r in conn.execute("PRAGMA table_info(sessions)")}
        assert info["id"][2] == "INTEGER"
        assert info["id"][5] == 1  # pk column index (1-based), not 0
        assert info["session_id"][3] == 1  # NOT NULL
        assert info["session_id"][5] == 0  # no longer part of the PK

    def test_session_id_is_unique_not_null(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19"},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sessions (session_id, target, obs_date) VALUES (?, ?, ?)",
                    ("S1", "NGC 7380", "2026-03-01"),
                )

    def test_data_preserved_across_rebuild(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "filter": "L-Pro", "frame_count": 42},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute("SELECT * FROM sessions WHERE session_id = 'S1'").fetchone())
        assert row["target"] == "M 81"
        assert row["obs_date"] == "2026-02-19"
        assert row["filter"] == "L-Pro"
        assert row["frame_count"] == 42

    def test_backfill_exact_date(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": "2026-03-01"},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_path, processed_date FROM sessions WHERE session_id = 'S1'"
            ).fetchone()
        assert row == ("processed", None, "2026-03-01")

    def test_backfill_processed_path(self, tmp_path):
        db = tmp_path / "old.db"
        path = "01_Deep Sky Objects/M 81/_Processed/2026-05-15"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": path},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_path, processed_date FROM sessions WHERE session_id = 'S1'"
            ).fetchone()
        assert row == ("processed", path, "2026-05-15")

    def test_backfill_skipped_copies_status_into_empty_notes(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": "skipped - bad tracking", "notes": None},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_path, processed_date, notes FROM sessions WHERE session_id = 'S1'"
            ).fetchone()
        assert row == ("skipped", None, None, "skipped - bad tracking")

    def test_backfill_skipped_does_not_overwrite_existing_notes(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": "Skip: cloud cover", "notes": "already had a note"},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, notes FROM sessions WHERE session_id = 'S1'"
            ).fetchone()
        assert row == ("skipped", "already had a note")

    def test_backfill_blank_status_stays_unprocessed(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": ""},
            {"session_id": "S2", "target": "M 81", "obs_date": "2026-02-20",
             "processed_status": None},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT session_id, processed_state, processed_path, processed_date FROM sessions"
            ).fetchall()
        assert set(rows) == {
            ("S1", "unprocessed", None, None),
            ("S2", "unprocessed", None, None),
        }

    def test_backfill_other_text_best_effort_path(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": "some custom note"},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_path, processed_date FROM sessions WHERE session_id = 'S1'"
            ).fetchone()
        assert row == ("processed", "some custom note", None)

    def test_filter_empty_string_becomes_null(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19", "filter": ""},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT filter FROM sessions WHERE session_id = 'S1'").fetchone()
        assert row[0] is None

    def test_indexes_present_after_migration(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19"},
        ])
        init_db(db)
        with sqlite3.connect(db) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
        assert {"idx_sessions_target", "idx_sessions_obs_date", "idx_sessions_processed_state"} <= names

    def test_idempotent_rerun_no_data_loss_or_reclobber(self, tmp_path):
        db = tmp_path / "old.db"
        _create_old_schema_db(db, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
             "processed_status": "2026-03-01"},
        ])
        init_db(db)
        # Simulate a manual update via set_processed_state after migration...
        set_processed_state(db, "S1", state="skipped", notes="changed my mind")
        # ...then re-running init_db (e.g. a later scan) must not re-derive
        # processed_state from the stale processed_status column.
        init_db(db)
        init_db(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, notes, session_id FROM sessions"
            ).fetchall()
        assert row == [("skipped", "changed my mind", "S1")]

    def test_fresh_db_and_migrated_db_converge_on_same_columns(self, tmp_path):
        fresh = tmp_path / "fresh.db"
        init_db(fresh)
        old = tmp_path / "old.db"
        _create_old_schema_db(old, [
            {"session_id": "S1", "target": "M 81", "obs_date": "2026-02-19"},
        ])
        init_db(old)
        with sqlite3.connect(fresh) as conn:
            fresh_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        with sqlite3.connect(old) as conn:
            old_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        assert fresh_cols == old_cols


class TestSetProcessedState:
    def _insert(self, db):
        init_db(db)
        upsert_session(db, {
            "session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
            "ota": "FRA400", "camera": "ZWO ASI585MC", "filter": "L-Pro",
            "gain": 200, "temperature_c": -10.0, "exposure_sec": 180.0,
            "focal_length": 400.0, "frame_count": 10, "total_integration_sec": 1800,
            "ra_deg": None, "dec_deg": None, "lights_path": "/fake", "notes": "",
        })

    def test_sets_state_only(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        assert set_processed_state(db, "S1", state="processed") is True
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_date, processed_path FROM sessions WHERE session_id='S1'"
            ).fetchone()
        assert row == ("processed", None, None)

    def test_sets_state_with_date_and_path(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        set_processed_state(
            db, "S1", state="processed",
            processed_date="2026-05-15", processed_path="a/b/_Processed/2026-05-15",
        )
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_date, processed_path FROM sessions WHERE session_id='S1'"
            ).fetchone()
        assert row == ("processed", "2026-05-15", "a/b/_Processed/2026-05-15")

    def test_refreshes_updated_at(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        with sqlite3.connect(db) as conn:
            before = conn.execute("SELECT updated_at FROM sessions WHERE session_id='S1'").fetchone()[0]
        import time
        time.sleep(1.1)
        set_processed_state(db, "S1", state="skipped")
        with sqlite3.connect(db) as conn:
            after = conn.execute("SELECT updated_at FROM sessions WHERE session_id='S1'").fetchone()[0]
        assert after > before

    def test_notes_untouched_when_not_passed(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE sessions SET notes = 'existing note' WHERE session_id='S1'")
        set_processed_state(db, "S1", state="processed")
        with sqlite3.connect(db) as conn:
            notes = conn.execute("SELECT notes FROM sessions WHERE session_id='S1'").fetchone()[0]
        assert notes == "existing note"

    def test_notes_overwritten_when_passed(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        set_processed_state(db, "S1", state="skipped", notes="bad tracking")
        with sqlite3.connect(db) as conn:
            notes = conn.execute("SELECT notes FROM sessions WHERE session_id='S1'").fetchone()[0]
        assert notes == "bad tracking"

    def test_invalid_state_raises_value_error(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        with pytest.raises(ValueError):
            set_processed_state(db, "S1", state="bogus")

    def test_in_progress_state_accepted(self, tmp_path):
        # F1: 'in_progress' is a valid archive-derived state alongside the
        # original three.
        db = tmp_path / "test.db"
        self._insert(db)
        assert set_processed_state(db, "S1", state="in_progress") is True
        with sqlite3.connect(db) as conn:
            state = conn.execute(
                "SELECT processed_state FROM sessions WHERE session_id='S1'"
            ).fetchone()[0]
        assert state == "in_progress"

    def test_unknown_session_id_returns_false(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        assert set_processed_state(db, "NoSuchSession", state="processed") is False


class TestMarkProcessedCommandCLI:
    """darkroom catalog mark → mark_processed_command (structured state)."""

    def _insert(self, db):
        init_db(db)
        upsert_session(db, {
            "session_id": "S1", "target": "M 81", "obs_date": "2026-02-19",
            "ota": "FRA400", "camera": "ZWO ASI585MC", "filter": "L-Pro",
            "gain": 200, "temperature_c": -10.0, "exposure_sec": 180.0,
            "focal_length": 400.0, "frame_count": 10, "total_integration_sec": 1800,
            "ra_deg": None, "dec_deg": None, "lights_path": "/fake", "notes": "",
        })

    def _args(self, **kwargs):
        defaults = {"db": None, "session_id": "S1", "state": "processed",
                    "date": None, "path": None, "notes": None}
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_sets_structured_state(self, tmp_path):
        db = tmp_path / "test.db"
        self._insert(db)
        mark_processed_command(self._args(db=str(db), state="processed", date="2026-05-15"))
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_state, processed_date FROM sessions WHERE session_id='S1'"
            ).fetchone()
        assert row == ("processed", "2026-05-15")

    def test_unknown_session_exits(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        self._insert(db)
        with pytest.raises(SystemExit):
            mark_processed_command(self._args(db=str(db), session_id="NoSuchSession"))
        assert "not found" in capsys.readouterr().err

    def test_missing_db_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            mark_processed_command(self._args(db=str(tmp_path / "missing.db")))


# ── S1: observing-site coordinates from FITS SITELAT/SITELONG headers ───────

class TestParseSiteDeg:
    def test_float_passthrough(self):
        assert _parse_site_deg(38.5631) == 38.5631

    def test_int_passthrough(self):
        assert _parse_site_deg(38) == 38.0

    def test_decimal_string(self):
        assert _parse_site_deg("38.5631") == pytest.approx(38.5631)

    def test_space_separated_sexagesimal(self):
        assert _parse_site_deg("38 33 47") == pytest.approx(38.563056, abs=1e-6)

    def test_colon_separated_sexagesimal_with_plus_sign(self):
        assert _parse_site_deg("+38:33:47") == pytest.approx(38.563056, abs=1e-6)

    def test_negative_sexagesimal(self):
        assert _parse_site_deg("-8 52 53") == pytest.approx(-8.881389, abs=1e-6)

    def test_negative_zero_degrees_still_negative(self):
        # Sign must come from the string token, not the parsed float (-0 == 0).
        assert _parse_site_deg("-0 30 0") == pytest.approx(-0.5)

    def test_garbage_returns_none(self):
        assert _parse_site_deg("garbage") is None

    def test_none_returns_none(self):
        assert _parse_site_deg(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_site_deg("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_site_deg("   ") is None


class TestFITSHeaderExtractorSiteCoords:
    def _make_fits(self, path: Path, sitelat=None, sitelong=None) -> Path:
        hdu = fits.PrimaryHDU()
        hdu.header["OBJECT"] = "M 81"
        hdu.header["DATE-OBS"] = "2026-02-19T22:00:00"
        hdu.header["EXPOSURE"] = 180.0
        if sitelat is not None:
            hdu.header["SITELAT"] = sitelat
        if sitelong is not None:
            hdu.header["SITELONG"] = sitelong
        hdu.writeto(path, overwrite=True)
        return path

    def test_extracts_site_coords(self, tmp_path):
        p = self._make_fits(tmp_path / "frame.fit", sitelat=38.5631, sitelong=-8.88149)
        meta = FITSHeaderExtractor.extract_metadata(p)
        assert meta["site_lat"] == pytest.approx(38.5631)
        assert meta["site_lon"] == pytest.approx(-8.88149)

    def test_missing_headers_return_none(self, tmp_path):
        p = self._make_fits(tmp_path / "frame.fit")
        meta = FITSHeaderExtractor.extract_metadata(p)
        assert meta["site_lat"] is None
        assert meta["site_lon"] is None


class TestAnalyzeSessionsSiteCoords:
    def _make_meta(self, **overrides):
        base = {
            "filename_stem": "Light_M81_180.0s_Bin1_0C_20260219_L-Pro_0001",
            "file_path": "/fake/path/frame.fit",
            "date_obs": "2026-02-19T22:00:00",
            "exposure": 180.0,
            "camera": "ZWO ASI585MC",
            "gain": 200,
            "temperature": -10.0,
            "focallen": 400,
            "filter_header": None,
            "object": "M 81",
            "ra_deg": None,
            "dec_deg": None,
            "site_lat": None,
            "site_lon": None,
        }
        base.update(overrides)
        return base

    def test_carries_first_frame_site_coords(self, tmp_path):
        lights = tmp_path / "M81" / "Lights"
        lights.mkdir(parents=True)
        meta_list = [
            self._make_meta(site_lat=38.5631, site_lon=-8.88149),
            self._make_meta(
                filename_stem="Light_M81_180.0s_Bin1_0C_20260219_L-Pro_0002",
                site_lat=38.9999, site_lon=-9.9999,  # second frame's value ignored
            ),
        ]
        result = SessionAnalyzer.analyze_sessions(meta_list, lights)
        assert len(result) == 1
        assert result[0]["site_lat"] == pytest.approx(38.5631)
        assert result[0]["site_lon"] == pytest.approx(-8.88149)

    def test_missing_site_coords_are_none(self, tmp_path):
        lights = tmp_path / "M81" / "Lights"
        lights.mkdir(parents=True)
        result = SessionAnalyzer.analyze_sessions([self._make_meta()], lights)
        assert result[0]["site_lat"] is None
        assert result[0]["site_lon"] is None


class TestUpsertSessionSiteCoords:
    def _session(self, **overrides):
        base = {
            "session_id": "M81_20260219_FRA400_ASI585MC_L-Pro",
            "target": "M 81",
            "obs_date": "2026-02-19",
            "ota": "FRA400",
            "camera": "ZWO ASI585MC",
            "filter": "L-Pro",
            "gain": 200,
            "temperature_c": -10.0,
            "exposure_sec": 180.0,
            "focal_length": 400.0,
            "frame_count": 10,
            "total_integration_sec": 1800,
            "ra_deg": None,
            "dec_deg": None,
            "lights_path": "/fake",
            "notes": "",
        }
        base.update(overrides)
        return base

    def _site_lat(self, db, session_id="M81_20260219_FRA400_ASI585MC_L-Pro"):
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT site_lat, site_lon FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row

    def test_session_without_site_keys_upserts_fine(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        upsert_session(db, self._session())  # no site_lat/site_lon keys at all
        assert self._site_lat(db) == (None, None)

    def test_coalesce_preserves_backfilled_value_on_rescan_without_header(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        upsert_session(db, self._session(site_lat=38.5631, site_lon=-8.88149))
        assert self._site_lat(db) == pytest.approx((38.5631, -8.88149))

        # A rescan of frames lacking SITELAT must not NULL out the backfilled value.
        upsert_session(db, self._session(site_lat=None, site_lon=None))
        assert self._site_lat(db) == pytest.approx((38.5631, -8.88149))

    def test_coalesce_lets_a_header_bearing_rescan_win(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        upsert_session(db, self._session(site_lat=38.5631, site_lon=-8.88149))
        upsert_session(db, self._session(site_lat=38.9, site_lon=-9.1))
        assert self._site_lat(db) == pytest.approx((38.9, -9.1))


class TestSiteMigration:
    def test_adds_site_columns_to_post_w3_db_missing_them(self, tmp_path):
        # Current (post-W3, id-PK) schema, minus the two S1 columns — models
        # a real DB from just before this migration was introduced.
        db = tmp_path / "old.db"
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE sessions (
                    id                       INTEGER PRIMARY KEY,
                    session_id               TEXT NOT NULL UNIQUE,
                    target                   TEXT NOT NULL,
                    obs_date                 TEXT NOT NULL,
                    ota                      TEXT,
                    camera                   TEXT,
                    filter                   TEXT,
                    gain                     INTEGER,
                    temperature_c            REAL,
                    exposure_sec             REAL,
                    focal_length             REAL,
                    frame_count              INTEGER,
                    total_integration_sec    INTEGER,
                    ra_deg                   REAL,
                    dec_deg                  REAL,
                    lights_path              TEXT,
                    processed_status         TEXT,
                    processed_state          TEXT NOT NULL DEFAULT 'unprocessed',
                    processed_path           TEXT,
                    processed_date           TEXT,
                    notes                    TEXT,
                    created_at               TEXT,
                    updated_at               TEXT
                );
                CREATE TABLE calibration_sets (set_id TEXT PRIMARY KEY, frame_type TEXT NOT NULL);
            """)
        init_db(db)
        with sqlite3.connect(db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert {"site_lat", "site_lon"} <= cols


class TestSitesHomeInvariant:
    def test_partial_unique_index_rejects_second_home(self, tmp_path):
        db = tmp_path / "test.db"
        init_db(db)
        conn = sqlite3.connect(db)
        add_site(conn, name="Home", lat=38.563, lon=-8.881, is_home=True)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sites (name, lat, lon, is_home) VALUES (?, ?, ?, ?)",
                ("Other", 38.6, -8.9, 1),
            )
