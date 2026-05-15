import pytest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from wbpp_finish import (
    _find_processing_date, _build_dest, _copy_flat,
    _list_intermediates, _list_outputs, _confirm_and_delete,
)


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_find_processing_date_returns_today(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir()
    touch(master / "masterLight_BIN-1_3840x2160_FILTER-L-Extreme_RGB.xisf")
    result = _find_processing_date(master, processed, None)
    assert result == date.today().isoformat()


def test_find_processing_date_prefers_processed(tmp_path):
    import os, time
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir(); processed.mkdir()
    older = master / "masterLight.xisf"
    newer = processed / "final.xisf"
    touch(older); touch(newer)
    # Make master file 2 days older than processed
    past = time.time() - 2 * 86400
    os.utime(older, (past, past))
    result = _find_processing_date(master, processed, None)
    assert result == date.today().isoformat()


def test_find_processing_date_override(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir()
    touch(master / "masterLight.xisf")
    assert _find_processing_date(master, processed, "2025-12-31") == "2025-12-31"


def test_find_processing_date_no_files_exits(tmp_path):
    master = tmp_path / "master"
    processed = tmp_path / "processed"
    master.mkdir(); processed.mkdir()
    with pytest.raises(SystemExit):
        _find_processing_date(master, processed, None)


def test_build_dest(tmp_path):
    dest = _build_dest(tmp_path, "M 81", "2026-05-15")
    assert dest == tmp_path / "04_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-15"


def test_build_dest_target_with_spaces(tmp_path):
    dest = _build_dest(tmp_path, "NGC 1499", "2026-03-01")
    assert dest == tmp_path / "04_Deep Sky Objects" / "NGC 1499" / "_Processed" / "2026-03-01"


def test_copy_flat_copies_files(tmp_path):
    src = tmp_path / "master"
    src.mkdir()
    touch(src / "masterLight.xisf")
    touch(src / "masterDark.xisf")
    dest = tmp_path / "dest" / "master"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 2
    assert (dest / "masterLight.xisf").exists()
    assert (dest / "masterDark.xisf").exists()


def test_copy_flat_skips_existing(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir(); dest.mkdir()
    touch(src / "file.xisf")
    touch(dest / "file.xisf")
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 0


def test_copy_flat_empty_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 0
    assert not dest.exists()


def test_copy_flat_dry_run_does_not_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    touch(src / "file.xisf")
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=True)
    assert count == 1
    assert not dest.exists()


def test_copy_flat_ignores_subdirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "subdir").mkdir()
    touch(src / "file.xisf")
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 1
    assert not (dest / "subdir").exists()


def test_list_intermediates_returns_existing(tmp_path):
    (tmp_path / "calibrated").mkdir()
    (tmp_path / "debayered").mkdir()
    (tmp_path / "SESSION_1").mkdir()
    (tmp_path / "SESSION_2").mkdir()
    (tmp_path / "master").mkdir()        # should NOT appear
    (tmp_path / "processed").mkdir()     # should NOT appear
    result = _list_intermediates(tmp_path)
    names = {p.name for p in result}
    assert "calibrated" in names
    assert "debayered" in names
    assert "SESSION_1" in names
    assert "SESSION_2" in names
    assert "master" not in names
    assert "processed" not in names


def test_list_intermediates_skips_missing(tmp_path):
    (tmp_path / "SESSION_1").mkdir()
    result = _list_intermediates(tmp_path)
    assert len(result) == 1
    assert result[0].name == "SESSION_1"


def test_list_intermediates_includes_all_named(tmp_path):
    for name in ("calibrated", "debayered", "fastIntegration", "logs"):
        (tmp_path / name).mkdir()
    result = _list_intermediates(tmp_path)
    names = {p.name for p in result}
    assert names == {"calibrated", "debayered", "fastIntegration", "logs"}


def test_list_outputs_returns_master_and_processed(tmp_path):
    (tmp_path / "master").mkdir()
    (tmp_path / "processed").mkdir()
    result = _list_outputs(tmp_path)
    names = {p.name for p in result}
    assert names == {"master", "processed"}


def test_list_outputs_skips_missing(tmp_path):
    (tmp_path / "master").mkdir()
    result = _list_outputs(tmp_path)
    assert len(result) == 1
    assert result[0].name == "master"


def test_confirm_and_delete_dry_run_does_not_delete(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    _confirm_and_delete([d], "Intermediates", dry_run=True)
    assert d.exists()


def test_confirm_and_delete_yes_deletes(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    with patch("builtins.input", return_value="yes"):
        _confirm_and_delete([d], "Intermediates", dry_run=False)
    assert not d.exists()


def test_confirm_and_delete_no_skips(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    with patch("builtins.input", return_value=""):
        _confirm_and_delete([d], "Intermediates", dry_run=False)
    assert d.exists()


def test_confirm_and_delete_empty_list(tmp_path):
    _confirm_and_delete([], "Intermediates", dry_run=False)  # should not raise
