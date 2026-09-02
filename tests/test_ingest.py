import os
import tempfile
import tomllib
from pathlib import Path
import pytest


# Import functions that will exist after implementation
from darkroom.ingest import (
    camera_slug,
    session_dest_rel,
    cal_dest_rel,
    _manifest_dest,
)
from darkroom.config import find_toml, resolve_path


def test_camera_slug():
    assert camera_slug("ZWO ASI585MC Pro") == "ZWOASI585MCPro"
    assert camera_slug("Canon6D") == "Canon6D"


def test_session_dest_rel():
    result = session_dest_rel("M 81", "2026-02-19", "FRA400", "ZWO ASI585MC Pro", "L-Pro")
    assert result == Path("01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro")


def test_session_dest_rel_no_filter():
    result = session_dest_rel("M 51", "2026-02-28", "FRA400", "ZWO ASI585MC Pro", None)
    assert result == Path("01_Deep Sky Objects/M 51/2026-02-28_FRA400_ZWOASI585MCPro/Lights/NoFilter")


def test_cal_dest_rel_flat():
    result = cal_dest_rel("Flat", "ZWO ASI585MC Pro", "FRA400", "L-Pro", "2026-02-20")
    assert result == Path("00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20")


def test_cal_dest_rel_flat_no_filter():
    result = cal_dest_rel("Flat", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-20")
    assert result == Path("00_Calibration/Flats/FRA400_ZWOASI585MCPro_NoFilter/2026-02-20")


def test_cal_dest_rel_dark():
    result = cal_dest_rel("Dark", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-20")
    assert result == Path("00_Calibration/Darks/ZWOASI585MCPro")


def test_cal_dest_rel_flatdark():
    result = cal_dest_rel("FlatDark", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-21")
    assert result == Path("00_Calibration/FlatDarks/ZWOASI585MCPro")


def test_cal_dest_rel_bias():
    result = cal_dest_rel("Bias", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-21")
    assert result == Path("00_Calibration/Bias/ZWOASI585MCPro/Raw")


def test_manifest_dest_appends_yaml_when_no_extension():
    dest, warning = _manifest_dest("run")
    assert dest == Path("run.yaml")
    assert warning is None


def test_manifest_dest_warns_on_json():
    dest, warning = _manifest_dest("manifest.json")
    assert dest == Path("manifest.json")
    assert warning is not None and "YAML" in warning


def test_manifest_dest_keeps_yaml_extension():
    dest, warning = _manifest_dest("run.yaml")
    assert dest == Path("run.yaml")
    assert warning is None


def test_manifest_dest_preserves_path_and_dotted_dirs():
    # extension defaulting must not clobber a real path
    dest, warning = _manifest_dest("/tmp/out/run")
    assert dest == Path("/tmp/out/run.yaml")
    assert warning is None


def test_find_toml_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_toml() == {}


def test_find_toml_reads_flat_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "darkroom.toml").write_text(
        'archive_path = "/staging"\ncatalog_path = "/catalog.db"\n'
    )
    assert find_toml()["archive_path"] == "/staging"


def test_find_toml_reads_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "darkroom.toml").write_text(
        '[darkroom]\narchive_path = "/staging"\n'
    )
    assert find_toml()["archive_path"] == "/staging"


def test_resolve_path_from_cli():
    assert resolve_path("/from/cli", "DARKROOM_ARCHIVE", "archive_path") == Path("/from/cli")


def test_resolve_path_from_env(monkeypatch):
    monkeypatch.setenv("DARKROOM_ARCHIVE", "/from/env")
    assert resolve_path(None, "DARKROOM_ARCHIVE", "archive_path") == Path("/from/env")


def test_resolve_path_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKROOM_ARCHIVE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "darkroom.toml").write_text('archive_path = "/from/toml"\n')
    assert resolve_path(None, "DARKROOM_ARCHIVE", "archive_path") == Path("/from/toml")


def test_resolve_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKROOM_ARCHIVE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_path(None, "DARKROOM_ARCHIVE", "archive_path") is None


from darkroom.ingest import resolve_filter, KNOWN_FILTERS


def test_resolve_filter_known():
    # When filter is already detected, return it unchanged
    assert resolve_filter("L-Pro", interactive=False) == ("L-Pro", False)
    assert resolve_filter("L-Extreme", interactive=False) == ("L-Extreme", False)


def test_resolve_filter_non_interactive_unknown():
    # No TTY: return NoFilter with needs_review=True
    result = resolve_filter(None, interactive=False)
    assert result == ("NoFilter", True)


def test_resolve_filter_interactive_chooses_from_list(monkeypatch):
    # Simulate user entering "1" to choose L-Pro
    monkeypatch.setattr("builtins.input", lambda _: "1")
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == KNOWN_FILTERS[0]
    assert needs_review is False


def test_resolve_filter_interactive_empty_input_gives_nofilter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == "NoFilter"
    assert needs_review is False


def test_resolve_filter_interactive_manual_entry(monkeypatch):
    inputs = iter([str(len(KNOWN_FILTERS) + 1), "AstronomikL2"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == "AstronomikL2"
    assert needs_review is False


from darkroom.ingest import build_session_entry, existing_catalog_sessions, make_cal_set_id
from darkroom.scanner import Session


def _make_session(filter_="L-Pro", n_files=3) -> Session:
    with tempfile.TemporaryDirectory() as tmpdir:
        files = []
        for i in range(n_files):
            f = Path(tmpdir) / f"Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220{i:02d}00_-20.0C_L-Pro_{i+1:04d}.fit"
            f.touch()
            files.append(f)
        return Session(
            target="M 81", obs_date="2026-02-19", ota="FRA400",
            camera="ZWO ASI585MC Pro", filter=filter_, gain=200,
            temperature_c=-20.0, exposure_sec=180.0, focal_length=400.0,
            ra_deg=148.888, dec_deg=69.065, files=files,
        )


def test_build_session_entry_new():
    session = _make_session()
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=False)

    assert entry["session_id"] == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    assert entry["status"] == "new"
    assert entry["needs_review"] is False
    assert entry["filter"] == "L-Pro"
    assert entry["frame_count"] == 3
    assert len(entry["files"]) == 3
    assert all(f["copy"] is True for f in entry["files"])
    assert entry["lights_rel_path"] == "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"


def test_build_session_entry_existing_same_count():
    session = _make_session()
    output = Path("/staging")
    catalog = {"M81_20260219_FRA400_ZWOASI585MCPro_L-Pro": 3}
    entry = build_session_entry(session, output, catalog_sessions=catalog, interactive=False)

    assert entry["status"] == "existing"
    # Every frame is listed even when nothing is due to be copied, so that
    # `ingest review` can rebuild the copy plan if an identity edit changes the
    # session_id. `cmd_commit` skips "existing" entries wholesale regardless.
    assert len(entry["files"]) == 3
    assert all(f["copy"] is False for f in entry["files"])


def test_build_session_entry_no_filter_non_interactive():
    session = _make_session(filter_=None)
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=False)

    assert entry["needs_review"] is True
    assert entry["filter"] is None
    assert "UnknownFilter" in entry["session_id"]


def test_build_session_entry_no_filter_interactive(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "1")  # choose L-Pro
    session = _make_session(filter_=None)
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=True)

    assert entry["needs_review"] is False
    assert entry["filter"] == "L-Pro"
    assert entry["session_id"].endswith("_L-Pro")


