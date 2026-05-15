import pytest
from unittest.mock import patch
import tempfile
from pathlib import Path
from darkroom.scanner import scan_source, Session, CalibrationGroup, ScanResult


# Reusable metadata template for a single light frame
def light_meta(filename_stem: str, date_obs: str = "2026-02-19T22:00:00") -> dict:
    return {
        "filename_stem": filename_stem,
        "file_path": "",
        "date_obs": date_obs,
        "exposure": 180.0,
        "camera": "ZWO ASI585MC Pro",
        "gain": 200,
        "temperature": -20.0,
        "object": "M 81",
        "filter_header": None,
        "imagetyp": "Light Frame",
        "focallen": 400,
        "ra_deg": 148.888,
        "dec_deg": 69.065,
    }


def test_scan_source_single_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        f1 = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001.fit"
        f2 = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220300_-20.0C_L-Pro_0002.fit"
        f1.touch()
        f2.touch()

        def mock_extract(path):
            return {**light_meta(path.stem), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert isinstance(result, ScanResult)
    assert len(result.sessions) == 1
    s = result.sessions[0]
    assert s.target == "M 81"
    assert s.obs_date == "2026-02-19"
    assert s.filter == "L-Pro"
    assert s.ota == "FRA400"
    assert s.camera == "ZWO ASI585MC Pro"
    assert s.gain == 200
    assert s.temperature_c == -20.0
    assert s.exposure_sec == 180.0
    assert s.ra_deg == pytest.approx(148.888)
    assert len(s.files) == 2


def test_scan_source_two_nights_same_target():
    # Frames on two different imaging nights produce two sessions
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 101"
        light_dir.mkdir(parents=True)
        f1 = light_dir / "Light_M 101_180.0s_Bin1_585MC_gain200_20260222-220000_-20.0C_L-Pro_0001.fit"
        f2 = light_dir / "Light_M 101_180.0s_Bin1_585MC_gain200_20260225-220000_-20.0C_L-Pro_0001.fit"
        f1.touch()
        f2.touch()

        def mock_extract(path):
            date_obs = "2026-02-22T22:00:00" if "20260222" in path.name else "2026-02-25T22:00:00"
            meta = {**light_meta(path.stem, date_obs), "file_path": str(path), "object": "M 101"}
            return meta

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.sessions) == 2
    dates = {s.obs_date for s in result.sessions}
    assert dates == {"2026-02-22", "2026-02-25"}


def test_scan_source_no_filter_in_filename():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 51"
        light_dir.mkdir(parents=True)
        f = light_dir / "Light_M 51_300.0s_Bin1_585MC_gain200_20260228-220000_-20.0C_0001.fit"
        f.touch()

        def mock_extract(path):
            return {**light_meta(path.stem, "2026-02-28T22:00:00"), "file_path": str(path), "object": "M 51", "exposure": 300.0}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.sessions) == 1
    assert result.sessions[0].filter is None


def test_scan_source_thumbnails_excluded():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        fit = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001.fit"
        thn = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001_thn.jpg"
        fit.touch()
        thn.touch()

        def mock_extract(path):
            return {**light_meta(path.stem), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.sessions[0].files) == 1
    assert result.sessions[0].files[0].suffix == ".fit"


def test_scan_source_empty_source():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = scan_source(Path(tmpdir))
    assert result.sessions == []
    assert result.calibration == []
