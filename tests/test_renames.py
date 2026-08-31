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


# ---------------------------------------------------------------------------
# "deepening" edits: new_path nested inside old_path
#
# A legacy row whose lights_path predates the Lights/<Filter>/ level gets that
# level added the moment any identity edit recomputes the path through
# session_dest_rel. old_path is then new_path's own ancestor, so it always
# exists — the generic "both exist" conflict would be a false alarm that can
# never clear, and there is no CLI way to ack a stuck conflict.
#
# Found live on NGC 7380/2025-09-13 (2026-08-31): 0 frames at the old level,
# 100 already in Lights/NoFilter, reported as a permanent conflict.
# ---------------------------------------------------------------------------


def _make_deepening_rename(backend: LocalBackend, archive: Path) -> dict:
    """Legacy shallow lights_path -> canonical Lights/<Filter>/ under it."""
    sid = "NGC7380_20250913_FRA400_ZWOASI585MCPro_L-Pro"
    shallow = "01_Deep Sky Objects/NGC 7380/2025-09-13_FRA400_ZWOASI585MCPro"
    backend.upsert_session(_session(
        sid, target="NGC 7380", obs_date="2025-09-13", lights_path=shallow,
    ))
    backend.update_session_fields(sid, filter="L-Extreme")
    rename = backend.list_pending_renames()[0]
    assert rename["old_path"] == shallow
    assert rename["new_path"].startswith(shallow + "/")   # genuinely nested
    return rename


def test_deepening_already_done_when_frames_sit_at_new(archive, backend):
    rename = _make_deepening_rename(backend, archive)
    new_dir = archive / rename["new_path"]
    new_dir.mkdir(parents=True)
    (new_dir / "light_0001.fit").write_bytes(b"data")

    results = apply_renames(archive, backend, apply=True)

    assert results[0].outcome == ALREADY_DONE
    assert "already holds the frames" in results[0].detail
    assert backend.list_pending_renames() == []          # acked, not stuck
    assert (new_dir / "light_0001.fit").exists()         # nothing moved


def test_deepening_not_acked_under_dry_run(archive, backend):
    rename = _make_deepening_rename(backend, archive)
    new_dir = archive / rename["new_path"]
    new_dir.mkdir(parents=True)
    (new_dir / "light_0001.fit").write_bytes(b"data")

    results = apply_renames(archive, backend, apply=False)

    assert results[0].outcome == ALREADY_DONE
    assert len(backend.list_pending_renames()) == 1


def test_deepening_conflicts_when_frames_still_loose_in_old(archive, backend):
    """Not a rename — shutil.move would move a directory into itself."""
    rename = _make_deepening_rename(backend, archive)
    old_dir = archive / rename["old_path"]
    old_dir.mkdir(parents=True)
    (old_dir / "light_0001.fit").write_bytes(b"data")
    (archive / rename["new_path"]).mkdir(parents=True)

    results = apply_renames(archive, backend, apply=True)

    assert results[0].outcome == CONFLICT
    assert "migrate-archive" in results[0].detail
    assert len(backend.list_pending_renames()) == 1
    assert (old_dir / "light_0001.fit").exists()         # untouched


def test_deepening_conflicts_when_neither_side_holds_frames(archive, backend):
    rename = _make_deepening_rename(backend, archive)
    (archive / rename["new_path"]).mkdir(parents=True)

    results = apply_renames(archive, backend, apply=True)

    assert results[0].outcome == CONFLICT
    assert len(backend.list_pending_renames()) == 1


def test_sibling_rename_still_a_plain_conflict(archive, backend):
    """Regression guard: the deepening branch must not swallow real conflicts."""
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    session = _session(sid)
    backend.upsert_session(session)
    old_dir = archive / session["lights_path"]
    old_dir.mkdir(parents=True)
    (old_dir / "light_0001.fit").write_bytes(b"data")
    backend.update_session_fields(sid, filter="L-Extreme")
    rename = backend.list_pending_renames()[0]
    new_dir = archive / rename["new_path"]
    new_dir.mkdir(parents=True)
    (new_dir / "light_0002.fit").write_bytes(b"data")

    results = apply_renames(archive, backend, apply=True)

    assert results[0].outcome == CONFLICT
    assert results[0].detail == "both old and new paths exist — left pending"
    assert len(backend.list_pending_renames()) == 1


@pytest.mark.parametrize("new_path,old_path,expected", [
    ("a/b/Lights/NoFilter", "a/b", True),
    ("a/b/c/d", "a/b", True),
    ("a/b", "a/b", False),                 # identical, not nested
    ("a/c", "a/b", False),                 # siblings
    ("a/bb/c", "a/b", False),              # prefix of the *string*, not a component
    # Case-only rename on a case-insensitive mount: the same inode, but not
    # nesting. Compared on path components so the filesystem can't confuse it.
    ("Sh2-101/2026-07-19/Lights/L", "SH2-101/2026-07-19/Lights/L", False),
])
def test_is_nested(new_path, old_path, expected):
    from darkroom.renames import _is_nested

    assert _is_nested(new_path, old_path) is expected
