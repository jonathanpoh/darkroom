"""Tests for darkroom.rescan (F8) — archive-vs-catalog diff and review-queue push."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from astropy.io import fits

from darkroom.catalog_cli import _rescan_archive_run
from darkroom.catalog_client import LocalBackend
from darkroom.cataloger import init_db, upsert_session
from darkroom.rescan import (
    DEFAULT_POINTING_TOLERANCE_DEG,
    ArchiveRootMissing,
    EmptyDiskDivergence,
    apply,
    scan,
)

DSO = "01_Deep Sky Objects"


def _write_light(
    path: Path,
    *,
    obj: str = "M 81",
    date_obs: str = "2026-02-19T22:00:00",
    exposure: float = 180.0,
    camera: str = "ZWOASI585MCPro",
    focallen: float = 400.0,
    gain: int = 200,
    temp: float = -20.0,
    ra: float = 148.89,
    dec: float = 69.07,
    filt: str = "L-Pro",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU()
    h = hdu.header
    h["OBJECT"] = obj
    h["DATE-OBS"] = date_obs
    h["EXPOSURE"] = exposure
    h["INSTRUME"] = camera
    h["FOCALLEN"] = focallen
    h["GAIN"] = gain
    h["CCD-TEMP"] = temp
    h["RA"] = ra
    h["DEC"] = dec
    h["FILTER"] = filt
    hdu.writeto(path, overwrite=True)


def _lights_dir(archive: Path, target: str, obs_date: str) -> Path:
    # Filenames are deliberately just a sequence number ("0001.fit") — a
    # single-part stem short-circuits parse.parse_filter() to None, so the
    # FILTER header (not filename guessing) decides the filter, keeping test
    # fixtures deterministic.
    return archive / DSO / target / f"{obs_date}_FRA400_ZWOASI585MCPro" / "Lights" / "L-Pro"


def _write_session_frames(
    archive: Path, *, target: str = "M 81", obs_date: str = "2026-02-19", n: int = 3, **overrides
) -> Path:
    lights = _lights_dir(archive, target, obs_date)
    hours = ["21:00:00", "22:00:00", "23:00:00", "23:30:00", "23:45:00"]
    for i in range(n):
        _write_light(
            lights / f"{i:04d}.fit",
            obj=target,
            date_obs=f"{obs_date}T{hours[i]}",
            **overrides,
        )
    return lights


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "cat.db"
    init_db(db)
    return db


class _FakeBackend:
    """Wraps a real LocalBackend for reads; records replace_rescan_proposals calls.

    catalog_client.py belongs to the other F8 agent (f8-web) and does not yet
    implement replace_rescan_proposals/list_rescan_proposals — this fake
    stands in for the store the contract describes, per F8_CONTRACT.md.
    """

    def __init__(self, db_path: Path):
        self._local = LocalBackend(db_path)
        self.replaced: list[list[dict]] = []

    def query_sessions(self, **kwargs):
        return self._local.query_sessions(**kwargs)

    def replace_rescan_proposals(self, proposals: list[dict]) -> int:
        self.replaced.append(proposals)
        return len(proposals)


def _only_create(archive: Path, backend) -> dict:
    """Scan a fresh archive/catalog pair and return the single 'create' proposal."""
    proposals = scan(archive, backend)
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "create"
    return proposals[0]


def _catalog_row_from_create(create_proposal: dict, session_id: str) -> dict:
    """Build a session dict to upsert from a 'create' proposal's changes."""
    row = {field: delta["proposed"] for field, delta in create_proposal["changes"].items()}
    row["session_id"] = session_id
    row["target"] = create_proposal["target"]
    row["obs_date"] = create_proposal["obs_date"]
    row["lights_path"] = create_proposal["lights_path"]
    row["notes"] = ""
    return row


# ── on-disk-and-matching ──────────────────────────────────────────────────

def test_matching_session_produces_no_proposal(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive)
    db = _db(tmp_path)
    backend = LocalBackend(db)

    create = _only_create(archive, backend)
    session_id = create["session_id"]
    upsert_session(db, _catalog_row_from_create(create, session_id))

    assert scan(archive, backend) == []


# ── on-disk-with-no-catalog-row ────────────────────────────────────────────