def test_existing_catalog_sessions_empty_when_no_db(tmp_path):
    result = existing_catalog_sessions(tmp_path / "nonexistent.db")
    assert result == {}


def test_make_cal_set_id():
    result = make_cal_set_id("Flat", "ZWO ASI585MC Pro", 200, 1.35, -20.0, "2026-02-20")
    assert result == "Flat_ZWOASI585MCPro_1.35s_200g_-20C_2026-02-20"


def test_make_cal_set_id_dslr_uses_iso():
    # R3: every Canon6D set in the catalog carries the ISO form; ingest used
    # to write "1600g", so the same physical set got two ids.
    assert (
        make_cal_set_id("Flat", "Canon EOS 6D", 1600, 0.004, 15.0, "2026-02-20")
        == "Flat_Canon6D_0.004s_ISO1600_15C_2026-02-20"
    )
    assert make_cal_set_id("Bias", "Canon6D", 0, 0.0001, 15.0, "2026-02-20").startswith(
        "Bias_Canon6D_0.0001s_ISOAuto_"
    )


from darkroom.ingest import build_cal_entry
from darkroom.scanner import CalibrationGroup


def _make_cal_group(frame_type="Flat", filter_="L-Pro", n_files=2) -> CalibrationGroup:
    with tempfile.TemporaryDirectory() as tmpdir:
        files = []
        for i in range(n_files):
            f = Path(tmpdir) / f"Flat_1.35s_Bin1_585MC_gain200_20260220-09{i:02d}00_-20.0C_L-Pro_{i+1:04d}.fit"
            f.touch()
            files.append(f)
        return CalibrationGroup(
            frame_type=frame_type, camera="ZWO ASI585MC Pro", ota="FRA400",
            filter=filter_, gain=200, exposure_sec=1.35, temperature_c=-20.0,
            capture_date="2026-02-20", files=files,
        )


