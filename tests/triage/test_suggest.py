from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from darkroom.triage.suggest import (
    has_placeholder,
    suggest_calibration_dest,
    suggest_legacy_session,
)


def make_fits(path: Path, **headers) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16))
    for k, v in headers.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


class TestHasPlaceholder:
    def test_none_and_empty(self):
        assert has_placeholder(None)
        assert has_placeholder("")

    def test_with_token(self):
        assert has_placeholder("/a/{OTA?}/b")
        assert has_placeholder("/a/2024-01-01_{OTA?}_Cam")

    def test_complete_path(self):
        assert not has_placeholder("/a/b/c")


class TestSuggestLegacySession:
    def test_complete_suggestion(self, tmp_path):
        target = tmp_path / "04_Deep Sky Objects" / "M 81"
        session = target / "2023-08-06"
        make_fits(session / "Lights" / "frame.fit",
                  **{"DATE-OBS": "2023-08-06T21:30:00", "FOCALLEN": 400,
                     "INSTRUME": "ZWO ASI585MC Pro"})
        proposed, missing = suggest_legacy_session(session, target)
        assert missing == []
        assert proposed.endswith("2023-08-06_FRA400_ZWOASI585MCPro")
        assert not has_placeholder(proposed)

    def test_unknown_ota_is_partial(self, tmp_path):
        target = tmp_path / "04_Deep Sky Objects" / "M 45"
        session = target / "2023-08-12"
        # 250mm has no canonical OTA mapping
        make_fits(session / "Lights" / "frame.fit",
                  **{"DATE-OBS": "2023-08-12T20:00:00", "FOCALLEN": 250,
                     "INSTRUME": "Canon EOS 6D"})
        proposed, missing = suggest_legacy_session(session, target)
        assert "ota" in missing
        assert "{OTA?}" in proposed
        assert has_placeholder(proposed)
        # the parts we DO know are still filled in
        assert "2023-08-12" in proposed
        assert "CanonEOS6D" in proposed

    def test_missing_date_and_camera(self, tmp_path):
        target = tmp_path / "04_Deep Sky Objects" / "M 31"
        session = target / "old_session"
        make_fits(session / "Lights" / "frame.fit", **{"FOCALLEN": 180})
        proposed, missing = suggest_legacy_session(session, target)
        assert "date" in missing and "camera" in missing
        assert "{DATE?}" in proposed and "{CAMERA?}" in proposed
        assert "FMA180" in proposed  # ota resolved

    def test_no_frames(self, tmp_path):
        target = tmp_path / "04_Deep Sky Objects" / "M 31"
        session = target / "empty"
        session.mkdir(parents=True)
        proposed, missing = suggest_legacy_session(session, target)
        assert proposed is None
        assert missing == ["frames"]


class TestSuggestCalibrationDest:
    def test_dark_only_needs_camera(self, tmp_path):
        archive = tmp_path / "staging"
        darks = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23" / "Darks"
        make_fits(darks / "dark.fit",
                  **{"INSTRUME": "ZWO ASI585MC Pro", "EXPOSURE": 180.0})
        proposed, missing = suggest_calibration_dest(darks, archive)
        assert missing == []
        assert proposed.endswith("00_Calibration/Darks/ZWOASI585MCPro")

    def test_flat_full_suggestion(self, tmp_path):
        archive = tmp_path / "staging"
        flats = archive / "04_Deep Sky Objects" / "M 42" / "2024-01-10" / "Flats"
        make_fits(flats / "Flat_180.0s_L-Pro_0001.fit",
                  **{"INSTRUME": "ZWO ASI585MC Pro", "FOCALLEN": 400,
                     "DATE-OBS": "2024-01-10T08:00:00", "EXPOSURE": 2.0})
        proposed, missing = suggest_calibration_dest(flats, archive)
        assert missing == []
        assert proposed.endswith(
            "00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2024-01-10"
        )

    def test_flat_missing_filter_is_partial(self, tmp_path):
        archive = tmp_path / "staging"
        flats = archive / "04_Deep Sky Objects" / "M 42" / "2024-01-10" / "Flats"
        # no filter in filename or header
        make_fits(flats / "frame0001.fit",
                  **{"INSTRUME": "ZWO ASI585MC Pro", "FOCALLEN": 400,
                     "DATE-OBS": "2024-01-10T08:00:00", "EXPOSURE": 2.0})
        proposed, missing = suggest_calibration_dest(flats, archive)
        assert "filter" in missing
        assert "{FILTER?}" in proposed
        assert has_placeholder(proposed)

    def test_short_dark_becomes_flatdark(self, tmp_path):
        archive = tmp_path / "staging"
        darks = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23" / "Darks"
        make_fits(darks / "dark.fit",
                  **{"INSTRUME": "Canon EOS 6D", "EXPOSURE": 2.0})
        proposed, missing = suggest_calibration_dest(darks, archive)
        assert proposed.endswith("00_Calibration/FlatDarks/CanonEOS6D")

    def test_bias_dest(self, tmp_path):
        archive = tmp_path / "staging"
        bias = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23" / "bias"
        make_fits(bias / "bias.fit",
                  **{"INSTRUME": "ZWO ASI585MC Pro", "EXPOSURE": 0.001})
        proposed, missing = suggest_calibration_dest(bias, archive)
        assert missing == []
        assert proposed.endswith("00_Calibration/Bias/ZWOASI585MCPro/Raw")