def test_disk_only_session_produces_create_proposal(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive)
    backend = LocalBackend(_db(tmp_path))

    create = _only_create(archive, backend)

    assert create["kind"] == "create"
    assert create["tier"] == "review"
    assert create["target"] == "M 81"
    assert create["obs_date"] == "2026-02-19"
    assert create["lights_path"] == str(
        _lights_dir(archive, "M 81", "2026-02-19").relative_to(archive)
    )
    # 'create' -> current is None for every field.
    assert all(delta["current"] is None for delta in create["changes"].values())
    assert create["changes"]["frame_count"]["proposed"] == 3
    assert create["changes"]["ra_deg"]["proposed"] == pytest.approx(148.89)


# ── in-catalog-with-lights_path-missing-on-disk ────────────────────────────

def test_catalog_only_session_produces_delete_proposal(tmp_path):
    archive = tmp_path / "archive"
    # A real, unrelated session on disk keeps this test decoupled from the
    # empty-disk guard (test_scan_raises_empty_disk_divergence_*) — it's
    # exercising a genuine per-session delete, not a whole-archive wipe.
    _write_session_frames(archive, target="NGC 7000", obs_date="2026-01-01")
    db = _db(tmp_path)
    row = {
        "session_id": "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        "target": "M 81",
        "obs_date": "2026-02-19",
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": 3,
        "total_integration_sec": 540,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": str(_lights_dir(archive, "M 81", "2026-02-19").relative_to(archive)),
        "notes": "",
    }
    upsert_session(db, row)
    backend = LocalBackend(db)

    proposals = scan(archive, backend)

    # Plus a 'create' for the unrelated NGC 7000 session, which is on disk
    # only — not the focus of this test, just a side effect of avoiding the
    # empty-disk guard.
    assert len(proposals) == 2
    p = next(p for p in proposals if p["kind"] == "delete")
    assert p["tier"] == "review"
    assert p["session_id"] == row["session_id"]
    assert p["target"] == "M 81"
    assert p["lights_path"] == row["lights_path"]
    # 'delete' -> proposed is None for every field, current carries the old value.
    assert all(delta["proposed"] is None for delta in p["changes"].values())
    assert p["changes"]["frame_count"]["current"] == 3
    assert p["changes"]["ra_deg"]["current"] == pytest.approx(148.89)


# ── on-disk-diverging: tiering ─────────────────────────────────────────────

def test_interior_frame_deletion_is_safe_tier_frame_count_only(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, n=3)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]
    upsert_session(db, _catalog_row_from_create(create, session_id))
    assert scan(archive, backend) == []

    # Delete the *middle* frame — start_utc/end_utc (min/max over survivors)
    # are unaffected, so only frame_count/total_integration_sec move (the F8
    # SH2-101 case this tiering rule was written for).
    lights = _lights_dir(archive, "M 81", "2026-02-19")
    (lights / "0001.fit").unlink()

    proposals = scan(archive, backend)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["session_id"] == session_id
    assert p["kind"] == "update"
    assert p["tier"] == "safe"
    assert set(p["changes"]) == {"frame_count", "total_integration_sec"}
    assert p["changes"]["frame_count"] == {"current": 3, "proposed": 2}
    assert p["changes"]["total_integration_sec"] == {"current": 540, "proposed": 360}


def test_pointing_divergence_is_review_tier(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, ra=10.0)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]
    row = _catalog_row_from_create(create, session_id)
    # Catalog disagrees with disk by more than the default 0.5deg tolerance.
    row["ra_deg"] = 10.0 + DEFAULT_POINTING_TOLERANCE_DEG + 0.1
    upsert_session(db, row)

    proposals = scan(archive, backend)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["kind"] == "update"
    assert p["tier"] == "review"
    assert "ra_deg" in p["changes"]
    assert p["changes"]["ra_deg"]["current"] == pytest.approx(10.6)
    assert p["changes"]["ra_deg"]["proposed"] == pytest.approx(10.0)


def test_mixed_safe_and_review_fields_stays_review_tier(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, n=3, temp=-20.0)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]
    row = _catalog_row_from_create(create, session_id)
    row["temperature_c"] = -15.0  # a review-tier field
    upsert_session(db, row)

    # Also drop a frame so a safe-tier field changes too.
    lights = _lights_dir(archive, "M 81", "2026-02-19")
    (lights / "0001.fit").unlink()

    proposals = scan(archive, backend)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["tier"] == "review"
    assert {"frame_count", "total_integration_sec", "temperature_c"} <= set(p["changes"])