def test_build_cal_entry_flat_all_new(tmp_path):
    group = _make_cal_group()
    entry = build_cal_entry(group, output=tmp_path, interactive=False)

    assert entry["set_id"] == "Flat_ZWOASI585MCPro_1.35s_200g_-20C_2026-02-20"
    assert entry["frame_type"] == "Flat"
    assert entry["filter"] == "L-Pro"
    assert entry["needs_review"] is False
    assert entry["folder_rel_path"] == "00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20"
    assert len(entry["files"]) == 2
    assert all(f["copy"] is True for f in entry["files"])


def test_build_cal_entry_files_already_at_dest(tmp_path):
    group = _make_cal_group(n_files=1)
    dest_dir = tmp_path / "00_Calibration" / "Flats" / "FRA400_ZWOASI585MCPro_L-Pro" / "2026-02-20"
    dest_dir.mkdir(parents=True)
    # Pre-create the file at destination
    (dest_dir / group.files[0].name).touch()

    entry = build_cal_entry(group, output=tmp_path, interactive=False)

    assert len(entry["files"]) == 1
    assert entry["files"][0]["copy"] is False


def test_build_cal_entry_dark_no_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        group = CalibrationGroup(
            frame_type="Dark", camera="ZWO ASI585MC Pro", ota="FRA400",
            filter=None, gain=200, exposure_sec=180.0, temperature_c=-20.0,
            capture_date="2026-02-20",
            files=[Path(tmpdir) / "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001.fit"],
        )
        group.files[0].touch()
        entry = build_cal_entry(group, output=Path(tmpdir) / "out", interactive=False)

    assert entry["needs_review"] is False
    assert entry["folder_rel_path"] == "00_Calibration/Darks/ZWOASI585MCPro"


# ---------------------------------------------------------------------------
# Flat filter inference from Light sessions
# ---------------------------------------------------------------------------

from darkroom.ingest import infer_flat_filter
from darkroom.scanner import Session


def _make_light_session(obs_date="2026-06-16", filter_="L-Extreme", camera="ZWOASI585MCPro", ota="FRA400"):
    return Session(
        target="NGC 6992", obs_date=obs_date, ota=ota, camera=camera,
        filter=filter_, gain=200, temperature_c=-10.0, exposure_sec=180.0,
        focal_length=400.0, ra_deg=None, dec_deg=None, files=[],
    )


def test_infer_flat_filter_single_match():
    """Flat taken morning after imaging → infer from the single matching session."""
    sessions = [_make_light_session(obs_date="2026-06-16", filter_="L-Extreme")]
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    assert infer_flat_filter(group, sessions) == ["L-Extreme"]


def test_infer_flat_filter_same_day():
    """Flat taken same day as imaging → still matches."""
    sessions = [_make_light_session(obs_date="2026-06-16", filter_="L-Synergy")]
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-16"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    assert infer_flat_filter(group, sessions) == ["L-Synergy"]


def test_infer_flat_filter_multiple_candidates():
    """Two filters on the same night → returns both sorted."""
    sessions = [
        _make_light_session(obs_date="2026-06-16", filter_="L-Extreme"),
        _make_light_session(obs_date="2026-06-16", filter_="L-Synergy"),
    ]
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    assert infer_flat_filter(group, sessions) == ["L-Extreme", "L-Synergy"]


