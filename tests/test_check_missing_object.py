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