# ── RA/Dec tolerance ────────────────────────────────────────────────────────

def test_ra_wraparound_at_360_is_not_a_divergence(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, ra=359.9)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]
    row = _catalog_row_from_create(create, session_id)
    # Naive |a-b| = 359.8; wrapped it's 0.2, well under the 0.5deg default.
    row["ra_deg"] = 0.1
    upsert_session(db, row)

    assert scan(archive, backend) == []


def test_ra_wraparound_at_360_beyond_tolerance_is_a_divergence(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, ra=359.5)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]
    row = _catalog_row_from_create(create, session_id)
    # Wrapped distance from 359.5 to 0.5 is 1.0deg, over the 0.5deg default.
    row["ra_deg"] = 0.5
    upsert_session(db, row)

    proposals = scan(archive, backend)

    assert len(proposals) == 1
    assert proposals[0]["changes"]["ra_deg"]["current"] == pytest.approx(0.5)


def test_pointing_tolerance_boundary(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, ra=10.0)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]

    # Just below tolerance (0.49 < 0.5): not reported.
    row_below = _catalog_row_from_create(create, session_id)
    row_below["ra_deg"] = 10.0 + 0.49
    upsert_session(db, row_below)
    assert scan(archive, backend, pointing_tolerance_deg=0.5) == []

    # Exactly at tolerance (0.5 >= 0.5): reported — "below tolerance" is the
    # only case the F8 contract excuses.
    row_at = _catalog_row_from_create(create, session_id)
    row_at["ra_deg"] = 10.5
    upsert_session(db, row_at)
    proposals = scan(archive, backend, pointing_tolerance_deg=0.5)
    assert len(proposals) == 1
    assert proposals[0]["changes"]["ra_deg"]["current"] == pytest.approx(10.5)


def test_pointing_tolerance_flag_is_configurable(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive, ra=10.0)
    db = _db(tmp_path)
    backend = LocalBackend(db)
    create = _only_create(archive, backend)
    session_id = create["session_id"]
    row = _catalog_row_from_create(create, session_id)
    row["ra_deg"] = 10.3
    upsert_session(db, row)

    # 0.3deg apart: divergence under the default 0.5 tolerance, but visible
    # once the caller tightens it to 0.1.
    assert scan(archive, backend, pointing_tolerance_deg=0.5) == []
    assert len(scan(archive, backend, pointing_tolerance_deg=0.1)) == 1


# ── apply() ──────────────────────────────────────────────────────────────

def test_apply_pushes_to_review_queue_and_never_touches_sessions(tmp_path):
    archive = tmp_path / "archive"
    _write_session_frames(archive)
    db = _db(tmp_path)
    fake = _FakeBackend(db)

    proposals = scan(archive, fake)
    written = apply(fake, proposals)

    assert written == len(proposals) == 1
    assert fake.replaced == [proposals]
    # No session row exists — apply() must not have created one via any
    # write path (there is no upsert/update call anywhere in rescan.apply).
    assert LocalBackend(db).query_sessions() == []


# ── CLI ──────────────────────────────────────────────────────────────────

def _cli_args(
    catalog, archive, *, apply_: bool, tolerance: float = 0.5, yes: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        catalog=str(catalog), catalog_url=None, api_token=None,
        archive=str(archive), apply=apply_, pointing_tolerance=tolerance, yes=yes,
    )


def test_cli_dry_run_prints_grouped_summary_and_writes_nothing(tmp_path, capsys):
    archive = tmp_path / "archive"
    _write_session_frames(archive)
    db = _db(tmp_path)

    _rescan_archive_run(_cli_args(db, archive, apply_=False))

    out = capsys.readouterr().out
    assert "M 81" in out
    assert "[create/review]" in out
    assert "run with --apply to push these to the review queue" in out
    assert "does NOT write to sessions" in out
    assert LocalBackend(db).query_sessions() == []


def test_cli_apply_pushes_proposals_via_backend(tmp_path, capsys, monkeypatch):
    archive = tmp_path / "archive"
    _write_session_frames(archive)
    db = _db(tmp_path)
    fake = _FakeBackend(db)
    monkeypatch.setattr("darkroom.catalog_cli.resolve_backend", lambda *a, **k: fake)

    _rescan_archive_run(_cli_args(db, archive, apply_=True))

    out = capsys.readouterr().out
    assert "Pushed 1 proposal(s)" in out
    assert len(fake.replaced) == 1
    assert fake.replaced[0][0]["kind"] == "create"