def test_infer_flat_filter_no_match_wrong_camera():
    """Camera mismatch → no candidates."""
    sessions = [_make_light_session(camera="Canon6D")]
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    assert infer_flat_filter(group, sessions) == []


def test_infer_flat_filter_no_match_too_far():
    """Session 2+ days before flat → no match."""
    sessions = [_make_light_session(obs_date="2026-06-14")]
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    assert infer_flat_filter(group, sessions) == []


def test_infer_flat_filter_skips_sessions_without_filter():
    """Sessions with filter=None are ignored."""
    sessions = [_make_light_session(filter_=None)]
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    assert infer_flat_filter(group, sessions) == []


def test_build_cal_entry_flat_infers_filter_from_sessions(tmp_path):
    """build_cal_entry uses session inference for filterless flats."""
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    sessions = [_make_light_session(obs_date="2026-06-16", filter_="L-Extreme")]
    entry = build_cal_entry(group, output=tmp_path, interactive=False, sessions=sessions)
    assert entry["filter"] == "L-Extreme"
    assert entry["needs_review"] is False
    assert "L-Extreme" in entry["folder_rel_path"]


def test_build_cal_entry_flat_ambiguous_non_interactive(tmp_path):
    """Multiple candidate filters in non-interactive mode → needs_review."""
    group = _make_cal_group(filter_=None)
    group.capture_date = "2026-06-17"
    group.camera = "ZWOASI585MCPro"
    group.ota = "FRA400"
    sessions = [
        _make_light_session(obs_date="2026-06-16", filter_="L-Extreme"),
        _make_light_session(obs_date="2026-06-16", filter_="L-Synergy"),
    ]
    entry = build_cal_entry(group, output=tmp_path, interactive=False, sessions=sessions)
    assert entry["needs_review"] is True


# ── plan_session_files ───────────────────────────────────────────────────────

from darkroom.ingest import plan_session_files


def test_plan_session_files_new_copies_everything(tmp_path):
    srcs = [Path("/card/b.fit"), Path("/card/a.fit")]
    status, files = plan_session_files(
        srcs, Path("dest"), tmp_path / "dest", "SID", catalog_sessions={},
    )
    assert status == "new"
    assert [f["dst"] for f in files] == ["dest/a.fit", "dest/b.fit"]  # sorted by src
    assert all(f["copy"] is True for f in files)


def test_plan_session_files_existing_lists_without_copying(tmp_path):
    srcs = [Path("/card/a.fit"), Path("/card/b.fit")]
    status, files = plan_session_files(
        srcs, Path("dest"), tmp_path / "dest", "SID", catalog_sessions={"SID": 2},
    )
    assert status == "existing"
    assert len(files) == 2
    assert all(f["copy"] is False for f in files)


def test_plan_session_files_topup_flags_only_missing_frames(tmp_path):
    dest_abs = tmp_path / "dest"
    dest_abs.mkdir()
    (dest_abs / "a.fit").write_text("")
    srcs = [Path("/card/a.fit"), Path("/card/b.fit")]

    status, files = plan_session_files(
        srcs, Path("dest"), dest_abs, "SID", catalog_sessions={"SID": 1},
    )
    assert status == "topup"
    assert {Path(f["src"]).name: f["copy"] for f in files} == {"a.fit": False, "b.fit": True}


def test_plan_session_files_topup_with_no_dest_dir_copies_all(tmp_path):
    srcs = [Path("/card/a.fit"), Path("/card/b.fit")]
    status, files = plan_session_files(
        srcs, Path("dest"), tmp_path / "nope", "SID", catalog_sessions={"SID": 1},
    )
    assert status == "topup"
    assert all(f["copy"] is True for f in files)


# ── cmd_commit ───────────────────────────────────────────────────────────────

import argparse

import yaml as _yaml

from darkroom.ingest import cmd_commit


def _commit_manifest(tmp_path, entries, cal=()):
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    path = tmp_path / "m.yaml"
    path.write_text(_yaml.dump({
        "meta": {"asiair": "/card", "archive": str(archive),
                 "catalog": str(catalog), "generated": "x"},
        "sessions": list(entries),
        "calibration": list(cal),
    }))
    return path, archive, catalog


