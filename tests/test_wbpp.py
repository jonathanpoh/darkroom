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
from darkroom.cataloger import init_db, upsert_calibration_set
from darkroom.catalog_client import LocalBackend
from darkroom.names import make_session_id, parse_wbpp_panel_dir, session_dest_rel
from darkroom.prep import build_wbpp_sessions


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


def test_discover_darks_temperature_filters_out_of_tolerance(tmp_path):
    d = tmp_path / "Darks"
    # B11 follow-up: raw dark sets at different temperatures share one folder
    touch(d / "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_-20.0C_0001.fit")
    touch(d / "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_-10.0C_0001.fit")
    files = discover_darks(d, exposure_sec=180.0, temperature_c=-20.0, temp_tolerance=3.0)
    assert len(files) == 1
    assert "-20.0C" in files[0].name


def test_discover_darks_temperature_keeps_unparseable(tmp_path):
    d = tmp_path / "Darks"
    touch(d / "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_0001.fit")  # no temp token
    files = discover_darks(d, exposure_sec=180.0, temperature_c=-20.0, temp_tolerance=3.0)
    assert len(files) == 1


def test_discover_darks_temperature_none_keeps_all(tmp_path):
    d = tmp_path / "Darks"
    touch(d / "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_-20.0C_0001.fit")
    touch(d / "Dark_180.0s_Bin1_585MC_gain200_20260201-000000_-10.0C_0001.fit")
    files = discover_darks(d, exposure_sec=180.0)
    assert len(files) == 2


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


def test_discover_flat_darks_wrong_date_excluded(tmp_path):
    d = tmp_path / "FlatDarks"
    touch(d / "Dark_1.35s_Bin1_585MC_gain200_20260221-053000_-20.0C_0001.fit")
    files = discover_flat_darks(d, capture_date=date(2026, 2, 20))
    assert files == []


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
    assert any(r.name == "notes.txt" for r in real)


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


# ── M3: build_wbpp_sessions splits mosaic panels into their own trees ─────────
#
# WBPP merges every panel at final integration regardless of grouping
# keywords (tested on real data 2026-08-31, see BACKLOG.md M3) — the only
# reliable separation is one WBPP run per panel, i.e. one directory per panel.

def _panel_row(
    archive: Path, *, obs_date: str, panel: str | None, target: str = "IC 4604",
    ota: str = "FMA180", camera: str = "ZWOASI585MCPro", filter_: str = "L-Pro",
    gain: int = 200, exposure_sec: float = 60.0, temperature_c: float = -10.0,
    frame_count: int = 2,
) -> dict:
    """Build one archived-lights row as build_wbpp_sessions/_build_night expect
    it (i.e. what backend.query_sessions would return), touching frame_count
    real .fit files at its lights_path so discover_lights finds them.
    """
    lights_rel = session_dest_rel(target, obs_date, ota, camera, filter_, panel=panel)
    tag = panel or "main"
    for i in range(frame_count):
        touch(archive / lights_rel / f"Light_{obs_date}_{tag}_{i}.fit")
    return {
        "session_id": make_session_id(target, obs_date, ota, camera, filter_, panel=panel),
        "target": target, "obs_date": obs_date, "ota": ota, "camera": camera,
        "filter": filter_, "panel": panel, "gain": gain, "exposure_sec": exposure_sec,
        "temperature_c": temperature_c, "frame_count": frame_count,
        "lights_path": str(lights_rel),
    }


def _empty_backend(tmp_path: Path) -> LocalBackend:
    """A catalog with the schema but no calibration sets — build_wbpp_sessions
    doesn't need real darks/flats/bias to exercise directory layout, and
    _build_night degrades cleanly (prints '0 symlinks') when none match.
    """
    catalog = tmp_path / "cat.db"
    init_db(catalog)
    return LocalBackend(catalog)


