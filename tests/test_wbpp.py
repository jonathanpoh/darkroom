import os
from datetime import date
from pathlib import Path
import pytest
from darkroom.wbpp import (
    next_session_num,
    discover_lights,
    discover_darks,
    discover_flat_files,
    discover_flat_darks,
    make_symlinks,
    find_real_files,
    clear_sessions,
)


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


# ── next_session_num ─────────────────────────────────────────────────────────

def test_next_session_num_empty(tmp_path):
    assert next_session_num(tmp_path) == 1


def test_next_session_num_with_existing(tmp_path):
    (tmp_path / "SESSION_1").mkdir()
    (tmp_path / "SESSION_3").mkdir()
    assert next_session_num(tmp_path) == 4


def test_next_session_num_ignores_non_session_dirs(tmp_path):
    (tmp_path / "SESSION_2").mkdir()
    (tmp_path / "notes.txt").write_text("hi")
    assert next_session_num(tmp_path) == 3


# ── discover_lights ───────────────────────────────────────────────────────────

def test_discover_lights_returns_fit_files(tmp_path):
    d = tmp_path / "Lights"
    touch(d / "Light_0001.fit")
    touch(d / "Light_0002.fit")
    touch(d / "Light_thn_0001.fit")  # thumbnail, excluded
    files = discover_lights(d)
    assert len(files) == 2
    assert all(f.suffix == ".fit" for f in files)


def test_discover_lights_missing_dir(tmp_path):
    files = discover_lights(tmp_path / "nonexistent")
    assert files == []


# ── discover_darks ────────────────────────────────────────────────────────────

def test_discover_darks_matches_exposure(tmp_path):
    d = tmp_path / "Darks"
    # ASIAir filename format: parse_exposure needs _<exposure>_ pattern (YYYYMMDD-HHMMSS)
    touch(d / "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_-20.0C_0001.fit")
    touch(d / "Dark_60.0s_Bin1_585MC_gain200_20260201-000000_-20.0C_0001.fit")
    files = discover_darks(d, exposure_sec=180.0)
    assert len(files) == 1
    assert "180.0s" in files[0].name


def test_discover_darks_no_match(tmp_path):
    d = tmp_path / "Darks"
    touch(d / "Dark_60.0s_Bin1_585MC_gain200_20260201-000000_-20.0C_0001.fit")
    files = discover_darks(d, exposure_sec=180.0)
    assert files == []


def test_discover_darks_missing_dir(tmp_path):
    files = discover_darks(tmp_path / "nonexistent", exposure_sec=180.0)
    assert files == []


# ── discover_flat_files ───────────────────────────────────────────────────────

def test_discover_flat_files_returns_all_fit(tmp_path):
    d = tmp_path / "Flats"
    touch(d / "Flat_0001.fit")
    touch(d / "Flat_0002.fit")
    files = discover_flat_files(d)
    assert len(files) == 2


def test_discover_flat_files_missing_dir(tmp_path):
    files = discover_flat_files(tmp_path / "nonexistent")
    assert files == []


# ── discover_flat_darks ───────────────────────────────────────────────────────

def test_discover_flat_darks_matches_date(tmp_path):
    d = tmp_path / "FlatDarks"
    # ASIAir datetime format: _YYYYMMDD-HHMMSS_
    touch(d / "Dark_1.35s_Bin1_585MC_gain200_20260220-053000_-20.0C_0001.fit")
    touch(d / "Dark_1.35s_Bin1_585MC_gain200_20260221-053000_-20.0C_0001.fit")
    files = discover_flat_darks(d, capture_date=date(2026, 2, 20))
    assert len(files) == 1
    assert "20260220" in files[0].name


def test_discover_flat_darks_missing_dir(tmp_path):
    files = discover_flat_darks(tmp_path / "nonexistent", capture_date=date(2026, 2, 20))
    assert files == []


# ── make_symlinks ─────────────────────────────────────────────────────────────

def test_make_symlinks_creates_links(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    files = [touch(src_dir / f"file_{i}.fit") for i in range(3)]
    dest = tmp_path / "dest"
    count = make_symlinks(files, dest)
    assert count == 3
    assert dest.is_dir()
    for f in files:
        link = dest / f.name
        assert link.is_symlink()
        assert link.resolve() == f.resolve()


def test_make_symlinks_skips_existing(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir(); dest.mkdir()
    f = touch(src / "file.fit")
    make_symlinks([f], dest)
    count = make_symlinks([f], dest)
    assert count == 0


def test_make_symlinks_empty_list(tmp_path):
    dest = tmp_path / "dest"
    count = make_symlinks([], dest)
    assert count == 0
    assert not dest.exists()


# ── find_real_files ───────────────────────────────────────────────────────────

def test_find_real_files_finds_non_symlinks(tmp_path):
    target = tmp_path / "M81"
    touch(target / "SESSION_1" / "notes.txt")
    src = tmp_path / "real.fit"
    src.write_bytes(b"")
    link = target / "SESSION_1" / "light.fit"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(src)
    real = find_real_files(target)
    assert len(real) == 1
    assert real[0].name == "notes.txt"


def test_find_real_files_empty_dir(tmp_path):
    assert find_real_files(tmp_path) == []


def test_find_real_files_nonexistent(tmp_path):
    assert find_real_files(tmp_path / "nonexistent") == []


# ── clear_sessions ────────────────────────────────────────────────────────────

def test_clear_sessions_removes_session_dirs(tmp_path):
    target = tmp_path / "M81"
    (target / "SESSION_1").mkdir(parents=True)
    (target / "SESSION_2").mkdir(parents=True)
    touch(target / "other_file.txt")
    clear_sessions(target)
    assert not (target / "SESSION_1").exists()
    assert not (target / "SESSION_2").exists()
    assert (target / "other_file.txt").exists()


def test_clear_sessions_nonexistent_dir(tmp_path):
    clear_sessions(tmp_path / "nonexistent")  # should not raise
