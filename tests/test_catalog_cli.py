"""Tests for `darkroom catalog sites ...` / `backfill-sites` (S1 Phase 3)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from astropy.io import fits

from darkroom.catalog_cli import (
    _backfill_sites_run,
    _sites_add_run,
    _sites_list_run,
    _sites_set_run,
    add_subparser,
)
from darkroom.catalog_client import LocalBackend
from darkroom.cataloger import init_db, upsert_session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _session(
    session_id,
    target="M 81",
    obs_date="2026-02-19",
    lights_path=None,
    site_lat=None,
    site_lon=None,
    **extra,
) -> dict:
    base = {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": 100,
        "total_integration_sec": 18000,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": (
            lights_path
            if lights_path is not None
            else f"01_Deep Sky Objects/{target}/{obs_date}_FRA400_ZWOASI585MCPro/Lights/L-Pro"
        ),
        "notes": "",
        "site_lat": site_lat,
        "site_lon": site_lon,
    }
    base.update(extra)
    return base


def _make_fits(path: Path, sitelat=None, sitelong=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU()
    hdu.header["OBJECT"] = "M 81"
    hdu.header["DATE-OBS"] = "2026-02-19T22:00:00"
    hdu.header["EXPOSURE"] = 180.0
    if sitelat is not None:
        hdu.header["SITELAT"] = sitelat
    if sitelong is not None:
        hdu.header["SITELONG"] = sitelong
    hdu.writeto(path, overwrite=True)
    return path


def _args(catalog, **kw) -> argparse.Namespace:
    base = dict(catalog=str(catalog), catalog_url=None, api_token=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _add_args(catalog, name, lat, lon, radius_m=1000.0, bortle=None, sqm=None, home=False):
    """Mirrors the `sites add` subparser's real defaults (radius_m=1000.0)."""
    return _args(
        catalog, name=name, lat=lat, lon=lon, radius_m=radius_m,
        bortle=bortle, sqm=sqm, home=home,
    )


def _set_args(catalog, name, new_name=None, lat=None, lon=None, radius_m=None, bortle=None, sqm=None, home=False):
    """Mirrors the `sites set` subparser: every value flag defaults to None
    (not provided) except --home, which is store_true."""
    return _args(
        catalog, name=name, new_name=new_name, lat=lat, lon=lon, radius_m=radius_m,
        bortle=bortle, sqm=sqm, home=home,
    )


def _backfill_args(catalog, archive, apply=False):
    return _args(catalog, archive=str(archive), apply=apply)


# ---------------------------------------------------------------------------
# sites add
# ---------------------------------------------------------------------------

def test_sites_add_happy_path(tmp_path, capsys):
    db = tmp_path / "cat.db"
    args = _add_args(db, "Home", lat=38.5245, lon=-8.8926, radius_m=1000.0, bortle=4, sqm=21.5, home=True)
    _sites_add_run(args)

    out = capsys.readouterr().out
    assert "added site 'Home'" in out

    rows = LocalBackend(db).list_sites()
    assert len(rows) == 1
    assert rows[0]["name"] == "Home"
    assert rows[0]["bortle"] == 4
    assert rows[0]["sqm"] == 21.5
    assert rows[0]["is_home"] == 1


def test_sites_add_duplicate_name_exits_1(tmp_path, capsys):
    db = tmp_path / "cat.db"
    _sites_add_run(_add_args(db, "Home", lat=38.5, lon=-8.8))

    with pytest.raises(SystemExit) as exc:
        _sites_add_run(_add_args(db, "Home", lat=39.0, lon=-9.0))

    assert "already exists" in str(exc.value)


# ---------------------------------------------------------------------------
# sites list
# ---------------------------------------------------------------------------

def test_sites_list_empty_hint(tmp_path, capsys):
    db = tmp_path / "cat.db"
    init_db(db)
    _sites_list_run(_args(db))

    out = capsys.readouterr().out
    assert "sites add" in out


