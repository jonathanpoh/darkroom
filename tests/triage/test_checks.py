from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest
from astropy.io import fits

from darkroom.triage.checks import (
    check_object_value,
    check_fits_object,
    check_ra_dec,
)


def make_fits(path: Path, **headers) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((10, 10), dtype=np.uint16))
    for k, v in headers.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


class TestCheckObjectValue:
    def test_none_is_missing(self):
        assert check_object_value(None) == "MISSING"

    def test_blank_is_missing(self):
        assert check_object_value("  ") == "MISSING"

    def test_fov_detected(self):
        assert check_object_value("FOV") == "FOV"
        assert check_object_value("fov") == "FOV"

    def test_valid_returns_none(self):
        assert check_object_value("M 81") is None
        assert check_object_value("NGC 6960") is None


class TestCheckFitsObject:
    def test_good_object(self, tmp_path):
        f = make_fits(tmp_path / "good.fit", OBJECT="NGC 6960")
        reason, val = check_fits_object(f)
        assert reason is None
        assert val == "NGC 6960"

    def test_fov_object(self, tmp_path):
        f = make_fits(tmp_path / "fov.fit", OBJECT="FOV")
        reason, val = check_fits_object(f)
        assert reason == "FOV"

    def test_missing_object(self, tmp_path):
        f = make_fits(tmp_path / "missing.fit")
        reason, val = check_fits_object(f)
        assert reason == "MISSING"


class TestCheckRaDec:
    def _make_mock_table(self, ra: float, dec: float) -> MagicMock:
        """Return a mock SIMBAD table whose RA/DEC columns convert to floats correctly."""
        ra_col = MagicMock()
        ra_col.__getitem__ = MagicMock(return_value=ra)
        dec_col = MagicMock()
        dec_col.__getitem__ = MagicMock(return_value=dec)
        mock_table = MagicMock()
        mock_table.__getitem__ = MagicMock(side_effect=lambda k: ra_col if k == "RA" else dec_col)
        return mock_table

    def test_matching_coords_returns_none(self, tmp_path):
        # M 81 is at RA~148.9, Dec~69.1
        f = make_fits(tmp_path / "m81.fit", RA=148.888, DEC=69.065)
        mock_table = self._make_mock_table(148.888, 69.065)

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 81", threshold_deg=5.0)

        assert result is None

    def test_mismatch_returns_dict(self, tmp_path):
        # Frame points at M 81 but folder says NGC 224 (M 31 — far away)
        f = make_fits(tmp_path / "wrong.fit", RA=148.888, DEC=69.065)
        mock_table = self._make_mock_table(10.685, 41.269)  # M 31

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 31", threshold_deg=5.0)

        assert result is not None
        assert result["separation_deg"] > 5.0
        assert "simbad_ra" in result

    def test_no_ra_dec_header_returns_none(self, tmp_path):
        f = make_fits(tmp_path / "noradec.fit", OBJECT="M 81")
        result = check_ra_dec(f, "M 81")
        assert result is None

    def test_simbad_unknown_target_returns_none(self, tmp_path):
        f = make_fits(tmp_path / "frame.fit", RA=10.0, DEC=20.0)
        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = None
            result = check_ra_dec(f, "Unknown Nebula X")
        assert result is None