def test_build_wbpp_sessions_four_panels_one_night(tmp_path):
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    panels = ("1-1", "1-2", "2-1", "2-2")
    rows = [_panel_row(archive, obs_date="2026-04-26", panel=p) for p in panels]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")

    target_dir = wbpp_root / "IC4604"
    for p in panels:
        panel_dir = target_dir / f"PANEL_{p}"
        assert (panel_dir / "SESSION_1").is_dir()
        assert (panel_dir / "Output" / "processed").is_dir()
    # Target-level merged-mosaic output, alongside the four panel outputs.
    assert (target_dir / "Output" / "processed").is_dir()
    # No stray SESSION_N at target root — every row went into a panel.
    assert not (target_dir / "SESSION_1").exists()


def test_build_wbpp_sessions_two_nights_two_panels(tmp_path):
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    rows = [
        _panel_row(archive, obs_date=d, panel=p)
        for d in ("2026-04-26", "2026-04-27")
        for p in ("1-1", "1-2")
    ]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")

    target_dir = wbpp_root / "IC4604"
    for p in ("1-1", "1-2"):
        panel_dir = target_dir / f"PANEL_{p}"
        assert (panel_dir / "SESSION_1").is_dir()
        assert (panel_dir / "SESSION_2").is_dir()


def test_build_wbpp_sessions_null_panel_unchanged_layout(tmp_path):
    """A non-mosaic target's layout is byte-identical to before M3: no
    PANEL_ level appears anywhere."""
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    rows = [_panel_row(archive, obs_date="2026-04-26", panel=None)]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")

    target_dir = wbpp_root / "IC4604"
    assert (target_dir / "SESSION_1").is_dir()
    assert (target_dir / "Output" / "processed").is_dir()
    assert not any(
        parse_wbpp_panel_dir(p.name) for p in target_dir.iterdir() if p.is_dir()
    )


def _mixed_rows(archive):
    """One single-pointing night plus two panels of a mosaic night."""
    return [
        _panel_row(archive, obs_date="2026-04-20", panel=None),
        _panel_row(archive, obs_date="2026-04-26", panel="1-1"),
        _panel_row(archive, obs_date="2026-04-26", panel="1-2"),
    ]


def test_build_wbpp_sessions_refuses_to_mix_panels_and_single_pointing(tmp_path, capsys):
    """Mosaic panels and ordinary nights cannot share one WBPP tree.

    `<target>/Output/` would mean two things at once — the mosaic's merge
    destination and the ordinary session's WBPP output dir — and `finish`
    resolves that by ignoring target-level SESSION_N once any PANEL_* exists,
    so the single-pointing night would be built and then never finished.
    """
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"

    with pytest.raises(SystemExit):
        build_wbpp_sessions(_mixed_rows(archive), backend=backend, output=archive,
                            wbpp_root=wbpp_root, target_name="IC 4604")

    err = capsys.readouterr().err
    # Must name which is which, so the user can re-run with the right --date.
    assert "single-pointing  2026-04-20" in err
    assert "panel 1-1" in err
    assert "panel 1-2" in err
    # And hand back runnable commands rather than just complaining.
    assert '--target "IC 4604" --date 2026-04-26' in err
    assert '--target "IC 4604" --date 2026-04-20' in err
    # Nothing built.
    assert not (wbpp_root / "IC4604").exists()


def test_build_wbpp_sessions_mixed_null_and_panels(tmp_path):
    """With the guard bypassed, the layout is still correct: the ordinary night
    builds at target level, the panels get their own dirs."""
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"

    build_wbpp_sessions(_mixed_rows(archive), backend=backend, output=archive,
                        wbpp_root=wbpp_root, target_name="IC 4604",
                        allow_mixed_panels=True)

    target_dir = wbpp_root / "IC4604"
    assert (target_dir / "SESSION_1").is_dir()
    assert (target_dir / "PANEL_1-1" / "SESSION_1").is_dir()
    assert (target_dir / "PANEL_1-2" / "SESSION_1").is_dir()


