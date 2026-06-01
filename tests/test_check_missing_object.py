import pytest
from pathlib import Path
from check_missing_object import check_object_value, collect_fits_files, scan_file


class TestCheckObjectValue:
    def test_none_returns_missing(self):
        assert check_object_value(None) == "MISSING"

    def test_empty_string_returns_missing(self):
        assert check_object_value("") == "MISSING"

    def test_whitespace_returns_missing(self):
        assert check_object_value("   ") == "MISSING"

    def test_fov_uppercase_returns_fov(self):
        assert check_object_value("FOV") == "FOV"

    def test_fov_lowercase_returns_fov(self):
        assert check_object_value("fov") == "FOV"

    def test_fov_mixed_case_returns_fov(self):
        assert check_object_value("Fov") == "FOV"

    def test_valid_object_returns_none(self):
        assert check_object_value("M 81") is None

    def test_valid_object_ngc_returns_none(self):
        assert check_object_value("NGC 7380") is None


class TestCollectFitsFiles:
    def test_single_fit_file_found(self, tmp_path):
        f = tmp_path / "frame.fit"
        f.touch()
        assert collect_fits_files(tmp_path) == [f]

    def test_fits_extension_found(self, tmp_path):
        f = tmp_path / "frame.fits"
        f.touch()
        assert collect_fits_files(tmp_path) == [f]

    def test_recursive_subdirectory(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "frame.fit"
        f.touch()
        assert collect_fits_files(tmp_path) == [f]

    def test_thumbnail_excluded(self, tmp_path):
        (tmp_path / "frame_thn.fit").touch()
        assert collect_fits_files(tmp_path) == []

    def test_non_fits_excluded(self, tmp_path):
        (tmp_path / "frame.xisf").touch()
        assert collect_fits_files(tmp_path) == []

    def test_case_insensitive_extension(self, tmp_path):
        f = tmp_path / "frame.FIT"
        f.touch()
        assert collect_fits_files(tmp_path) == [f]

    def test_returns_sorted(self, tmp_path):
        (tmp_path / "b.fit").touch()
        (tmp_path / "a.fit").touch()
        result = collect_fits_files(tmp_path)
        assert result == sorted(result)


from astropy.io import fits
import numpy as np


def _make_fits(path: Path, object_val=None, set_key=True) -> Path:
    """Write a minimal FITS file. If set_key=False, OBJECT header is absent."""
    hdu = fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16))
    if set_key:
        hdu.header["OBJECT"] = object_val if object_val is not None else ""
    hdu.writeto(path, overwrite=True)
    return path


class TestScanFile:
    def test_valid_object_returns_none(self, tmp_path):
        p = _make_fits(tmp_path / "good.fit", "M 81")
        assert scan_file(p) is None

    def test_missing_key_flagged(self, tmp_path):
        p = _make_fits(tmp_path / "nokey.fit", set_key=False)
        assert scan_file(p) == (p, "MISSING")

    def test_empty_object_flagged(self, tmp_path):
        p = _make_fits(tmp_path / "empty.fit", "")
        assert scan_file(p) == (p, "MISSING")

    def test_fov_flagged(self, tmp_path):
        p = _make_fits(tmp_path / "fov.fit", "FOV")
        assert scan_file(p) == (p, "FOV")

    def test_fov_case_insensitive(self, tmp_path):
        p = _make_fits(tmp_path / "fov_lc.fit", "fov")
        assert scan_file(p) == (p, "FOV")

    def test_corrupt_file_returns_none_with_warning(self, tmp_path, capsys):
        p = tmp_path / "corrupt.fit"
        p.write_bytes(b"not a fits file")
        result = scan_file(p)
        assert result is None
        assert "WARNING" in capsys.readouterr().err