def _committable_session(src_dir, names, *, status="new", copy=True):
    src_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (src_dir / n).write_text(n)
    dest = "01_Deep Sky Objects/M 81/2026-06-21_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    return {
        "session_id": "M81_20260621_FRA400_ZWOASI585MCPro_L-Pro",
        "target": "M 81", "obs_date": "2026-06-21", "ota": "FRA400",
        "camera": "ZWOASI585MCPro", "filter": "L-Pro", "gain": 200,
        "temperature_c": -20.0, "exposure_sec": 180.0, "focal_length": 400.0,
        "frame_count": len(names), "needs_review": False, "status": status,
        "lights_rel_path": dest,
        "files": [
            {"src": str(src_dir / n), "dst": f"{dest}/{n}", "copy": copy} for n in names
        ],
    }


def test_cmd_commit_copies_flagged_files(tmp_path, capsys):
    entry = _committable_session(tmp_path / "card", ["a.fit", "b.fit"])
    path, archive, _ = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    assert (archive / entry["lights_rel_path"] / "a.fit").exists()
    assert (archive / entry["lights_rel_path"] / "b.fit").exists()
    assert "2 files copied" in capsys.readouterr().out


def test_cmd_commit_skips_existing_entries_despite_listed_files(tmp_path, capsys):
    """The U3 manifest lists every frame even for 'existing' sessions.

    They carry copy: False and the entry is skipped wholesale, so listing them
    must not cause a re-copy.
    """
    entry = _committable_session(
        tmp_path / "card", ["a.fit", "b.fit"], status="existing", copy=False,
    )
    path, archive, _ = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    assert not (archive / entry["lights_rel_path"]).exists()
    assert "0 files copied" in capsys.readouterr().out


def test_cmd_commit_topup_copies_only_flagged_frames(tmp_path):
    entry = _committable_session(tmp_path / "card", ["a.fit", "b.fit"], status="topup")
    entry["files"][0]["copy"] = False
    path, archive, _ = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    dest = archive / entry["lights_rel_path"]
    assert not (dest / "a.fit").exists()
    assert (dest / "b.fit").exists()


def test_cmd_commit_refuses_unresolved_needs_review(tmp_path, capsys):
    entry = _committable_session(tmp_path / "card", ["a.fit"])
    entry["needs_review"] = True
    entry["filter"] = None
    path, _, _ = _commit_manifest(tmp_path, [entry])

    with pytest.raises(SystemExit) as exc:
        cmd_commit(argparse.Namespace(manifest=str(path)))

    assert exc.value.code == 1
    assert "unresolved needs_review" in capsys.readouterr().err


def test_cmd_commit_registers_sessions_in_the_catalog(tmp_path):
    from darkroom.catalog_client import LocalBackend

    entry = _committable_session(tmp_path / "card", ["a.fit"])
    path, _, catalog = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    rows = LocalBackend(catalog).query_sessions()
    assert [r["session_id"] for r in rows] == [entry["session_id"]]
    assert rows[0]["lights_path"] == entry["lights_rel_path"]


def test_existing_catalog_sessions_empty_db_file_has_no_schema(tmp_path):
    """An existing-but-unmigrated db means 'nothing known', not a traceback.

    sqlite creates an empty file on connect, so the catalog path can exist
    without the schema. This runs on the unattended CCC postflight path.
    """
    import sqlite3 as _sqlite3

    db = tmp_path / "empty.db"
    _sqlite3.connect(db).close()
    assert db.exists()

    assert existing_catalog_sessions(db) == {}


def test_existing_catalog_sessions_unreadable_file(tmp_path):
    db = tmp_path / "not-a.db"
    db.write_text("this is not a sqlite database")
    assert existing_catalog_sessions(db) == {}


# ── notes protection on re-upsert ────────────────────────────────────────────