def test_sites_list_with_sites_and_sessions(tmp_path, capsys):
    db = tmp_path / "cat.db"
    backend = LocalBackend(db)
    backend.add_site({"name": "Home", "lat": 38.5245, "lon": -8.8926, "radius_m": 500.0, "is_home": True})
    backend.add_site({"name": "DarkSite", "lat": 38.0, "lon": -8.0, "radius_m": 500.0, "sqm": 21.8, "bortle": 3})

    # Matches "Home" (within 500m).
    backend.upsert_session(_session("s1", site_lat=38.5246, site_lon=-8.8927))
    # Has GPS, but nowhere near any configured site -> unmatched.
    backend.upsert_session(_session("s2", site_lat=10.0, site_lon=10.0))
    # No GPS at all.
    backend.upsert_session(_session("s3"))

    _sites_list_run(_args(db))
    out = capsys.readouterr().out

    assert "Home (home)" in out
    assert "DarkSite" in out
    assert "1 sessions matched, 1 unmatched (GPS but no site in radius), 1 without GPS" in out
    assert "s2: 10.0000, 10.0000" in out


# ---------------------------------------------------------------------------
# sites set
# ---------------------------------------------------------------------------

def test_sites_set_sqm_and_bortle(tmp_path, capsys):
    db = tmp_path / "cat.db"
    backend = LocalBackend(db)
    backend.add_site({"name": "Home", "lat": 38.5, "lon": -8.8})

    _sites_set_run(_set_args(db, "Home", bortle=5, sqm=20.9))

    out = capsys.readouterr().out
    assert "updated site 'Home'" in out
    row = backend.list_sites()[0]
    assert row["bortle"] == 5
    assert row["sqm"] == 20.9


def test_sites_set_rename(tmp_path, capsys):
    db = tmp_path / "cat.db"
    backend = LocalBackend(db)
    backend.add_site({"name": "Home", "lat": 38.5, "lon": -8.8})

    _sites_set_run(_set_args(db, "Home", new_name="Backyard"))

    out = capsys.readouterr().out
    assert "renamed to 'Backyard'" in out
    names = {s["name"] for s in backend.list_sites()}
    assert names == {"Backyard"}


def test_sites_set_home_rehomes(tmp_path):
    db = tmp_path / "cat.db"
    backend = LocalBackend(db)
    backend.add_site({"name": "Home", "lat": 38.5, "lon": -8.8, "is_home": True})
    backend.add_site({"name": "DarkSite", "lat": 38.0, "lon": -8.0})

    _sites_set_run(_set_args(db, "DarkSite", home=True))

    sites = {s["name"]: s["is_home"] for s in backend.list_sites()}
    assert sites == {"Home": 0, "DarkSite": 1}


def test_sites_set_no_flags_exits_1(tmp_path, capsys):
    db = tmp_path / "cat.db"
    backend = LocalBackend(db)
    backend.add_site({"name": "Home", "lat": 38.5, "lon": -8.8})

    with pytest.raises(SystemExit) as exc:
        _sites_set_run(_set_args(db, "Home"))

    assert "nothing to update" in str(exc.value)


def test_sites_set_missing_site_exits_1(tmp_path, capsys):
    db = tmp_path / "cat.db"
    init_db(db)

    with pytest.raises(SystemExit) as exc:
        _sites_set_run(_set_args(db, "NoSuchSite", sqm=20.0))

    assert "not found" in str(exc.value)


# ---------------------------------------------------------------------------
# backfill-sites
# ---------------------------------------------------------------------------

def test_backfill_dry_run_prints_proposals_without_mutating(tmp_path, capsys):
    db = tmp_path / "cat.db"
    archive = tmp_path / "archive"
    init_db(db)

    lights_path = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    upsert_session(db, _session("s1", lights_path=lights_path))
    _make_fits(archive / lights_path / "Light_0001.fit", sitelat=38.5245, sitelong=-8.8926)

    _backfill_sites_run(_backfill_args(db, archive, apply=False))

    out = capsys.readouterr().out
    assert "s1: 38.5245, -8.8926 -> (no site in radius)" in out
    assert "1 would be set, 0 no site headers, 0 missing on disk; run with --apply to write" in out

    rows = LocalBackend(db).query_sessions(session_id="s1")
    assert rows[0]["site_lat"] is None
    assert rows[0]["site_lon"] is None


