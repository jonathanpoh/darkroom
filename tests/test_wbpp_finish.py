import pytest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from darkroom.cataloger import init_db, upsert_calibration_set, upsert_session
from darkroom.finish import (
    _find_processing_date, _build_dest, _copy_flat,
    _list_session_dirs, _confirm_and_delete, _resolve_session_ids,
)
from darkroom.prep import _build_night


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_find_processing_date_returns_today(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir()
    touch(master / "masterLight_BIN-1_3840x2160_FILTER-L-Extreme_RGB.xisf")
    result = _find_processing_date(master, processed, None)
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
    result = _find_processing_date(master, processed, None)
    assert result == date.today().isoformat()


def test_find_processing_date_override(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir()
    touch(master / "masterLight.xisf")
    assert _find_processing_date(master, processed, "2025-12-31") == "2025-12-31"


def test_find_processing_date_no_files_exits(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir(); processed.mkdir()
    with pytest.raises(SystemExit):
        _find_processing_date(master, processed, None)


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
    result = _list_session_dirs(tmp_path)
    names = {p.name for p in result}
    assert names == {"SESSION_1", "SESSION_2"}


def test_list_session_dirs_empty(tmp_path):
    (tmp_path / "Output").mkdir()
    result = _list_session_dirs(tmp_path)
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
        "processed_status": "", "notes": "",
    })

    lights_dir = archive / lights_rel
    light = touch(lights_dir / "Light_M81_180.0s_FRA400_L-Pro_20260219-230000_-20C_0001.fit")

    wbpp_target = tmp_path / "WBPP" / "M81"
    link_dir = wbpp_target / "SESSION_1" / "Lights" / "FILTER_L-Pro"
    link_dir.mkdir(parents=True)
    (link_dir / light.name).symlink_to(light.resolve())

    assert _resolve_session_ids(wbpp_target, catalog, archive) == [sid]


def test_resolve_session_ids_no_match_returns_empty(tmp_path):
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)
    wbpp_target = tmp_path / "WBPP" / "M81"
    link_dir = wbpp_target / "SESSION_1" / "Lights" / "FILTER_L-Pro"
    link_dir.mkdir(parents=True)
    stray = touch(archive / "elsewhere" / "x.fit")
    (link_dir / "x.fit").symlink_to(stray.resolve())
    assert _resolve_session_ids(wbpp_target, catalog, archive) == []


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
    _build_night([session], output=archive, catalog=catalog,
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
    _build_night([session], output=archive, catalog=catalog,
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
    _build_night([session], output=archive, catalog=catalog,
                 session_dir=session_dir, flat_window=3)

    bias_links = list((session_dir / "Bias").glob("*"))
    assert len(bias_links) == 1
    assert bias_links[0].resolve().name == "masterBias_gain200.xisf"