def test_build_wbpp_sessions_guard_allows_panels_only(tmp_path):
    """The guard must not fire on an all-panel prep — the normal mosaic case."""
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    rows = [
        _panel_row(archive, obs_date="2026-04-26", panel="1-1"),
        _panel_row(archive, obs_date="2026-04-26", panel="1-2"),
    ]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                        target_name="IC 4604")

    assert (wbpp_root / "IC4604" / "PANEL_1-1" / "SESSION_1").is_dir()


def test_build_wbpp_sessions_lights_isolated_per_panel(tmp_path):
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    rows = [
        _panel_row(archive, obs_date="2026-04-26", panel="1-1", frame_count=3),
        _panel_row(archive, obs_date="2026-04-26", panel="1-2", frame_count=5),
    ]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")

    target_dir = wbpp_root / "IC4604"
    lights_1_1 = list((target_dir / "PANEL_1-1" / "SESSION_1" / "Lights" / "FILTER_L-Pro").glob("*.fit"))
    lights_1_2 = list((target_dir / "PANEL_1-2" / "SESSION_1" / "Lights" / "FILTER_L-Pro").glob("*.fit"))
    assert len(lights_1_1) == 3
    assert len(lights_1_2) == 5


def test_build_wbpp_sessions_no_panel_level_under_calibration(tmp_path):
    """Calibration must NOT be panel-split: each panel's tree gets its own
    full calibration set by virtue of being its own tree, not a nested
    PANEL_ subdir under Flats/Darks/FlatDarks."""
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)
    flats_rel = "00_Calibration/Flats/FMA180_ZWOASI585MCPro_L-Pro/2026-04-27"
    touch(archive / flats_rel / "Flat_L-Pro_2.0s_20260427-080000_-10.0C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "flat1", "frame_type": "Flat", "camera": "ZWOASI585MCPro", "ota": "FMA180",
        "filter": "L-Pro", "gain": 200, "exposure_sec": 2.0, "temperature_c": -10.0,
        "frame_count": 1, "capture_date": "2026-04-27", "folder_path": flats_rel,
        "is_master": 0,
    })
    backend = LocalBackend(catalog)
    wbpp_root = tmp_path / "WBPP"
    rows = [_panel_row(archive, obs_date="2026-04-26", panel="1-1")]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")

    session_dir = wbpp_root / "IC4604" / "PANEL_1-1" / "SESSION_1"
    checked_any = False
    for sub in ("Darks", "Flats", "FlatDarks"):
        base = session_dir / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            checked_any = True
            assert parse_wbpp_panel_dir(p.name) is None
    assert checked_any, "expected at least the Flats/FILTER_L-Pro/ tree to exist"


def test_build_wbpp_sessions_overwrite_clears_panel_dirs(tmp_path):
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    rows = [_panel_row(archive, obs_date="2026-04-26", panel="1-1")]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")
    target_dir = wbpp_root / "IC4604"
    assert (target_dir / "PANEL_1-1" / "SESSION_1").is_dir()

    # Simulate a stray leftover SESSION_2 in the panel dir from a previous
    # run with more nights — --overwrite must remove the whole panel dir,
    # not just SESSION dirs at the target root.
    (target_dir / "PANEL_1-1" / "SESSION_2").mkdir()

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604", overwrite=True)

    assert (target_dir / "PANEL_1-1" / "SESSION_1").is_dir()
    assert not (target_dir / "PANEL_1-1" / "SESSION_2").exists()


def test_build_wbpp_sessions_overwrite_refuses_real_files_without_tty(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    backend = _empty_backend(tmp_path)
    wbpp_root = tmp_path / "WBPP"
    rows = [_panel_row(archive, obs_date="2026-04-26", panel="1-1")]

    build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                         target_name="IC 4604")
    # A real (non-symlink) file inside a panel dir simulates PixInsight output
    # left behind — --overwrite must refuse to delete it, not just at the
    # target root but inside PANEL_* trees too.
    real = wbpp_root / "IC4604" / "PANEL_1-1" / "SESSION_1" / "notes.txt"
    touch(real)

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit):
        build_wbpp_sessions(rows, backend=backend, output=archive, wbpp_root=wbpp_root,
                             target_name="IC 4604", overwrite=True)
    assert real.exists()