def test_upsert_session_preserves_notes_against_an_empty_incoming_note(tmp_path):
    """A re-ingest must not destroy what was written about a night.

    ingest always sends notes="" (it has none to contribute), and a session
    only has to *look* new for commit to upsert it — so without this an
    already-catalogued session lost its notes silently.
    """
    from darkroom.cataloger import init_db, upsert_session
    from darkroom.catalog_client import LocalBackend

    db = tmp_path / "cat.db"
    init_db(db)
    base = {
        "session_id": "S1", "target": "M 81", "obs_date": "2026-06-21",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 4, "total_integration_sec": 720,
        "ra_deg": None, "dec_deg": None, "lights_path": "p",
    }
    upsert_session(db, {**base, "notes": "guiding poor after 01:00"})
    upsert_session(db, {**base, "notes": ""})           # the re-ingest

    rows = LocalBackend(db).query_sessions()
    assert rows[0]["notes"] == "guiding poor after 01:00"


def test_upsert_session_still_accepts_a_real_note(tmp_path):
    from darkroom.cataloger import init_db, upsert_session
    from darkroom.catalog_client import LocalBackend

    db = tmp_path / "cat.db"
    init_db(db)
    base = {
        "session_id": "S1", "target": "M 81", "obs_date": "2026-06-21",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 4, "total_integration_sec": 720,
        "ra_deg": None, "dec_deg": None, "lights_path": "p",
    }
    upsert_session(db, {**base, "notes": "first"})
    upsert_session(db, {**base, "notes": "second"})

    assert LocalBackend(db).query_sessions()[0]["notes"] == "second"


def test_upsert_session_notes_key_is_optional(tmp_path):
    from darkroom.cataloger import init_db, upsert_session

    db = tmp_path / "cat.db"
    init_db(db)
    upsert_session(db, {
        "session_id": "S1", "target": "M 81", "obs_date": "2026-06-21",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 4, "total_integration_sec": 720,
        "ra_deg": None, "dec_deg": None, "lights_path": "p",
    })  # no "notes" key at all


# ── resolve_catalog_sessions ─────────────────────────────────────────────────

from darkroom.ingest import catalog_frame_counts, resolve_catalog_sessions


def test_catalog_frame_counts():
    rows = [{"session_id": "A", "frame_count": 12}, {"session_id": "B", "frame_count": None}]
    assert catalog_frame_counts(rows) == {"A": 12, "B": 0}


def test_resolve_catalog_sessions_local_reads_the_file(tmp_path):
    from darkroom.cataloger import init_db, upsert_session

    db = tmp_path / "cat.db"
    init_db(db)
    upsert_session(db, {
        "session_id": "S1", "target": "M 81", "obs_date": "2026-06-21",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 4, "total_integration_sec": 720,
        "ra_deg": None, "dec_deg": None, "lights_path": "p", "notes": "",
    })
    counts, verified = resolve_catalog_sessions(db)
    assert counts == {"S1": 4}
    assert verified is True


def test_resolve_catalog_sessions_local_never_creates_the_db(tmp_path):
    """A scan must stay read-only on the catalog (LocalBackend would create it)."""
    db = tmp_path / "nope.db"
    counts, verified = resolve_catalog_sessions(db)

    assert (counts, verified) == ({}, True)
    assert not db.exists()


def test_resolve_catalog_sessions_uses_the_server_when_configured(tmp_path, monkeypatch):
    """With a catalog_url set, the verdict must come from the server, not sqlite."""
    monkeypatch.setenv("DARKROOM_CATALOG_URL", "http://catalog.invalid")

    class FakeBackend:
        def query_sessions(self):
            return [{"session_id": "REMOTE", "frame_count": 7}]

    monkeypatch.setattr("darkroom.ingest.resolve_backend", lambda _: FakeBackend())

    counts, verified = resolve_catalog_sessions(tmp_path / "ignored.db")
    assert counts == {"REMOTE": 7}
    assert verified is True


