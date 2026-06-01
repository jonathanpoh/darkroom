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