def test_backfill_apply_writes_coords_then_second_dry_run_reports_nothing(tmp_path, capsys):
    db = tmp_path / "cat.db"
    archive = tmp_path / "archive"
    init_db(db)

    lights_path = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    upsert_session(db, _session("s1", lights_path=lights_path))
    _make_fits(archive / lights_path / "Light_0001.fit", sitelat=38.5245, sitelong=-8.8926)

    _backfill_sites_run(_backfill_args(db, archive, apply=True))
    out = capsys.readouterr().out
    assert "1 set, 0 no site headers, 0 missing on disk" in out

    rows = LocalBackend(db).query_sessions(session_id="s1")
    assert rows[0]["site_lat"] == pytest.approx(38.5245)
    assert rows[0]["site_lon"] == pytest.approx(-8.8926)

    # Second dry run: the now-set session is no longer a candidate.
    _backfill_sites_run(_backfill_args(db, archive, apply=False))
    out2 = capsys.readouterr().out
    assert "0 would be set, 0 no site headers, 0 missing on disk; run with --apply to write" in out2


def test_backfill_skips_sessions_that_already_have_site_lat(tmp_path, capsys):
    db = tmp_path / "cat.db"
    archive = tmp_path / "archive"
    init_db(db)

    lights_path = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    upsert_session(db, _session("s1", lights_path=lights_path, site_lat=1.0, site_lon=2.0))
    # Headers would resolve to a different value entirely -- must not be touched.
    _make_fits(archive / lights_path / "Light_0001.fit", sitelat=38.5245, sitelong=-8.8926)

    _backfill_sites_run(_backfill_args(db, archive, apply=True))
    out = capsys.readouterr().out
    assert "0 set" in out

    rows = LocalBackend(db).query_sessions(session_id="s1")
    assert rows[0]["site_lat"] == pytest.approx(1.0)
    assert rows[0]["site_lon"] == pytest.approx(2.0)


def test_backfill_tallies_no_headers_and_missing_folder_non_fatally(tmp_path, capsys):
    db = tmp_path / "cat.db"
    archive = tmp_path / "archive"
    init_db(db)

    no_header_path = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    upsert_session(db, _session("s1", lights_path=no_header_path))
    _make_fits(archive / no_header_path / "Light_0001.fit")  # no SITELAT/SITELONG

    missing_path = "01_Deep Sky Objects/M 82/2026-02-20_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    upsert_session(db, _session("s2", target="M 82", obs_date="2026-02-20", lights_path=missing_path))
    # Deliberately never create the folder for s2.

    _backfill_sites_run(_backfill_args(db, archive, apply=False))
    out = capsys.readouterr().out
    assert "0 would be set, 1 no site headers, 1 missing on disk; run with --apply to write" in out

    rows = LocalBackend(db).query_sessions()
    for row in rows:
        assert row["site_lat"] is None


def test_backfill_no_archive_exits_1(tmp_path, monkeypatch):
    db = tmp_path / "cat.db"
    init_db(db)
    monkeypatch.delenv("DARKROOM_ARCHIVE", raising=False)
    monkeypatch.setattr("darkroom.config.find_toml", lambda: {})

    args = _args(db, archive=None, apply=False)
    with pytest.raises(SystemExit):
        _backfill_sites_run(args)


# ---------------------------------------------------------------------------
# argparse registration end-to-end
# ---------------------------------------------------------------------------

def test_argparse_registration_sites_add():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    add_subparser(sub)

    args = p.parse_args(["catalog", "sites", "add", "X", "38.5", "-8.8", "--home"])
    assert args.func is _sites_add_run
    assert args.name == "X"
    assert args.lat == pytest.approx(38.5)
    assert args.lon == pytest.approx(-8.8)
    assert isinstance(args.lat, float)
    assert args.home is True
    assert args.radius_m == pytest.approx(1000.0)


def test_argparse_registration_backfill_sites():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    add_subparser(sub)

    args = p.parse_args(["catalog", "backfill-sites", "--archive", "/tmp/x", "--apply"])
    assert args.func is _backfill_sites_run
    assert args.archive == "/tmp/x"
    assert args.apply is True
