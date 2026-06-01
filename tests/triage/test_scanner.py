import re
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from unittest.mock import patch

from darkroom.triage.scanner import (
    TriageCandidate,
    scan_flat_restructure,
    scan_calibration_in_target,
    scan_processed_dirs,
    scan_thumbnail_cleanup,
    scan_legacy_sessions,
    scan_archive,
    scan_fits_headers,
)

_FLAT_DATE_RE = re.compile(r"^\d{8}_")
_CANONICAL_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\w+_\w+")


def make_fits(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16))
    hdu.header["OBJECT"] = "M 81"
    hdu.header["FOCALLEN"] = 400
    hdu.writeto(path, overwrite=True)
    return path


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "staging"


class TestScanFlatRestructure:
    def test_detects_yyyymmdd_flat_folder(self, archive):
        flat_dir = archive / "00_Calibration" / "Flats" / "20240110_FMA180_Canon6D_L-Pro"
        flat_dir.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert len(candidates) == 1
        c = candidates[0]
        assert c.category == "flat_restructure"
        assert "FMA180_Canon6D_L-Pro" in c.proposed_path
        assert "2024-01-10" in c.proposed_path

    def test_skips_already_canonical(self, archive):
        canon = (archive / "00_Calibration" / "Flats"
                 / "FMA180_Canon6D_L-Pro" / "2024-01-10")
        canon.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert candidates == []

    def test_normalises_nofilter_typo(self, archive):
        flat_dir = (archive / "00_Calibration" / "Flats"
                    / "20250203_FRA400_Canon6D_NoFIlter")
        flat_dir.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert candidates[0].proposed_path.endswith("NoFilter/2025-02-03")

    def test_unknown_ota_flagged(self, archive):
        flat_dir = (archive / "00_Calibration" / "Flats"
                    / "20230716_100mm_Canon6D")
        flat_dir.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert len(candidates) == 1
        assert candidates[0].proposed_path is None  # can't auto-map


