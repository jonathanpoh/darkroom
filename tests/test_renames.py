"""Tests for darkroom.renames (U2 Phase 1) — executing the pending-renames ledger."""
from __future__ import annotations

from pathlib import Path

import pytest

from darkroom.catalog_client import LocalBackend
from darkroom.renames import (
    ALREADY_DONE,
    APPLIED,
    CONFLICT,
    ERROR,
    MISSING,
    apply_renames,
)


def _session(
    session_id,
    target="M 81",
    obs_date="2026-02-19",
    ota="FRA400",
    camera="ZWOASI585MCPro",
    filter="L-Pro",
    **extra,
):
    base = {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": ota,
        "camera": camera,
        "filter": filter,
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": 100,
        "total_integration_sec": 18000,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": f"01_Deep Sky Objects/{target}/{obs_date}_{ota}_{camera}/Lights/{filter}",
        "notes": "",
    }
    base.update(extra)
    return base


@pytest.fixture
def archive(tmp_path) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    return root


@pytest.fixture
def backend(tmp_path) -> LocalBackend:
    return LocalBackend(tmp_path / "catalog.db")


def _make_pending_rename(
    backend: LocalBackend,
    archive: Path,
    *,
    create_old: bool = True,
    edit_field: str = "filter",
    edit_value: str = "L-Extreme",
) -> str:
    """Upsert a session, create its old_path folder (with a file in it) unless
    create_old=False, then edit identity to produce exactly one pending rename.
    Returns the session_id *after* the edit (the current one)."""
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    session = _session(sid)
    backend.upsert_session(session)
    old_rel = session["lights_path"]
    if create_old:
        old_dir = archive / old_rel
        old_dir.mkdir(parents=True)
        (old_dir / "light_0001.fit").write_bytes(b"data")

    backend.update_session_fields(sid, **{edit_field: edit_value})
    suffix = {
        "filter": f"M81_20260219_FRA400_ZWOASI585MCPro_{edit_value}",
        "obs_date": f"M81_{edit_value.replace('-', '')}_FRA400_ZWOASI585MCPro_L-Pro",
    }
    return suffix[edit_field]


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_reports_applied_and_mutates_nothing(archive, backend):
    new_sid = _make_pending_rename(backend, archive)
    rename_before = backend.list_pending_renames()[0]

    results = apply_renames(archive, backend, apply=False)

    assert len(results) == 1
    assert results[0].outcome == APPLIED
    assert results[0].session_id == new_sid

    old_dir = archive / rename_before["old_path"]
    new_dir = archive / rename_before["new_path"]
    assert old_dir.is_dir()  # untouched
    assert not new_dir.exists()  # untouched

    assert backend.list_pending_renames() == [rename_before]  # ledger untouched


# ---------------------------------------------------------------------------
# --apply: normal move
# ---------------------------------------------------------------------------


def test_apply_moves_folder_creates_parents_acks_and_prunes_empty_old_parent(archive, backend):
    # Use an obs_date edit (not filter) so old_path and new_path land under
    # *different* session-date directories — the old one is left with
    # nothing else in it and should be pruned all the way up to (but not
    # including) the target folder, which still holds the new session dir.
    _make_pending_rename(backend, archive, edit_field="obs_date", edit_value="2026-02-20")
    rename = backend.list_pending_renames()[0]
    old_rel = Path(rename["old_path"])
    new_rel = Path(rename["new_path"])
    old_dir = archive / old_rel

    results = apply_renames(archive, backend, apply=True)

    assert len(results) == 1
    assert results[0].outcome == APPLIED

    new_dir = archive / new_rel
    assert new_dir.is_dir()
    assert (new_dir / "light_0001.fit").exists()
    assert not old_dir.exists()

    # old_dir = .../M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro.
    # Emptying it out prunes Lights/, then the whole 2026-02-19_... session
    # dir (both now empty) — but stops at 'M 81', which still holds the new
    # 2026-02-20_... session dir and so is never empty.
    old_session_dir = old_dir.parent.parent
    assert old_session_dir.name == "2026-02-19_FRA400_ZWOASI585MCPro"
    assert not old_session_dir.exists()  # pruned all the way up
    assert (archive / "01_Deep Sky Objects" / "M 81").is_dir()  # not pruned

    assert backend.list_pending_renames() == []  # acked


