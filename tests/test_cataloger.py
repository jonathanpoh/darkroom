import os
import sys
import types
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from darkroom.cataloger import (
    parse_filter,
    parse_ota,
    make_session_id,
    find_lights_folders,
    compute_imaging_night,
    SessionAnalyzer,
    init_db,
    upsert_session,
    upsert_calibration_set,
    mark_processed,
    _find_latest_processed_date,
    mark_processed_by_target,
    finish_command,
)


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

    def test_outside_tolerance_is_unknown(self):
        assert parse_ota(300) == "Unknown"
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
        # M81/2026-02-19_FRA400_ASI585MC_L-Pro/Lights/frame.fit
        lights = tmp_path / "M81" / "2026-02-19_FRA400_ASI585MC_L-Pro" / "Lights"
        lights.mkdir(parents=True)
        (lights / "frame001.fit").touch()
        result = find_lights_folders(tmp_path)
        assert lights in result

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
                "SELECT processed_status FROM sessions WHERE target = 'M 81'"
            ).fetchall()
        assert all(r[0] == "2026-05-15" for r in rows)

    def test_case_insensitive_match(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        count = mark_processed_by_target(db, "m 81", "2026-05-15")
        assert count == 2

    def test_does_not_affect_other_targets(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        mark_processed_by_target(db, "M 81", "2026-05-15")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT processed_status FROM sessions WHERE target = 'NGC 7380'"
            ).fetchone()
        assert row[0] == ""

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
                "frame_count": 10, "total_integration_sec": 1800,
                "ra_deg": None, "dec_deg": None, "lights_path": "/fake",
                "processed_status": "", "notes": "",
            }

        upsert_session(db, row("M81_20260219_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-19", "L-Pro"))
        upsert_session(db, row("M81_20260220_FRA400_ASI585MC_L-Pro", "M 81", "2026-02-20", "L-Pro"))

    def _args(self, **kwargs):
        defaults = {"db": None, "target": "M 81", "output": None, "date": None, "session": None}
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_date_flag_marks_all_sessions_for_target(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        finish_command(self._args(db=str(db), date="2026-05-15"))
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT processed_status FROM sessions WHERE target = 'M 81'"
            ).fetchall()
        assert all(r[0] == "2026-05-15" for r in rows)

    def test_output_flag_detects_date_from_processed_dir(self, tmp_path):
        db = tmp_path / "test.db"
        self._populate(db)
        processed_dir = tmp_path / "04_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-15"
        processed_dir.mkdir(parents=True)
        finish_command(self._args(db=str(db), output=str(tmp_path)))
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT processed_status FROM sessions WHERE target = 'M 81'"
            ).fetchall()
        assert all(r[0] == "2026-05-15" for r in rows)

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