class TestScanCalibrationInTarget:
    def test_detects_flats_subdir(self, archive):
        flats = (archive / "04_Deep Sky Objects" / "M 42" / "2025-01-17" / "Flats")
        flats.mkdir(parents=True)
        make_fits(flats / "flat001.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert any(c.category == "calibration_in_target" for c in candidates)

    def test_case_insensitive(self, archive):
        darks = (archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23" / "darks")
        darks.mkdir(parents=True)
        make_fits(darks / "dark001.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert len(candidates) == 1

    def test_plural_variants(self, archive):
        for name in ("flat", "Flats", "bias", "biases", "flatdarks"):
            d = (archive / "04_Deep Sky Objects" / "NGC 6960" / "2024-01-01" / name)
            d.mkdir(parents=True)
            make_fits(d / "frame.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert len(candidates) == 5

    def test_ignores_calibration_named_dirs_in_processed(self, archive):
        # A 'darks' folder inside _Processed/ or Pixinsight/ is processed output,
        # not raw calibration — must not be flagged.
        for proc in ("_Processed", "Pixinsight"):
            d = (archive / "04_Deep Sky Objects" / "NGC 6960" / proc / "darks")
            d.mkdir(parents=True)
            make_fits(d / "frame.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert candidates == []


class TestScanProcessedDirs:
    def test_detects_pixinsight_dir(self, archive):
        pi = archive / "04_Deep Sky Objects" / "NGC 6960" / "Pixinsight"
        pi.mkdir(parents=True)
        (pi / "project.pxiproject").write_text("x")
        candidates = scan_processed_dirs(archive / "04_Deep Sky Objects")
        assert len(candidates) == 1
        assert candidates[0].category == "processed_dir"
        assert candidates[0].proposed_path.endswith("_Processed")

    def test_skips_already_canonical(self, archive):
        proc = archive / "04_Deep Sky Objects" / "NGC 6960" / "_Processed"
        proc.mkdir(parents=True)
        candidates = scan_processed_dirs(archive / "04_Deep Sky Objects")
        assert candidates == []


class TestScanThumbnailCleanup:
    def test_detects_thn_jpg(self, archive):
        thn = archive / "04_Deep Sky Objects" / "M 81" / "2024-01-01" / "img_thn.jpg"
        thn.parent.mkdir(parents=True)
        thn.write_bytes(b"jpg")
        candidates = scan_thumbnail_cleanup(archive)
        assert len(candidates) == 1
        assert candidates[0].category == "thumbnail_cleanup"

    def test_case_insensitive_extension(self, archive):
        thn = archive / "frame_thn.JPG"
        archive.mkdir(parents=True, exist_ok=True)
        thn.write_bytes(b"jpg")
        candidates = scan_thumbnail_cleanup(archive)
        assert len(candidates) == 1


class TestScanLegacySessions:
    def test_detects_date_only_folder(self, archive):
        session = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23"
        make_fits(session / "Lights" / "frame.fit")
        candidates = scan_legacy_sessions(archive / "04_Deep Sky Objects")
        assert any(c.category == "legacy_session" for c in candidates)

    def test_skips_canonical_session(self, archive):
        session = (archive / "04_Deep Sky Objects" / "M 42"
                   / "2026-02-22_FRA400_ZWOASI585MCPro")
        make_fits(session / "Lights" / "L-Pro" / "frame.fit")
        candidates = scan_legacy_sessions(archive / "04_Deep Sky Objects")
        assert candidates == []

    def test_skips_processed_dirs(self, archive):
        proc = archive / "04_Deep Sky Objects" / "M 42" / "_Processed"
        proc.mkdir(parents=True)
        candidates = scan_legacy_sessions(archive / "04_Deep Sky Objects")
        assert candidates == []


class TestScanFitsHeaders:
    def test_detects_missing_object(self, archive):
        session = (archive / "04_Deep Sky Objects" / "M 81"
                   / "2023-08-06_FRA400_ZWOASI585MCPro")
        make_fits(session / "Lights" / "frame.fit")
        # Patch check_fits_object to return MISSING for any file
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("MISSING", None)):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert any(c.category == "missing_object" for c in candidates)

    def test_detects_fov_object(self, archive):
        session = (archive / "04_Deep Sky Objects" / "NGC 6960"
                   / "2024-05-07_FRA400_ZWOASI585MCPro")
        make_fits(session / "Lights" / "frame.fit")
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("FOV", "FOV")):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert any(c.category == "missing_object" for c in candidates)

    def test_proposes_object_from_folder_name(self, archive):
        session = (archive / "04_Deep Sky Objects" / "NGC 6960"
                   / "2024-05-07_FRA400_ZWOASI585MCPro")
        make_fits(session / "Lights" / "frame.fit")
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("FOV", "FOV")):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        c = next(c for c in candidates if c.category == "missing_object")
        assert c.proposed_value == "NGC 6960"

    def test_ra_dec_mismatch_flagged(self, archive):
        session = (archive / "04_Deep Sky Objects" / "M 81"
                   / "2024-01-01_FRA400_ZWOASI585MCPro")
        make_fits(session / "Lights" / "frame.fit")
        mismatch = {"separation_deg": 30.0, "simbad_ra": 10.0, "simbad_dec": 40.0,
                    "frame_ra": 100.0, "frame_dec": 20.0, "target_name": "M 81"}
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=(None, "M 81")), \
             patch("darkroom.triage.scanner.check_ra_dec", return_value=mismatch):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert any(c.category == "ra_dec_mismatch" for c in candidates)

    def test_skips_legacy_session_to_avoid_collision(self, archive):
        # A non-canonical (legacy) session folder is handled by
        # scan_legacy_sessions; scan_fits_headers must NOT emit a candidate for
        # the same source_path, or the two collide on the UNIQUE source_path.
        session = archive / "04_Deep Sky Objects" / "M 81" / "2023-08-06"
        make_fits(session / "Lights" / "frame.fit")
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("MISSING", None)):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert candidates == []

    def test_no_source_path_collision_with_legacy(self, archive):
        # Verify the two scanners never share a source_path on the same archive.
        legacy = archive / "04_Deep Sky Objects" / "M 81" / "2023-08-06"
        make_fits(legacy / "Lights" / "frame.fit")
        canonical = (archive / "04_Deep Sky Objects" / "M 81"
                     / "2024-01-01_FRA400_ZWOASI585MCPro")
        make_fits(canonical / "Lights" / "frame.fit")
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("MISSING", None)):
            header = {c.source_path for c in
                      scan_fits_headers(archive / "04_Deep Sky Objects")}
        legacy_paths = {c.source_path for c in
                        scan_legacy_sessions(archive / "04_Deep Sky Objects")}
        assert header.isdisjoint(legacy_paths)
