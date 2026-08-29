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
    assert s.camera == "ZWOASI585MCPro"
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


def dark_meta(filename_stem: str, exposure: float, date_obs: str = "2026-02-20T09:20:00") -> dict:
    return {
        "filename_stem": filename_stem,
        "file_path": "",
        "date_obs": date_obs,
        "exposure": exposure,
        "camera": "ZWO ASI585MC Pro",
        "gain": 200,
        "temperature": -20.0,
        "object": "",
        "filter_header": None,
        "imagetyp": "Dark Frame",
        "focallen": 400,
        "ra_deg": None,
        "dec_deg": None,
    }


def test_scan_calibration_dark_classified():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f = dark_dir / "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001.fit"
        f.touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   return_value={**dark_meta(f.stem, 180.0), "file_path": str(f)}):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert result.calibration[0].frame_type == "Dark"
    assert result.calibration[0].exposure_sec == 180.0
    assert result.calibration[0].capture_date == "2026-02-20"


def test_scan_calibration_flatdark_reclassified():
    # Short darks (< 10s) in the Dark/ folder are reclassified as FlatDark
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f = dark_dir / "Dark_1.35s_Bin1_585MC_gain200_20260220-093000_-20.0C_0001.fit"
        f.touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   return_value={**dark_meta(f.stem, 1.35), "file_path": str(f)}):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert result.calibration[0].frame_type == "FlatDark"


def test_scan_calibration_dark_at_exact_threshold_stays_dark():
    # Boundary: reclassify_flat_dark uses strict "<", so exactly 10.0s stays Dark.
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f = dark_dir / "Dark_10.0s_Bin1_585MC_gain200_20260220-094000_-20.0C_0001.fit"
        f.touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   return_value={**dark_meta(f.stem, 10.0), "file_path": str(f)}):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert result.calibration[0].frame_type == "Dark"


def test_scan_calibration_dark_just_over_threshold_stays_dark():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f = dark_dir / "Dark_10.01s_Bin1_585MC_gain200_20260220-095000_-20.0C_0001.fit"
        f.touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   return_value={**dark_meta(f.stem, 10.01), "file_path": str(f)}):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert result.calibration[0].frame_type == "Dark"


def test_scan_calibration_flat_with_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        flat_dir = source / "Flat"
        flat_dir.mkdir(parents=True)
        f = flat_dir / "Flat_1.35s_Bin1_585MC_gain200_20260220-090000_-20.0C_L-Pro_0001.fit"
        f.touch()

        meta = {
            **dark_meta(f.stem, 1.35, "2026-02-20T09:00:00"),
            "file_path": str(f),
            "imagetyp": "Flat Frame",
        }
        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", return_value=meta):
            result = scan_source(source)

    assert len(result.calibration) == 1
    cal = result.calibration[0]
    assert cal.frame_type == "Flat"
    assert cal.filter == "L-Pro"
    assert cal.exposure_sec == 1.35


def test_scan_calibration_groups_same_params():
    # Two files with identical params land in one CalibrationGroup
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f1 = dark_dir / "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001.fit"
        f2 = dark_dir / "Dark_180.0s_Bin1_585MC_gain200_20260220-093000_-20.0C_0002.fit"
        f1.touch()
        f2.touch()

        def mock_extract(path):
            return {**dark_meta(path.stem, 180.0), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert len(result.calibration[0].files) == 2


# ── F4: session wall-clock span ──────────────────────────────────────────────

def test_scan_source_populates_session_span():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        stems = [
            "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001",
            "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220300_-20.0C_L-Pro_0002",
        ]
        for stem in stems:
            (light_dir / f"{stem}.fit").touch()

        def mock_extract(path):
            date_obs = (
                "2026-02-19T22:00:00" if path.name.endswith("0001.fit")
                else "2026-02-19T22:03:00"
            )
            return {**light_meta(path.stem, date_obs), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    s = result.sessions[0]
    assert s.start_utc == "2026-02-19T22:00:00"
    # end covers the last sub-exposure, not just when it started
    assert s.end_utc == "2026-02-19T22:06:00"


def test_scan_source_span_ignores_file_iteration_order():
    """Frames are collected in filename order, which need not be chronological.

    Regression guard for using frames[0] as "the first frame" (the live bug
    BACKLOG F5 records for temperature_c).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        # _0001 sorts first but was shot *last*, and with a longer exposure.
        (light_dir / "Light_M 81_300.0s_Bin1_585MC_gain200_20260219-233000_-20.0C_L-Pro_0001.fit").touch()
        (light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0002.fit").touch()

        def mock_extract(path):
            if path.name.endswith("0001.fit"):
                return {**light_meta(path.stem, "2026-02-19T23:30:00"),
                        "file_path": str(path), "exposure": 300.0}
            return {**light_meta(path.stem, "2026-02-19T22:00:00"), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    s = result.sessions[0]
    assert s.start_utc == "2026-02-19T22:00:00"
    assert s.end_utc == "2026-02-19T23:35:00"


def test_scan_source_metadata_from_chronologically_first_frame():
    """B14: representative metadata must come from the chronologically-first
    frame (earliest DATE-OBS), not the filename-first one.

    Simulates the SH2-101 scenario: 5×300s shot first, then 87×180s.
    "180.0s" sorts before "300.0s" lexically, so frames[0] by filename
    is a 180s frame — but the 300s frames were captured first.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "SH2-101"
        light_dir.mkdir(parents=True)
        # 180s frame — filename sorts FIRST ("1" < "3"), captured SECOND
        (light_dir / "Light_SH2-101_180.0s_Bin1_585MC_gain200_20260719-233000_-20.0C_L-Synergy_0006.fit").touch()
        # 300s frame — filename sorts SECOND, captured FIRST
        (light_dir / "Light_SH2-101_300.0s_Bin1_585MC_gain200_20260719-220000_-20.0C_L-Synergy_0001.fit").touch()

        def mock_extract(path):
            if "300.0s" in path.name:
                return {
                    **light_meta(path.stem, "2026-07-19T22:00:00"),
                    "file_path": str(path),
                    "object": "SH2-101",
                    "exposure": 300.0,
                    "ra_deg": 315.0,
                    "dec_deg": 68.2,
                }
            return {
                **light_meta(path.stem, "2026-07-19T23:30:00"),
                "file_path": str(path),
                "object": "SH2-101",
                "exposure": 180.0,
                "ra_deg": 315.5,
                "dec_deg": 34.1,  # wrong framing after mis-slew
            }

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    s = result.sessions[0]
    # Must reflect the 300s frame (captured first), not the 180s frame
    assert s.exposure_sec == 300.0
    assert s.ra_deg == pytest.approx(315.0)
    assert s.dec_deg == pytest.approx(68.2)


def test_scan_source_span_is_none_without_date_obs():
    """A session can only exist with a resolvable night, but guard the shape."""
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        (light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001.fit").touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   side_effect=lambda p: {**light_meta(p.stem), "file_path": str(p), "date_obs": ""}):
            result = scan_source(source)

    assert result.sessions == []
