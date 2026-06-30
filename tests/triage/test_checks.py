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
        """Return a mock SIMBAD table whose ra/dec columns convert to floats correctly.

        astroquery >= 0.4.7 returns lowercase column names.
        """
        ra_col = MagicMock()
        ra_col.__getitem__ = MagicMock(return_value=ra)
        dec_col = MagicMock()
        dec_col.__getitem__ = MagicMock(return_value=dec)
        mock_table = MagicMock()
        mock_table.colnames = ["main_id", "ra", "dec"]
        mock_table.__len__ = MagicMock(return_value=1)
        mock_table.__getitem__ = MagicMock(side_effect=lambda k: ra_col if k == "ra" else dec_col)
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

    def test_sexagesimal_coords_do_not_crash(self, tmp_path):
        """Regression for B4: float(ra)/float(dec) crashed with ValueError on
        sexagesimal strings ("09 55 33" / "+69 03 55") — older rigs write RA/DEC
        that way instead of float degrees. That ValueError used to propagate all
        the way out of scan_archive and abort the whole triage scan.

        "09 55 33" hourangle == 148.8875 deg, "+69 03 55" == 69.065278 deg —
        same M 81 coordinates already used in float-degree form elsewhere in
        this file (148.888 / 69.065), so the mock SIMBAD position below matches.
        """
        f = make_fits(tmp_path / "sexagesimal.fit", RA="09 55 33", DEC="+69 03 55")
        mock_table = self._make_mock_table(148.888, 69.065)

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 81", threshold_deg=5.0)

        assert result is None

    def test_sexagesimal_mismatch_returns_degree_values(self, tmp_path):
        """Same sexagesimal input, but mismatched against a distant target — the
        returned dict's frame_ra/frame_dec (line 71-72 of checks.py) must also
        not crash, and must report parsed degree values, not the raw strings."""
        f = make_fits(tmp_path / "wrong.fit", RA="09 55 33", DEC="+69 03 55")  # M 81
        mock_table = self._make_mock_table(10.685, 41.269)  # M 31, far away

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 31", threshold_deg=5.0)

        assert result is not None
        assert result["frame_ra"] == pytest.approx(148.8875, abs=1e-3)
        assert result["frame_dec"] == pytest.approx(69.065278, abs=1e-3)
        assert result["separation_deg"] > 5.0
