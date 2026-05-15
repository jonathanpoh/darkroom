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
