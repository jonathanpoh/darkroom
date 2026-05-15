import os
import tempfile
import tomllib
from pathlib import Path
import pytest


# Import functions that will exist after implementation
from archive_ingest import (
    camera_slug,
    session_dest_rel,
    cal_dest_rel,
    load_config,
    resolve_path,
)


def test_camera_slug():
    assert camera_slug("ZWO ASI585MC Pro") == "ZWOASI585MCPro"
    assert camera_slug("Canon6D") == "Canon6D"


def test_session_dest_rel():
    result = session_dest_rel("M 81", "2026-02-19", "FRA400", "ZWO ASI585MC Pro", "L-Pro")
    assert result == Path("04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights")


def test_session_dest_rel_no_filter():
    result = session_dest_rel("M 51", "2026-02-28", "FRA400", "ZWO ASI585MC Pro", None)
    assert result == Path("04_Deep Sky Objects/M 51/2026-02-28_FRA400_ZWOASI585MCPro_NoFilter/Lights")


def test_cal_dest_rel_flat():
    result = cal_dest_rel("Flat", "ZWO ASI585MC Pro", "FRA400", "L-Pro", "2026-02-20")
    assert result == Path("00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20")


def test_cal_dest_rel_flat_no_filter():
    result = cal_dest_rel("Flat", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-20")
    assert result == Path("00_Calibration/Flats/FRA400_ZWOASI585MCPro_NoFilter/2026-02-20")


def test_cal_dest_rel_dark():
    result = cal_dest_rel("Dark", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-20")
    assert result == Path("00_Calibration/Darks/ZWOASI585MCPro")


def test_cal_dest_rel_flatdark():
    result = cal_dest_rel("FlatDark", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-21")
    assert result == Path("00_Calibration/FlatDarks/ZWOASI585MCPro")


def test_cal_dest_rel_bias():
    result = cal_dest_rel("Bias", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-21")
    assert result == Path("00_Calibration/Bias/ZWOASI585MCPro/Raw")


def test_load_config_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_config() == {}


def test_load_config_reads_project_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "darkroom.toml").write_text(
        '[darkroom]\noutput_path = "/staging"\ncatalog_path = "/catalog.db"\n'
    )
    config = load_config()
    assert config["darkroom"]["output_path"] == "/staging"


def test_resolve_path_from_cli():
    result = resolve_path("/from/cli", "DARKROOM_OUTPUT", {}, "output_path", "output")
    assert result == Path("/from/cli")


def test_resolve_path_from_env(monkeypatch):
    monkeypatch.setenv("DARKROOM_OUTPUT", "/from/env")
    result = resolve_path(None, "DARKROOM_OUTPUT", {}, "output_path", "output")
    assert result == Path("/from/env")


def test_resolve_path_from_config(monkeypatch):
    monkeypatch.delenv("DARKROOM_OUTPUT", raising=False)
    config = {"darkroom": {"output_path": "/from/config"}}
    result = resolve_path(None, "DARKROOM_OUTPUT", config, "output_path", "output")
    assert result == Path("/from/config")


def test_resolve_path_missing_exits(monkeypatch):
    monkeypatch.delenv("DARKROOM_OUTPUT", raising=False)
    with pytest.raises(SystemExit):
        resolve_path(None, "DARKROOM_OUTPUT", {}, "output_path", "output")


from archive_ingest import resolve_filter, KNOWN_FILTERS


def test_resolve_filter_known():
    # When filter is already detected, return it unchanged
    assert resolve_filter("L-Pro", interactive=False) == ("L-Pro", False)
    assert resolve_filter("L-Extreme", interactive=False) == ("L-Extreme", False)


def test_resolve_filter_non_interactive_unknown():
    # No TTY: return NoFilter with needs_review=True
    result = resolve_filter(None, interactive=False)
    assert result == ("NoFilter", True)


def test_resolve_filter_interactive_chooses_from_list(monkeypatch):
    # Simulate user entering "1" to choose L-Pro
    monkeypatch.setattr("builtins.input", lambda _: "1")
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == KNOWN_FILTERS[0]
    assert needs_review is False


def test_resolve_filter_interactive_empty_input_gives_nofilter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == "NoFilter"
    assert needs_review is False


def test_resolve_filter_interactive_manual_entry(monkeypatch):
    inputs = iter([str(len(KNOWN_FILTERS) + 1), "AstronomikL2"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == "AstronomikL2"
    assert needs_review is False


from archive_ingest import build_session_entry, existing_catalog_sessions, make_cal_set_id
from darkroom.scanner import Session


def _make_session(filter_="L-Pro", n_files=3) -> Session:
    with tempfile.TemporaryDirectory() as tmpdir:
        files = []
        for i in range(n_files):
            f = Path(tmpdir) / f"Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220{i:02d}00_-20.0C_L-Pro_{i+1:04d}.fit"
            f.touch()
            files.append(f)
        return Session(
            target="M 81", obs_date="2026-02-19", ota="FRA400",
            camera="ZWO ASI585MC Pro", filter=filter_, gain=200,
            temperature_c=-20.0, exposure_sec=180.0, ra_deg=148.888,
            dec_deg=69.065, files=files,
        )


def test_build_session_entry_new():
    session = _make_session()
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=False)

    assert entry["session_id"] == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    assert entry["status"] == "new"
    assert entry["needs_review"] is False
    assert entry["filter"] == "L-Pro"
    assert entry["frame_count"] == 3
    assert len(entry["files"]) == 3
    assert all(f["copy"] is True for f in entry["files"])
    assert entry["lights_rel_path"] == "04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights"


def test_build_session_entry_existing_same_count():
    session = _make_session()
    output = Path("/staging")
    catalog = {"M81_20260219_FRA400_ZWOASI585MCPro_L-Pro": 3}
    entry = build_session_entry(session, output, catalog_sessions=catalog, interactive=False)

    assert entry["status"] == "existing"
    assert entry["files"] == []


def test_build_session_entry_no_filter_non_interactive():
    session = _make_session(filter_=None)
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=False)

    assert entry["needs_review"] is True
    assert entry["filter"] is None
    assert "UnknownFilter" in entry["session_id"]


def test_build_session_entry_no_filter_interactive(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "1")  # choose L-Pro
    session = _make_session(filter_=None)
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=True)

    assert entry["needs_review"] is False
    assert entry["filter"] == "L-Pro"
    assert entry["session_id"].endswith("_L-Pro")


def test_existing_catalog_sessions_empty_when_no_db(tmp_path):
    result = existing_catalog_sessions(tmp_path / "nonexistent.db")
    assert result == {}


def test_make_cal_set_id():
    result = make_cal_set_id("Flat", "ZWO ASI585MC Pro", 200, 1.35, -20.0, "2026-02-20")
    assert result == "Flat_ZWOASI585MCPro_1.35s_200g_-20C_2026-02-20"