def test_apply_stops_pruning_at_non_empty_ancestor(archive, backend):
    _make_pending_rename(backend, archive, edit_field="obs_date", edit_value="2026-02-20")
    rename = backend.list_pending_renames()[0]
    old_dir = archive / rename["old_path"]
    old_session_dir = old_dir.parent.parent

    # Add an unrelated file directly in the old session dir (a sibling of
    # Lights/) so it's non-empty even after Lights/ itself is pruned —
    # pruning must stop there instead of continuing up to 'M 81'.
    (old_session_dir / "sibling.txt").write_bytes(b"keep me")

    apply_renames(archive, backend, apply=True)

    assert not old_dir.parent.exists()  # Lights/ still pruned (it emptied out)
    assert old_session_dir.is_dir()  # but the session dir survives — non-empty
    assert (old_session_dir / "sibling.txt").exists()


def test_apply_never_prunes_the_archive_root(tmp_path, backend):
    # Archive root == the session's own immediate content (contrived, but
    # exercises the "never at/above archive_root" boundary).
    archive = tmp_path / "archive"
    archive.mkdir()
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    session = _session(sid, lights_path="Lights")
    backend.upsert_session(session)
    old_dir = archive / "Lights"
    old_dir.mkdir()
    (old_dir / "light_0001.fit").write_bytes(b"data")

    backend.update_session_fields(sid, filter="L-Extreme")

    apply_renames(archive, backend, apply=True)

    assert archive.is_dir()  # archive root itself never removed


# ---------------------------------------------------------------------------
# --apply: already in place
# ---------------------------------------------------------------------------


def test_already_in_place_acks_under_apply(archive, backend):
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    session = _session(sid)
    backend.upsert_session(session)
    # Don't create old_path; pre-create new_path instead — simulates the
    # move already having happened by hand.
    backend.update_session_fields(sid, filter="L-Extreme")
    rename = backend.list_pending_renames()[0]
    new_dir = archive / rename["new_path"]
    new_dir.mkdir(parents=True)

    results = apply_renames(archive, backend, apply=True)
    assert results[0].outcome == ALREADY_DONE
    assert backend.list_pending_renames() == []  # acked


def test_already_in_place_not_acked_under_dry_run(archive, backend):
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    session = _session(sid)
    backend.upsert_session(session)
    backend.update_session_fields(sid, filter="L-Extreme")
    rename = backend.list_pending_renames()[0]
    (archive / rename["new_path"]).mkdir(parents=True)

    results = apply_renames(archive, backend, apply=False)
    assert results[0].outcome == ALREADY_DONE
    assert len(backend.list_pending_renames()) == 1  # not acked


# ---------------------------------------------------------------------------
# missing / conflict
# ---------------------------------------------------------------------------


def test_missing_both_leaves_pending(archive, backend):
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    backend.upsert_session(_session(sid))
    backend.update_session_fields(sid, filter="L-Extreme")  # neither path created

    results = apply_renames(archive, backend, apply=True)
    assert results[0].outcome == MISSING
    assert len(backend.list_pending_renames()) == 1  # left pending


def test_conflict_leaves_pending(archive, backend):
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    session = _session(sid)
    backend.upsert_session(session)
    old_dir = archive / session["lights_path"]
    old_dir.mkdir(parents=True)
    backend.update_session_fields(sid, filter="L-Extreme")
    rename = backend.list_pending_renames()[0]
    (archive / rename["new_path"]).mkdir(parents=True)  # both exist now

    results = apply_renames(archive, backend, apply=True)
    assert results[0].outcome == CONFLICT
    assert len(backend.list_pending_renames()) == 1  # left pending
    assert old_dir.is_dir()  # untouched
    assert (archive / rename["new_path"]).is_dir()  # untouched


# ---------------------------------------------------------------------------
# unsafe paths
# ---------------------------------------------------------------------------


def test_absolute_old_path_rejected(archive, backend):
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    backend.upsert_session(_session(sid))
    backend.update_session_fields(sid, filter="L-Extreme")

    # Hand-corrupt the ledger row to an absolute path.
    conn = backend._open()
    conn.execute("UPDATE pending_renames SET old_path = ?", ("/etc/passwd",))
    conn.commit()
    conn.close()

    results = apply_renames(archive, backend, apply=True)
    assert results[0].outcome == ERROR
    assert len(backend.list_pending_renames()) == 1  # left pending, nothing acked


def test_dotdot_new_path_rejected(archive, backend):
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    backend.upsert_session(_session(sid))
    backend.update_session_fields(sid, filter="L-Extreme")

    conn = backend._open()
    conn.execute("UPDATE pending_renames SET new_path = ?", ("../../evil",))
    conn.commit()
    conn.close()

    results = apply_renames(archive, backend, apply=True)
    assert results[0].outcome == ERROR
    assert len(backend.list_pending_renames()) == 1