def test_cli_requires_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKROOM_ARCHIVE", raising=False)
    monkeypatch.setattr("darkroom.config.find_toml", lambda: {})
    db = _db(tmp_path)
    args = argparse.Namespace(
        catalog=str(db), catalog_url=None, api_token=None,
        archive=None, apply=False, pointing_tolerance=0.5, yes=False,
    )
    with pytest.raises(SystemExit):
        _rescan_archive_run(args)


# ── guard: missing DSO root ─────────────────────────────────────────────────

def test_scan_raises_when_dso_root_missing(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()  # exists, but no "01_Deep Sky Objects" subfolder
    backend = LocalBackend(_db(tmp_path))

    with pytest.raises(ArchiveRootMissing):
        scan(archive, backend)


def test_cli_refuses_when_dso_root_missing_no_prompt(tmp_path, capsys, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    db = _db(tmp_path)

    def _no_prompt(_prompt):
        raise AssertionError("must not prompt when the archive root is simply missing")

    monkeypatch.setattr("builtins.input", _no_prompt)

    with pytest.raises(SystemExit) as exc_info:
        _rescan_archive_run(_cli_args(db, archive, apply_=False))

    assert "archive DSO root not found" in str(exc_info.value)


# ── guard: empty disk, non-empty catalog ────────────────────────────────────

def test_scan_raises_empty_disk_divergence_when_catalog_nonempty(tmp_path):
    archive = tmp_path / "archive"
    (archive / DSO).mkdir(parents=True)  # root exists, walk finds nothing
    db = _db(tmp_path)
    row = {
        "session_id": "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 3, "total_integration_sec": 540,
        "ra_deg": 148.89, "dec_deg": 69.07,
        "lights_path": str(_lights_dir(archive, "M 81", "2026-02-19").relative_to(archive)),
        "notes": "",
    }
    upsert_session(db, row)
    backend = LocalBackend(db)

    with pytest.raises(EmptyDiskDivergence) as exc_info:
        scan(archive, backend)
    assert exc_info.value.catalog_session_count == 1


def test_scan_allow_empty_disk_proceeds_to_delete_proposals(tmp_path):
    archive = tmp_path / "archive"
    (archive / DSO).mkdir(parents=True)
    db = _db(tmp_path)
    row = {
        "session_id": "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 3, "total_integration_sec": 540,
        "ra_deg": 148.89, "dec_deg": 69.07,
        "lights_path": str(_lights_dir(archive, "M 81", "2026-02-19").relative_to(archive)),
        "notes": "",
    }
    upsert_session(db, row)
    backend = LocalBackend(db)

    proposals = scan(archive, backend, allow_empty_disk=True)

    assert len(proposals) == 1
    assert proposals[0]["kind"] == "delete"


def test_scan_empty_catalog_full_disk_is_first_run_not_a_divergence(tmp_path):
    """The asymmetric case: empty catalog + sessions on disk must NOT raise."""
    archive = tmp_path / "archive"
    _write_session_frames(archive)
    backend = LocalBackend(_db(tmp_path))  # catalog empty

    proposals = scan(archive, backend)  # must not raise

    assert len(proposals) == 1
    assert proposals[0]["kind"] == "create"


def _empty_disk_setup(tmp_path: Path) -> tuple[Path, Path]:
    archive = tmp_path / "archive"
    (archive / DSO).mkdir(parents=True)
    db = _db(tmp_path)
    row = {
        "session_id": "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
        "gain": 200, "temperature_c": -20.0, "exposure_sec": 180.0,
        "focal_length": 400.0, "frame_count": 3, "total_integration_sec": 540,
        "ra_deg": 148.89, "dec_deg": 69.07,
        "lights_path": str(_lights_dir(archive, "M 81", "2026-02-19").relative_to(archive)),
        "notes": "",
    }
    upsert_session(db, row)
    return archive, db


def test_cli_empty_disk_aborts_on_no(tmp_path, capsys, monkeypatch):
    archive, db = _empty_disk_setup(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")  # abort

    with pytest.raises(SystemExit):
        _rescan_archive_run(_cli_args(db, archive, apply_=False))

    err = capsys.readouterr().err
    assert "WARNING: 0 sessions found on disk" in err
    assert LocalBackend(db).query_sessions()  # catalog untouched


def test_cli_empty_disk_proceeds_on_yes_at_prompt(tmp_path, capsys, monkeypatch):
    archive, db = _empty_disk_setup(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")

    _rescan_archive_run(_cli_args(db, archive, apply_=False))

    out = capsys.readouterr().out
    assert "[delete/review]" in out


def test_cli_empty_disk_proceeds_with_yes_flag_no_prompt(tmp_path, capsys, monkeypatch):
    archive, db = _empty_disk_setup(tmp_path)

    def _no_prompt(_prompt):
        raise AssertionError("--yes must skip the prompt entirely")

    monkeypatch.setattr("builtins.input", _no_prompt)

    _rescan_archive_run(_cli_args(db, archive, apply_=False, yes=True))

    out = capsys.readouterr().out
    assert "[delete/review]" in out


def test_cli_empty_disk_refuses_without_tty_and_without_yes(tmp_path, capsys, monkeypatch):
    archive, db = _empty_disk_setup(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit):
        _rescan_archive_run(_cli_args(db, archive, apply_=False))

    err = capsys.readouterr().err
    assert "no TTY to confirm" in err
    assert LocalBackend(db).query_sessions()  # catalog untouched


# ── camera/exposure canonicalisation on the disk side ──────────────────────
#
# upsert_session canonicalizes `camera` and `exposure_sec` on write
# (names.normalize_session_fields), so a fresh scan has to compare against
# what would be STORED, not the raw FITS header values. Found on the live
# catalog: the first dry run proposed rewriting 209 of 231 sessions from the
# canonical 'Canon6D'/'ZWOASI585MCPro' back to the raw header spellings.

def test_raw_header_camera_name_is_not_a_divergence(tmp_path):
    """A raw INSTRUME of 'Canon EOS 6D' must match a stored 'Canon6D'."""
    archive = tmp_path / "archive"
    _write_session_frames(archive, camera="Canon EOS 6D")
    db = _db(tmp_path)
    backend = LocalBackend(db)

    create = _only_create(archive, backend)
    # The proposal itself reports the canonical form, not the header spelling.
    assert create["changes"]["camera"]["proposed"] == "Canon6D"

    upsert_session(db, _catalog_row_from_create(create, create["session_id"]))

    # Re-scanning the identical archive must now be a clean no-op.
    assert scan(archive, backend) == []


def test_sub_millisecond_exposure_noise_is_not_a_divergence(tmp_path):
    """EXPTIME 0.000125000005937181 is stored rounded to 0.0001 — not a change."""
    archive = tmp_path / "archive"
    _write_session_frames(archive, exposure=0.000125000005937181)
    db = _db(tmp_path)
    backend = LocalBackend(db)

    create = _only_create(archive, backend)
    assert create["changes"]["exposure_sec"]["proposed"] == pytest.approx(0.0001)

    upsert_session(db, _catalog_row_from_create(create, create["session_id"]))

    assert scan(archive, backend) == []


# ── rename pairing ─────────────────────────────────────────────────────────
#
# make_session_id only strips whitespace from the target; the disk-side scan
# applies _normalize_target as well. So a legacy row stored as 'SH2-101_...'
# and a fresh scan's 'Sh2-101_...' are the same night under two spellings.
# Surfacing that as delete + create would drop the row's id/created_at,
# processed_state and session_guiding row.

def _legacy_target_setup(tmp_path):
    """Archive holding one Sh2-101 night; catalog holding it under 'SH2-101'."""
    archive = tmp_path / "archive"
    _write_session_frames(archive, target="Sh2-101")
    db = _db(tmp_path)
    backend = LocalBackend(db)

    create = _only_create(archive, backend)
    row = _catalog_row_from_create(create, create["session_id"])
    # Store it under the legacy un-normalized spelling, as the live catalog does.
    row["target"] = "SH2-101"
    row["session_id"] = create["session_id"].replace("Sh2-101", "SH2-101", 1)
    upsert_session(db, row)
    return archive, db, backend, row["session_id"], create["session_id"]


def test_legacy_target_spelling_pairs_as_one_rename(tmp_path):
    archive, db, backend, old_id, new_id = _legacy_target_setup(tmp_path)

    proposals = scan(archive, backend)

    assert len(proposals) == 1, "must be one rename, not a delete + create pair"
    p = proposals[0]
    assert p["kind"] == "rename"
    assert p["tier"] == "review"
    # Keyed on the CATALOG-side id — that's what update_session_fields needs.
    assert p["session_id"] == old_id
    assert p["changes"]["target"] == {"current": "SH2-101", "proposed": "Sh2-101"}
    assert old_id != new_id


def test_rename_applies_in_place_preserving_row_identity(tmp_path):
    """Applying the rename must keep id/created_at, not delete-and-recreate."""
    from darkroom.catalog_db import apply_rescan_proposal, open_db

    archive, db, backend, old_id, new_id = _legacy_target_setup(tmp_path)
    proposal = scan(archive, backend)[0]

    with open_db(db) as conn:
        before = conn.execute(
            "SELECT id, created_at FROM sessions WHERE session_id = ?", (old_id,)
        ).fetchone()
        apply_rescan_proposal(conn, db, proposal)
        after = conn.execute(
            "SELECT id, created_at, target, session_id FROM sessions WHERE id = ?",
            (before["id"],),
        ).fetchone()

    assert after["target"] == "Sh2-101"
    assert after["session_id"] == new_id
    assert after["created_at"] == before["created_at"]
    # And the archive now agrees with the catalog.
    assert scan(archive, backend) == []


def test_ambiguous_rename_candidates_are_left_as_create_and_delete(tmp_path):
    """Two catalog rows collapsing to one canonical id must not be guessed at."""
    archive = tmp_path / "archive"
    _write_session_frames(archive, target="Sh2-101")
    db = _db(tmp_path)
    backend = LocalBackend(db)

    create = _only_create(archive, backend)
    for legacy in ("SH2-101", "sh2-101"):
        row = _catalog_row_from_create(create, create["session_id"])
        row["target"] = legacy
        row["session_id"] = create["session_id"].replace("Sh2-101", legacy, 1)
        upsert_session(db, row)

    kinds = sorted(p["kind"] for p in scan(archive, backend))

    assert "rename" not in kinds, "ambiguity must decline to guess"
    assert kinds == ["create", "delete", "delete"]


# ── rename pairing across filter canonicalisation (M2) ─────────────────────
#
# _filter_from_path's KNOWN_FILTERS guard means a stored filter that was never
# a real filter (a mosaic panel name) or was misspelled no longer round-trips
# from disk. Both sides have to be canonicalised or the row surfaces as an
# unrelated delete + create. Live: 9 such pairs on the real archive — the 8
# IC 4604 panels and one 'AstronimikL2' Moon session.

def _stored_under_filter(tmp_path, stored_filter: str):
    """One night whose folder name yields no filter; catalog holds a junk one.

    The frames carry no FILTER header and sit in a folder named for something
    that isn't a filter, so the disk side resolves to None (-> UnknownFilter)
    exactly as the live IC 4604 panels do. The catalog still holds the old
    non-canonical value.
    """
    archive = tmp_path / "archive"
    lights = (
        archive / DSO / "M 81" / "2026-02-19_FRA400_ZWOASI585MCPro"
        / "Lights" / stored_filter
    )
    lights.mkdir(parents=True)
    for i, hour in enumerate(["21:00:00", "22:00:00", "23:00:00"]):
        hdu = fits.PrimaryHDU()
        h = hdu.header
        h["OBJECT"] = "M 81"
        h["DATE-OBS"] = f"2026-02-19T{hour}"
        h["EXPOSURE"] = 180.0
        h["INSTRUME"] = "ZWOASI585MCPro"
        h["FOCALLEN"] = 400.0
        h["GAIN"] = 200
        h["CCD-TEMP"] = -20.0
        h["RA"] = 148.89
        h["DEC"] = 69.07
        # No FILTER card — the live rigs don't write one (CLAUDE.md), which is
        # why the folder name was being trusted in the first place.
        hdu.writeto(lights / f"{i:04d}.fit", overwrite=True)

    db = _db(tmp_path)
    backend = LocalBackend(db)

    create = _only_create(archive, backend)
    row = _catalog_row_from_create(create, create["session_id"])
    # Whatever the disk resolved to ('UnknownFilter' for a junk folder name,
    # the aliased spelling for a misspelled one) is what the id carries.
    disk_filter = create["session_id"].rsplit("_", 1)[1]
    row["filter"] = stored_filter
    row["session_id"] = create["session_id"].replace(disk_filter, stored_filter, 1)
    upsert_session(db, row)
    return archive, backend, row["session_id"], create["session_id"]


def test_mosaic_panel_name_in_filter_column_pairs_as_a_rename(tmp_path):
    """U2's junk filter values must not read as delete + create."""
    archive, backend, old_id, new_id = _stored_under_filter(tmp_path, "IC4604_1-1")

    proposals = scan(archive, backend)

    assert [p["kind"] for p in proposals] == ["rename"]
    assert proposals[0]["session_id"] == old_id
    assert proposals[0]["changes"]["filter"]["current"] == "IC4604_1-1"


def test_misspelled_filter_pairs_as_a_rename(tmp_path):
    """'AstronimikL2' -> 'AstronomikL2' is a rename, not a new session."""
    archive, backend, old_id, _ = _stored_under_filter(tmp_path, "AstronimikL2")

    proposals = scan(archive, backend)

    assert [p["kind"] for p in proposals] == ["rename"]
    assert proposals[0]["session_id"] == old_id


def test_a_scanned_create_proposal_can_actually_be_applied(tmp_path):
    """End-to-end: rescan's own output must satisfy apply_rescan_proposal.

    The two halves were tested in isolation and disagreed about where a
    create's `target` lives — rescan puts it on the proposal row (it is not in
    `_CHANGE_FIELDS`), while apply read only `changes`. Every real create
    therefore died on `NOT NULL constraint failed: sessions.target`, with the
    unit test passing because it hand-built a shape no producer emits.

    So this test deliberately does NOT construct a proposal: it takes whatever
    `scan` produces and feeds it straight to the applier.
    """
    import sqlite3

    from darkroom import catalog_db

    archive = tmp_path / "archive"
    _write_session_frames(archive)
    db = _db(tmp_path)                      # empty catalog
    proposals = scan(archive, LocalBackend(db))
    assert len(proposals) == 1 and proposals[0]["kind"] == "create"

    conn = catalog_db.open_db(db)
    try:
        catalog_db.apply_rescan_proposal(conn, db, proposals[0])
    finally:
        conn.close()

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (proposals[0]["session_id"],),
        ).fetchone()
    assert row is not None, "a scanned create proposal must be appliable"
    assert row["target"] == proposals[0]["target"]
    assert row["obs_date"] == proposals[0]["obs_date"]
    assert row["lights_path"] == proposals[0]["lights_path"]


def test_null_filter_and_nofilter_pair_as_a_rename_not_delete_plus_create(tmp_path):
    """The archive cannot express "filter unknown".

    `session_dest_rel` writes `Lights/NoFilter/` for a NULL filter while
    `make_session_id` writes `..._UnknownFilter`, so once a NULL-filter
    session's folder is canonicalised the disk reads back `NoFilter` and the
    row diverges from its own archive. That surfaced as a delete + create pair,
    which on apply drops processed_state, processed_date, created_at and the
    session_guiding row. Found live after it had already cost a `processed`
    row (NGC 1499 2023-09-18).
    """
    from darkroom.rescan import _canonical_session_id

    stored = {"session_id": "NGC1499_20230918_Canon200mm_Canon6D_UnknownFilter",
              "target": "NGC 1499", "obs_date": "2023-09-18",
              "ota": "Canon200mm", "camera": "Canon6D", "filter": None}
    from_disk = dict(stored, filter="NoFilter",
                     session_id="NGC1499_20230918_Canon200mm_Canon6D_NoFilter")

    assert _canonical_session_id(stored) == _canonical_session_id(from_disk), (
        "a NULL filter and the NoFilter its own folder round-trips to must be "
        "the same session, or the pair reads as delete + create"
    )


def test_a_real_filter_still_distinguishes_sessions(tmp_path):
    """The NoFilter/NULL equivalence must not blur genuine filters together."""
    from darkroom.rescan import _canonical_session_id

    base = {"target": "NGC 7000", "obs_date": "2025-08-01",
            "ota": "FRA400", "camera": "Canon6D"}
    assert (_canonical_session_id(dict(base, filter="L-Extreme"))
            != _canonical_session_id(dict(base, filter=None)))
    assert (_canonical_session_id(dict(base, filter="L-Extreme"))
            != _canonical_session_id(dict(base, filter="NoFilter")))
