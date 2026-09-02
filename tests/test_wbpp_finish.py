import argparse
import pytest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import sqlite3

from darkroom.catalog_client import LocalBackend
from darkroom.cataloger import init_db, upsert_calibration_set, upsert_session
from darkroom.finish import (
    _find_processing_date, _build_dest, _copy_flat, _confirm_and_delete,
    _lights_index, _resolve_session_ids, _mark_sessions, _panel_dirs, cmd_finish,
)
from darkroom.names import wbpp_panel_dir
from darkroom.wbpp import session_dirs
from darkroom.prep import _build_night, _no_darks_note, add_subparser as prep_add_subparser


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_find_processing_date_returns_today(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir()
    touch(master / "masterLight_BIN-1_3840x2160_FILTER-L-Extreme_RGB.xisf")
    result = _find_processing_date([master, processed], None)
    assert result == date.today().isoformat()


def test_find_processing_date_prefers_processed(tmp_path):
    import os, time
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir(); processed.mkdir()
    older = master / "masterLight.xisf"
    newer = processed / "final.xisf"
    touch(older); touch(newer)
    # Make master file 2 days older than processed
    past = time.time() - 2 * 86400
    os.utime(older, (past, past))
    result = _find_processing_date([master, processed], None)
    assert result == date.today().isoformat()


def test_find_processing_date_override(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir()
    touch(master / "masterLight.xisf")
    assert _find_processing_date([master, processed], "2025-12-31") == "2025-12-31"


def test_find_processing_date_no_files_exits(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir(); processed.mkdir()
    with pytest.raises(SystemExit):
        _find_processing_date([master, processed], None)


def test_find_processing_date_scans_every_dir_in_list(tmp_path):
    """Regression for M3: a mosaic passes every panel's dirs plus the
    target-level processed/ — the latest mtime across ALL of them wins, not
    just the first two."""
    import os, time
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.mkdir(); b.mkdir(); c.mkdir()
    old1 = touch(a / "x.xisf")
    old2 = touch(b / "y.xisf")
    newest = touch(c / "z.xisf")
    past = time.time() - 2 * 86400
    os.utime(old1, (past, past))
    os.utime(old2, (past, past))
    result = _find_processing_date([a, b, c], None)
    assert result == date.today().isoformat()
    assert newest.stat().st_mtime > old1.stat().st_mtime


def test_build_dest(tmp_path):
    dest = _build_dest(tmp_path, "M 81", "2026-05-15")
    assert dest == tmp_path / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-15"


def test_build_dest_target_with_spaces(tmp_path):
    dest = _build_dest(tmp_path, "NGC 1499", "2026-03-01")
    assert dest == tmp_path / "01_Deep Sky Objects" / "NGC 1499" / "_Processed" / "2026-03-01"


def test_copy_flat_copies_files(tmp_path):
    src = tmp_path / "master"
    src.mkdir()
    touch(src / "masterLight.xisf")
    touch(src / "masterDark.xisf")
    dest = tmp_path / "dest" / "master"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 2
    assert (dest / "masterLight.xisf").exists()
    assert (dest / "masterDark.xisf").exists()


def test_copy_flat_skips_existing(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir(); dest.mkdir()
    touch(src / "file.xisf")
    touch(dest / "file.xisf")
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 0


def test_copy_flat_empty_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 0
    assert not dest.exists()


def test_copy_flat_dry_run_does_not_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    touch(src / "file.xisf")
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=True)
    assert count == 1
    assert not dest.exists()


def test_copy_flat_ignores_subdirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "subdir").mkdir()
    touch(src / "file.xisf")
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 1
    assert not (dest / "subdir").exists()


def test_list_session_dirs_returns_only_session_dirs(tmp_path):
    (tmp_path / "SESSION_1").mkdir()
    (tmp_path / "SESSION_2").mkdir()
    (tmp_path / "Output").mkdir()        # should NOT appear
    result = session_dirs(tmp_path)
    names = {p.name for p in result}
    assert names == {"SESSION_1", "SESSION_2"}


def test_list_session_dirs_empty(tmp_path):
    (tmp_path / "Output").mkdir()
    result = session_dirs(tmp_path)
    assert result == []


def test_confirm_and_delete_dry_run_does_not_delete(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    _confirm_and_delete([d], "Intermediates", dry_run=True)
    assert d.exists()


def test_confirm_and_delete_yes_deletes(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    with patch("builtins.input", return_value="yes"):
        _confirm_and_delete([d], "Intermediates", dry_run=False)
    assert not d.exists()


def test_confirm_and_delete_no_skips(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    with patch("builtins.input", return_value=""):
        _confirm_and_delete([d], "Intermediates", dry_run=False)
    assert d.exists()


def test_confirm_and_delete_empty_list(tmp_path):
    _confirm_and_delete([], "Intermediates", dry_run=False)  # should not raise


# ── B1: finish resolves sessions under the Lights/<filter>/ layout ─────────────

def test_resolve_session_ids_filter_subdir_layout(tmp_path):
    """Regression for B1: lights_path now carries a Lights/<filter>/ subdir.

    finish must still resolve the session_id by matching each symlink's resolved
    archive directory against the catalog's stored lights_path — not by walking
    a fixed number of .parent levels.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(catalog, {
        "session_id": sid, "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 1, "total_integration_sec": 180,
        "ra_deg": None, "dec_deg": None, "lights_path": lights_rel,
        "notes": "",
    })

    lights_dir = archive / lights_rel
    light = touch(lights_dir / "Light_M81_180.0s_FRA400_L-Pro_20260219-230000_-20C_0001.fit")

    wbpp_target = tmp_path / "WBPP" / "M81"
    link_dir = wbpp_target / "SESSION_1" / "Lights" / "FILTER_L-Pro"
    link_dir.mkdir(parents=True)
    (link_dir / light.name).symlink_to(light.resolve())

    index = _lights_index(LocalBackend(catalog), archive)
    assert _resolve_session_ids([wbpp_target], index) == [sid]


# ── W1: finish marks structured processed_state ────────────────────────────

def test_mark_sessions_processed_sets_structured_state(tmp_path):
    """finish's _mark_sessions must set processed_state='processed'
    with processed_path/processed_date, not the legacy processed_status."""
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(catalog, {
        "session_id": sid, "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 1, "total_integration_sec": 180,
        "ra_deg": None, "dec_deg": None, "lights_path": lights_rel,
        "notes": "",
    })

    lights_dir = archive / lights_rel
    light = touch(lights_dir / "Light_M81_180.0s_FRA400_L-Pro_20260219-230000_-20C_0001.fit")

    wbpp_target = tmp_path / "WBPP" / "M81"
    link_dir = wbpp_target / "SESSION_1" / "Lights" / "FILTER_L-Pro"
    link_dir.mkdir(parents=True)
    (link_dir / light.name).symlink_to(light.resolve())

    status = "01_Deep Sky Objects/M 81/_Processed/2026-05-15"
    backend = LocalBackend(catalog)
    session_ids = _resolve_session_ids([wbpp_target], _lights_index(backend, archive))
    _mark_sessions(session_ids, backend, status, "2026-05-15", state="processed", dry_run=False)

    with sqlite3.connect(catalog) as conn:
        row = conn.execute(
            "SELECT processed_state, processed_path, processed_date, processed_status "
            "FROM sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
    assert row == ("processed", status, "2026-05-15", None)


def test_resolve_session_ids_no_match_returns_empty(tmp_path):
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)
    wbpp_target = tmp_path / "WBPP" / "M81"
    link_dir = wbpp_target / "SESSION_1" / "Lights" / "FILTER_L-Pro"
    link_dir.mkdir(parents=True)
    stray = touch(archive / "elsewhere" / "x.fit")
    (link_dir / "x.fit").symlink_to(stray.resolve())
    index = _lights_index(LocalBackend(catalog), archive)
    assert _resolve_session_ids([wbpp_target], index) == []


# ── B2: flat darks captured the morning after the flats ───────────────────────

def test_build_night_symlinks_flat_darks_dated_next_morning(tmp_path):
    """Regression for B2: flat darks captured on flat_date+1 must be symlinked.

    find_flat_darks accepts flat_date or flat_date+1, but prep previously filtered
    the files by the flat's own date, dropping the +1 set silently.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"
    flat_date = "2026-02-19"
    flatdark_date = "2026-02-20"  # captured the following morning

    flats_rel = "00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-19"
    flat = touch(archive / flats_rel / "Flat_L-Pro_2.0s_20260219-080000_-20C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "flat1", "frame_type": "Flat", "camera": cam, "ota": "FRA400",
        "filter": "L-Pro", "gain": 200, "exposure_sec": 2.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": flat_date, "folder_path": flats_rel,
        "is_master": 0,
    })

    fd_rel = "00_Calibration/FlatDarks/ZWOASI585MCPro"
    touch(archive / fd_rel / f"FlatDark_2.0s_20260220-090000_-20C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "fd1", "frame_type": "FlatDark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 2.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": flatdark_date, "folder_path": fd_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": flat_date, "frame_count": 1,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    fd_links = list((session_dir / "FlatDarks").glob("*"))
    assert len(fd_links) == 1
    assert fd_links[0].is_symlink()


# ── B5: wbpp prefers master calibration files over raw subs ───────────────────

def test_build_night_prefers_master_dark_over_raw_subs(tmp_path):
    """Regression for B5: when both a master dark and raw sub-frames match the
    same camera/gain/exposure, only the master should be symlinked into Darks/
    — not a mix of both.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    master_rel = "00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_-20C.xisf"
    touch(archive / master_rel)
    upsert_calibration_set(catalog, {
        "set_id": "dark_master", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": master_rel,
        "is_master": 1,
    })

    raw_rel = "00_Calibration/Darks/ZWOASI585MCPro/Raw/2026-02-19"
    touch(archive / raw_rel / "Dark_180.0s_Bin1_585MC_gain200_20260219-090000_-20.0C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "dark_raw", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1
    assert dark_links[0].resolve().name == "masterDark_180s_gain200_-20C.xisf"


def test_build_night_prefers_master_bias_over_raw_subs(tmp_path):
    """Regression for B5 (Bias half — same bug, separate loop in prep.py)."""
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    master_rel = "00_Calibration/Bias/ZWOASI585MCPro/Masters/masterBias_gain200.xisf"
    touch(archive / master_rel)
    upsert_calibration_set(catalog, {
        "set_id": "bias_master", "frame_type": "Bias", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": None, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": master_rel,
        "is_master": 1,
    })

    raw_rel = "00_Calibration/Bias/ZWOASI585MCPro/Raw/2026-02-19"
    touch(archive / raw_rel / "Bias_0.001s_Bin1_585MC_gain200_20260219-090000_-20.0C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "bias_raw", "frame_type": "Bias", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": None, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    bias_links = list((session_dir / "Bias").glob("*"))
    assert len(bias_links) == 1
    assert bias_links[0].resolve().name == "masterBias_gain200.xisf"


# ── B11: wbpp symlinks only the nearest-temperature dark master ───────────────

def _register_dark_master(catalog, *, set_id, cam, temperature_c, folder_path):
    """Shared helper for the B11 tests below — one master dark set at a given temp."""
    upsert_calibration_set(catalog, {
        "set_id": set_id, "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": temperature_c,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": folder_path,
        "is_master": 1,
    })


def test_build_night_symlinks_only_the_nearest_dark_master(tmp_path):
    """Regression for B11: _build_night used to symlink every matching dark
    master regardless of temperature. Only the single nearest match (by
    session temperature_c) should land in Darks/.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    for temp in (15.0, 20.0, 25.0):
        rel = f"00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_{temp:g}C.xisf"
        touch(archive / rel)
        _register_dark_master(catalog, set_id=f"dark_{temp:g}", cam=cam,
                               temperature_c=temp, folder_path=rel)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": 22.0,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1
    assert dark_links[0].resolve().name == "masterDark_180s_gain200_20C.xisf"


def test_build_night_skips_dark_masters_outside_tolerance(tmp_path):
    """Regression for B11: with no master within tolerance of the session
    temperature, Darks/ must end up empty rather than getting every master
    handed to WBPP as a fallback.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    for temp in (15.0, 20.0):
        rel = f"00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_{temp:g}C.xisf"
        touch(archive / rel)
        _register_dark_master(catalog, set_id=f"dark_{temp:g}", cam=cam,
                               temperature_c=temp, folder_path=rel)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": -20.0,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    darks_dir = session_dir / "Darks"
    dark_links = list(darks_dir.glob("*")) if darks_dir.exists() else []
    assert dark_links == []


def test_build_night_warns_when_session_sits_between_dark_masters(tmp_path, capsys):
    """Regression for B11: when a session sits exactly equidistant between two
    dark masters at different temperatures, _build_night must warn (naming
    both candidates) rather than silently picking one via backend row order.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    for temp in (20.0, 25.0):
        rel = f"00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_{temp:g}C.xisf"
        touch(archive / rel)
        _register_dark_master(catalog, set_id=f"dark_{temp:g}", cam=cam,
                               temperature_c=temp, folder_path=rel)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": 22.5,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    out = capsys.readouterr().out
    warning = next((ln for ln in out.splitlines() if "WARNING" in ln), None)
    assert warning is not None, out
    # Match the formatted temperatures, not bare "20"/"25" — those also occur in
    # "gain200", "180.0s" and the 2026 obs_date, so a substring check on the
    # whole output would pass even if the warning named no temperatures at all.
    assert "20C" in warning and "25C" in warning, warning
    assert "22.5C" in warning, warning

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1


def test_build_night_dark_temp_tolerance_is_configurable(tmp_path):
    """Regression for B11: --dark-temp-tolerance must actually widen (or
    narrow) which dark masters are eligible, not just get accepted and
    ignored.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    for temp in (15.0, 30.0):
        rel = f"00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_{temp:g}C.xisf"
        touch(archive / rel)
        _register_dark_master(catalog, set_id=f"dark_{temp:g}", cam=cam,
                               temperature_c=temp, folder_path=rel)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": 20.0,
    }

    backend = LocalBackend(catalog)

    session_dir_default = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=backend,
                 session_dir=session_dir_default, flat_window=3)
    darks_dir_default = session_dir_default / "Darks"
    dark_links_default = list(darks_dir_default.glob("*")) if darks_dir_default.exists() else []
    assert dark_links_default == []

    session_dir_wide = tmp_path / "WBPP" / "M81" / "SESSION_2"
    _build_night([session], output=archive, backend=backend,
                 session_dir=session_dir_wide, flat_window=3, dark_temp_tolerance=5.0)
    dark_links_wide = list((session_dir_wide / "Darks").glob("*"))
    assert len(dark_links_wide) == 1
    assert dark_links_wide[0].resolve().name == "masterDark_180s_gain200_15C.xisf"


# ── B11 review fixes: raw-subs temperature filtering & missing-file fallback ──

def test_build_night_raw_subs_fallback_is_temperature_filtered(tmp_path):
    """Regression: with no master dark rows, the raw-subs fallback used to scan
    by exposure only, leaking raws at other temperatures back in because raw
    dark sets at different temperatures share one Darks/<Camera>/ folder.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    raw_rel = "00_Calibration/Darks/ZWOASI585MCPro/Raw/2026-02-19"
    cold_file = touch(
        archive / raw_rel / "Dark_180.0s_Bin1_585MC_gain200_20260219-090000_-20.0C_0001.fit"
    )
    touch(archive / raw_rel / "Dark_180.0s_Bin1_585MC_gain200_20260219-090100_-10.0C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "dark_raw", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": -20.0,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1
    assert dark_links[0].resolve().name == cold_file.name


def test_build_night_falls_through_to_next_master_when_nearest_missing(tmp_path):
    """Regression: a stale folder_path on the nearest dark master row (file
    missing on disk) used to leave Darks/ empty. It should fall through the
    ranking to the next-nearest master that's actually present.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    missing_rel = "00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_-20C.xisf"
    # Do NOT touch missing_rel — simulates a stale folder_path with no file on disk.
    _register_dark_master(catalog, set_id="dark_-20", cam=cam,
                           temperature_c=-20.0, folder_path=missing_rel)

    present_rel = "00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_-18C.xisf"
    touch(archive / present_rel)
    _register_dark_master(catalog, set_id="dark_-18", cam=cam,
                           temperature_c=-18.0, folder_path=present_rel)

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": -20.0,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1
    assert dark_links[0].resolve().name == "masterDark_180s_gain200_-18C.xisf"


def test_build_night_falls_through_to_raw_subs_when_all_masters_missing(tmp_path):
    """Regression: when every matched master's file is missing on disk, fall
    all the way through to the raw-subs fallback rather than leaving Darks/
    empty.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    missing_rel = "00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_-20C.xisf"
    # Do NOT touch missing_rel — file missing on disk.
    _register_dark_master(catalog, set_id="dark_-20", cam=cam,
                           temperature_c=-20.0, folder_path=missing_rel)

    raw_rel = "00_Calibration/Darks/ZWOASI585MCPro/Raw/2026-02-19"
    raw_file = touch(
        archive / raw_rel / "Dark_180.0s_Bin1_585MC_gain200_20260219-090000_-20.0C_0001.fit"
    )
    upsert_calibration_set(catalog, {
        "set_id": "dark_raw", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
        "temperature_c": -20.0,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, backend=LocalBackend(catalog),
                 session_dir=session_dir, flat_window=3)

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1
    assert dark_links[0].resolve().name == raw_file.name


def test_no_darks_note_suggests_tolerance_for_raw_only_near_miss(tmp_path):
    """Regression: _no_darks_note used to say "no darks found at this
    gain/exposure" even when a raw (non-master) dark set existed at that
    gain/exposure just outside tolerance — it only looked at master rows'
    framing implicitly via find_darks, but the message text didn't distinguish
    a near-miss from a true absence. With matched_any=False and a raw set
    outside tolerance, the note must point at --dark-temp-tolerance.
    """
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    raw_rel = "00_Calibration/Darks/ZWOASI585MCPro/Raw/2026-02-19"
    upsert_calibration_set(catalog, {
        "set_id": "dark_raw", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -10.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    backend = LocalBackend(catalog)
    s0 = {"camera": cam, "gain": 200, "exposure_sec": 180.0}
    note = _no_darks_note(
        backend, s0, session_temp=-20.0, tolerance=3.0, matched_any=False,
    )

    assert "nearest raw set is" in note
    assert "--dark-temp-tolerance" in note
    assert "no darks found at this gain/exposure" not in note


def test_dark_temp_tolerance_rejects_negative(capsys):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    prep_add_subparser(sub)
    with pytest.raises(SystemExit):
        parser.parse_args(["wbpp", "--dark-temp-tolerance", "-5"])
    assert "non-negative" in capsys.readouterr().err


# ── dry-run previews session resolution ─────────────────────────────────────

def _dry_run_finish_setup(tmp_path, *, with_symlink):
    """Archive + catalog + WBPP target with Output/master ready for cmd_finish."""
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)
    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(catalog, {
        "session_id": sid, "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 1, "total_integration_sec": 180,
        "ra_deg": None, "dec_deg": None, "lights_path": lights_rel, "notes": "",
    })
    light = touch(archive / lights_rel / "Light_M81_0001.fit")
    wbpp_target = tmp_path / "WBPP" / "M81"
    touch(wbpp_target / "Output" / "master" / "masterLight.xisf")
    if with_symlink:
        link_dir = wbpp_target / "SESSION_1" / "Lights" / "FILTER_L-Pro"
        link_dir.mkdir(parents=True)
        (link_dir / light.name).symlink_to(light.resolve())
    return archive, catalog, tmp_path / "WBPP", sid


def test_cmd_finish_dry_run_previews_sessions_to_mark(tmp_path, capsys):
    """--dry-run must show which sessions a real run would mark, and not mark them."""
    archive, catalog, wbpp_root, sid = _dry_run_finish_setup(tmp_path, with_symlink=True)
    cmd_finish(output=archive, wbpp_root=wbpp_root, target="M 81",
               backend=LocalBackend(catalog), date_override="2026-07-01", dry_run=True)
    out = capsys.readouterr().out
    assert "would mark 1 session(s)" in out
    assert sid in out
    row = LocalBackend(catalog).query_sessions(session_id=sid)[0]
    assert row["processed_state"] == "unprocessed"


def test_cmd_finish_dry_run_warns_when_no_sessions_match(tmp_path, capsys):
    """--dry-run must warn loudly when the SESSION_N symlinks resolve nowhere."""
    archive, catalog, wbpp_root, _ = _dry_run_finish_setup(tmp_path, with_symlink=False)
    cmd_finish(output=archive, wbpp_root=wbpp_root, target="M 81",
               backend=LocalBackend(catalog), date_override="2026-07-01", dry_run=True)
    out = capsys.readouterr().out
    assert "WARNING: no catalog sessions matched" in out


def test_cmd_finish_copies_log_folders(tmp_path, capsys):
    """Output/logs and Output/asiair_logs are archived alongside master/processed."""
    archive, catalog, wbpp_root, _ = _dry_run_finish_setup(tmp_path, with_symlink=True)
    out_dir = wbpp_root / "M81" / "Output"
    touch(out_dir / "logs" / "20260705173607.log")
    touch(out_dir / "asiair_logs" / "PHD2_GuideLog_2026-06-16_233946.txt")
    touch(out_dir / "registered" / "not_copied.xisf")
    with patch("builtins.input", return_value=""):
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="M 81",
                   backend=LocalBackend(catalog), date_override="2026-07-01", dry_run=False)
    dest = archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-07-01"
    assert (dest / "logs" / "20260705173607.log").exists()
    assert (dest / "asiair_logs" / "PHD2_GuideLog_2026-06-16_233946.txt").exists()
    assert not (dest / "registered").exists()


def test_cmd_finish_rerun_is_idempotent(tmp_path, capsys):
    """A second run must not abort just because master/ was already archived
    (regression: the empty-master guard counted new copies, not source files),
    and must pick up log folders added between runs."""
    archive, catalog, wbpp_root, _ = _dry_run_finish_setup(tmp_path, with_symlink=True)
    out_dir = wbpp_root / "M81" / "Output"
    with patch("builtins.input", return_value=""):
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="M 81",
                   backend=LocalBackend(catalog), date_override="2026-07-01", dry_run=False)
        touch(out_dir / "logs" / "20260705173607.log")
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="M 81",
                   backend=LocalBackend(catalog), date_override="2026-07-01", dry_run=False)
    dest = archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-07-01"
    assert (dest / "logs" / "20260705173607.log").exists()


# ── M3: mosaic finish — panel dirs finish separately, the merge finishes the target ─

def _mosaic_session(catalog, archive, *, panel, filter_="L-Pro"):
    """Register one catalog session + its light frame on disk. Returns (session_id, light_path)."""
    lights_rel = (
        f"01_Deep Sky Objects/IC 4604/2026-05-26_FRA400_ZWOASI585MCPro/Lights/{filter_}/P{panel}"
    )
    sid = f"IC4604_20260526_FRA400_ZWOASI585MCPro_{filter_}_P{panel}"
    upsert_session(catalog, {
        "session_id": sid, "target": "IC 4604", "obs_date": "2026-05-26",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": filter_, "panel": panel,
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 1, "total_integration_sec": 180,
        "ra_deg": None, "dec_deg": None, "lights_path": lights_rel, "notes": "",
    })
    light = touch(archive / lights_rel / f"Light_IC4604_P{panel}_0001.fit")
    return sid, light


def _mosaic_finish_setup(tmp_path, *, panels=("1-1", "1-2"), with_merge):
    """Two-panel (default) mosaic WBPP tree + matching catalog rows, ready for cmd_finish.

    Each panel gets its own Output/master/ (so _finish_panel has something to
    copy) and its own SESSION_1/Lights/.../ symlink resolving to that panel's
    catalog session. with_merge=True additionally populates the target-level
    Output/processed/ with the hand-merged mosaic file.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    wbpp_root = tmp_path / "WBPP"
    wbpp_target = wbpp_root / "IC4604"
    sids = {}
    for panel in panels:
        sid, light = _mosaic_session(catalog, archive, panel=panel)
        sids[panel] = sid
        panel_dir = wbpp_target / wbpp_panel_dir(panel)
        touch(panel_dir / "Output" / "master" / f"masterLight_P{panel}.xisf")
        link_dir = panel_dir / "SESSION_1" / "Lights" / "FILTER_L-Pro"
        link_dir.mkdir(parents=True)
        (link_dir / light.name).symlink_to(light.resolve())

    if with_merge:
        touch(wbpp_target / "Output" / "processed" / "mosaic_merged.xisf")

    return archive, catalog, wbpp_root, sids


def test_cmd_finish_mosaic_panels_only_marks_in_progress(tmp_path, capsys):
    """No target-level merge yet: each panel's master/ lands under its own
    _Processed/<date>/<panel>/, sessions go in_progress, and finish says the
    merge is still outstanding rather than treating it as an error."""
    archive, catalog, wbpp_root, sids = _mosaic_finish_setup(tmp_path, with_merge=False)
    backend = LocalBackend(catalog)
    with patch("builtins.input", return_value=""):
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="IC 4604",
                   backend=backend, date_override="2026-05-29", dry_run=False)

    dest = archive / "01_Deep Sky Objects" / "IC 4604" / "_Processed" / "2026-05-29"
    assert (dest / "1-1" / "master" / "masterLight_P1-1.xisf").exists()
    assert (dest / "1-2" / "master" / "masterLight_P1-2.xisf").exists()
    assert not (dest / "processed").exists()  # no merge yet -> no top-level deliverable

    for panel, sid in sids.items():
        row = backend.query_sessions(session_id=sid)[0]
        assert row["processed_state"] == "in_progress", panel

    out = capsys.readouterr().out
    assert "Merged mosaic not found" in out
    assert "normal state" in out


def test_cmd_finish_mosaic_merge_marks_all_panels_processed(tmp_path):
    """A populated target-level Output/processed/ is the merged deliverable:
    it lands at _Processed/<date>/ top level and flips every panel's sessions
    to processed together, since none is individually finished."""
    archive, catalog, wbpp_root, sids = _mosaic_finish_setup(tmp_path, with_merge=True)
    backend = LocalBackend(catalog)
    with patch("builtins.input", return_value=""):
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="IC 4604",
                   backend=backend, date_override="2026-05-29", dry_run=False)

    dest = archive / "01_Deep Sky Objects" / "IC 4604" / "_Processed" / "2026-05-29"
    assert (dest / "processed" / "mosaic_merged.xisf").exists()
    assert (dest / "1-1" / "master" / "masterLight_P1-1.xisf").exists()

    for panel, sid in sids.items():
        row = backend.query_sessions(session_id=sid)[0]
        assert row["processed_state"] == "processed", panel
        assert row["processed_path"] == str(dest.relative_to(archive))


def test_cmd_finish_panel_flag_touches_only_that_panel(tmp_path):
    """--panel 1-1 finishes exactly one panel: the other panel's dir and
    session are left untouched."""
    archive, catalog, wbpp_root, sids = _mosaic_finish_setup(tmp_path, with_merge=False)
    backend = LocalBackend(catalog)
    with patch("builtins.input", return_value=""):
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="IC 4604",
                   backend=backend, date_override="2026-05-29", dry_run=False, panel="1-1")

    dest = archive / "01_Deep Sky Objects" / "IC 4604" / "_Processed" / "2026-05-29"
    assert (dest / "1-1" / "master" / "masterLight_P1-1.xisf").exists()
    assert not (dest / "1-2").exists()

    row_11 = backend.query_sessions(session_id=sids["1-1"])[0]
    assert row_11["processed_state"] == "in_progress"
    row_12 = backend.query_sessions(session_id=sids["1-2"])[0]
    assert row_12["processed_state"] == "unprocessed"

    # The untouched panel's WBPP dir must survive too.
    assert (wbpp_root / "IC4604" / wbpp_panel_dir("1-2") / "Output" / "master").exists()


def test_cmd_finish_panel_flag_unknown_panel_exits(tmp_path):
    archive, catalog, wbpp_root, _ = _mosaic_finish_setup(tmp_path, with_merge=False)
    with pytest.raises(SystemExit):
        cmd_finish(output=archive, wbpp_root=wbpp_root, target="IC 4604",
                   backend=LocalBackend(catalog), date_override="2026-05-29",
                   dry_run=False, panel="9-9")


def test_cmd_finish_mosaic_dry_run_writes_nothing(tmp_path):
    """--dry-run on a mosaic must copy no files and change no catalog state,
    for panels and the merge alike."""
    archive, catalog, wbpp_root, sids = _mosaic_finish_setup(tmp_path, with_merge=True)
    backend = LocalBackend(catalog)
    cmd_finish(output=archive, wbpp_root=wbpp_root, target="IC 4604",
               backend=backend, date_override="2026-05-29", dry_run=True)

    dest = archive / "01_Deep Sky Objects" / "IC 4604" / "_Processed" / "2026-05-29"
    assert not dest.exists()
    for panel, sid in sids.items():
        row = backend.query_sessions(session_id=sid)[0]
        assert row["processed_state"] == "unprocessed", panel

    # WBPP working trees are untouched too.
    assert (wbpp_root / "IC4604" / wbpp_panel_dir("1-1") / "SESSION_1").exists()


def test_panel_dirs_empty_for_non_mosaic_target(tmp_path):
    """_panel_dirs must return {} for an ordinary single-pointing target so
    cmd_finish takes the non-mosaic path — no PANEL_* dir anywhere."""
    target_dir = tmp_path / "M81"
    (target_dir / "SESSION_1").mkdir(parents=True)
    (target_dir / "Output").mkdir()
    assert _panel_dirs(target_dir) == {}


def test_panel_dirs_finds_panel_subdirs(tmp_path):
    target_dir = tmp_path / "IC4604"
    (target_dir / wbpp_panel_dir("1-1")).mkdir(parents=True)
    (target_dir / wbpp_panel_dir("2-10")).mkdir(parents=True)
    result = _panel_dirs(target_dir)
    assert set(result) == {"1-1", "2-10"}
    assert result["1-1"] == target_dir / wbpp_panel_dir("1-1")