def test_resolve_catalog_sessions_unreachable_server_degrades(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DARKROOM_CATALOG_URL", "http://catalog.invalid")

    def boom(_):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("darkroom.ingest.resolve_backend", boom)

    counts, verified = resolve_catalog_sessions(tmp_path / "ignored.db")
    assert counts == {}
    assert verified is False
    assert "catalog server unreachable" in capsys.readouterr().err


def test_cmd_commit_warns_on_an_unverified_manifest(tmp_path, capsys):
    entry = _committable_session(tmp_path / "card", ["a.fit"])
    path, _, _ = _commit_manifest(tmp_path, [entry])
    m = _yaml.safe_load(path.read_text())
    m["meta"]["status_verified"] = False
    path.write_text(_yaml.dump(m))

    cmd_commit(argparse.Namespace(manifest=str(path)))

    assert "statuses are unverified" in capsys.readouterr().err


# ── catalog provenance line ──────────────────────────────────────────────────

from darkroom.ingest import catalog_label, report_catalog


def test_catalog_label_local(tmp_path):
    assert catalog_label(tmp_path / "cat.db") == f"{tmp_path / 'cat.db'} (local file)"


def test_catalog_label_server(monkeypatch, tmp_path):
    monkeypatch.setenv("DARKROOM_CATALOG_URL", "https://darkroom.example.net")
    assert catalog_label(tmp_path / "cat.db") == "https://darkroom.example.net (server)"


def test_report_catalog_goes_to_stderr(tmp_path, capsys):
    """stdout carries the manifest in `scan` dry-run mode, so this must not."""
    report_catalog(tmp_path / "cat.db")
    captured = capsys.readouterr()
    assert "Catalog:" in captured.err
    assert captured.out == ""


def test_cmd_scan_dry_run_stdout_stays_valid_yaml(tmp_path, capsys, monkeypatch):
    """Regression guard: the provenance line must not corrupt the manifest."""
    import argparse as _argparse

    from darkroom.ingest import cmd_scan

    card = tmp_path / "card" / "Light" / "M 81"
    card.mkdir(parents=True)
    monkeypatch.setattr("darkroom.ingest.scan_source",
                        lambda _: __import__("darkroom.scanner", fromlist=["ScanResult"]).ScanResult())

    cmd_scan(
        _argparse.Namespace(
            asiair=str(tmp_path / "card"), archive=str(tmp_path / "nas"),
            catalog=str(tmp_path / "cat.db"), manifest=None,
        ),
        write_file=False,
    )
    captured = capsys.readouterr()

    assert "Catalog:" in captured.err
    parsed = _yaml.safe_load(captured.out)          # must not raise
    assert parsed["meta"]["archive"] == str(tmp_path / "nas")


def test_cmd_commit_reports_the_catalog(tmp_path, capsys):
    entry = _committable_session(tmp_path / "card", ["a.fit"])
    path, _, catalog = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    assert f"Catalog: {catalog} (local file)" in capsys.readouterr().err


# ── F4: session wall-clock span through the manifest and into the catalog ────

def test_build_session_entry_carries_the_session_span():
    session = _make_session()
    session.start_utc = "2026-02-19T22:00:00"
    session.end_utc = "2026-02-19T23:03:00"

    entry = build_session_entry(session, Path("/staging"), catalog_sessions={}, interactive=False)

    assert entry["start_utc"] == "2026-02-19T22:00:00"
    assert entry["end_utc"] == "2026-02-19T23:03:00"


def test_build_session_entry_span_is_none_when_the_scan_found_none():
    entry = build_session_entry(
        _make_session(), Path("/staging"), catalog_sessions={}, interactive=False
    )
    assert entry["start_utc"] is None
    assert entry["end_utc"] is None


def test_cmd_commit_registers_the_session_span(tmp_path):
    from darkroom.catalog_client import LocalBackend

    entry = _committable_session(tmp_path / "card", ["a.fit"])
    entry["start_utc"] = "2026-06-21T22:00:00"
    entry["end_utc"] = "2026-06-22T02:30:00"
    path, _, catalog = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    rows = LocalBackend(catalog).query_sessions()
    assert rows[0]["start_utc"] == "2026-06-21T22:00:00"
    assert rows[0]["end_utc"] == "2026-06-22T02:30:00"


def test_cmd_commit_tolerates_a_manifest_without_span_keys(tmp_path):
    """Manifests written before F4 must still commit (span lands NULL)."""
    from darkroom.catalog_client import LocalBackend

    entry = _committable_session(tmp_path / "card", ["a.fit"])
    assert "start_utc" not in entry
    path, _, catalog = _commit_manifest(tmp_path, [entry])

    cmd_commit(argparse.Namespace(manifest=str(path)))

    rows = LocalBackend(catalog).query_sessions()
    assert rows[0]["start_utc"] is None
