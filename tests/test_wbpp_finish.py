import pytest
from datetime import date
from pathlib import Path

from wbpp_finish import _find_master_date, _build_dest


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_find_master_date_returns_today(tmp_path):
    master = tmp_path / "master"
    master.mkdir()
    touch(master / "masterLight_BIN-1_3840x2160_FILTER-L-Extreme_RGB.xisf")
    result = _find_master_date(master)
    assert result == date.today().isoformat()


def test_find_master_date_no_file_exits(tmp_path):
    master = tmp_path / "master"
    master.mkdir()
    with pytest.raises(SystemExit):
        _find_master_date(master)


def test_build_dest(tmp_path):
    dest = _build_dest(tmp_path, "M 81", "2026-05-15")
    assert dest == tmp_path / "04_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-15"


def test_build_dest_target_with_spaces(tmp_path):
    dest = _build_dest(tmp_path, "NGC 1499", "2026-03-01")
    assert dest == tmp_path / "04_Deep Sky Objects" / "NGC 1499" / "_Processed" / "2026-03-01"
