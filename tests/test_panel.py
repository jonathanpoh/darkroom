"""M1: mosaic panel as a fourth session identity dimension, end to end.

Cross-cutting on purpose. The panel is an identity component, so the failure
that matters is not any one function being wrong — it is two of them
disagreeing, which leaves a catalog row pointing at a folder that isn't there
(the exact silent breakage CLAUDE.md warns about for the other identity
fields). The round-trip tests below pin write-side and read-side together.

Values are taken from the real 8-panel M8 mosaic: folders `M 8_1-1` … `M 8_4-2`,
FOCALLEN 51, ZWO ASI585MC Pro, filter `AstronimikL2` (a typo aliased to
`AstronomikL2`).
"""
from pathlib import Path

import pytest

from darkroom.cataloger import _filter_from_path, init_db, upsert_session
from darkroom.catalog_db import list_pending_renames, open_db, update_session_fields
from darkroom.ingest_review import recompute_session_entry
from darkroom.names import make_session_id, session_dest_rel
from darkroom.parse import panel_from_dirname, parse_ota, parse_panel
from darkroom.rescan import _canonical_session_id

PANELS = ("1-1", "1-2", "2-1", "2-2", "3-1", "3-2", "4-1", "4-2")


# ── the 50mm blocker ─────────────────────────────────────────────────────────

def test_real_mosaic_focallen_is_recognised():
    # Every frame of the real mosaic reports FOCALLEN 51, not a nominal 50 —
    # the same measured-vs-nominal drift that makes FRA400 report 402.
    assert parse_ota(51) == "Canon50mm"


@pytest.mark.parametrize("fl", [45, 50, 51, 55])
def test_canon50mm_window_bounds(fl):
    assert parse_ota(fl) == "Canon50mm"


@pytest.mark.parametrize("fl", [44, 56, 100])
def test_canon50mm_window_does_not_overreach(fl):
    # 100mm in particular: the archive has a lens-named 100mm folder, and
    # inventing a Canon50mm for it would be worse than Unknown.
    assert parse_ota(fl) == "Unknown"


# ── identity round-trip: what we write, we can read back ─────────────────────

@pytest.mark.parametrize("panel", PANELS)
def test_panel_identity_round_trips_through_the_archive_path(panel):
    """Split a real folder name, build the path, read the pieces back out."""
    base, parsed = parse_panel(f"M 8_{panel}")
    assert (base, parsed) == ("M 8", panel)

    dest = session_dest_rel(
        base, "2026-08-13", "Canon50mm", "ZWO ASI585MC Pro", "AstronomikL2",
        panel=parsed,
    )
    assert dest.name == f"P{panel}"
    # The read side must recover both components from that same path, or a
    # later rescan proposes changes against a session that is in fact correct.
    assert panel_from_dirname(dest.name) == panel
    assert _filter_from_path(Path("/archive") / dest) == "AstronomikL2"


def test_eight_panels_get_eight_distinct_session_ids():
    """The collision this whole feature exists to fix.

    Same target, night, optics and filter for all eight — without the panel
    they are one session_id eight times over.
    """
    ids = {
        make_session_id(
            "M 8", "2026-08-13", "Canon50mm", "ZWOASI585MCPro", "AstronomikL2",
            panel=p,
        )
        for p in PANELS
    }
    assert len(ids) == 8
    assert "M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2_P1-1" in ids


def test_filter_from_path_unchanged_without_a_panel_dir():
    dest = session_dest_rel("M 81", "2026-02-19", "FRA400", "ZWOASI585MCPro", "L-Pro")
    assert _filter_from_path(Path("/archive") / dest) == "L-Pro"


def test_panel_dir_regex_does_not_eat_an_ordinary_filter_dir():
    # A filter directory is what sits where the panel dir would be, so
    # "Pro"-ish names must not read as panel labels.
    for name in ("L-Pro", "L-Extreme", "NoFilter", "AstronomikL2", "P1", "Panel"):
        assert panel_from_dirname(name) is None


# ── the mixed target: NULL-panel and panelled sessions coexisting ────────────

def test_target_holds_both_panelled_and_single_pointing_sessions(tmp_path):
    """Required behaviour, not an edge case.

    IC 4604 really is one bare single-pointing night plus a 4-panel mosaic, and
    that is the normal end state for anything shot single-frame first and
    mosaicked later.
    """
    db = tmp_path / "cat.db"
    init_db(db)

    upsert_session(db, {
        "session_id": "IC4604_20230715_Unknown_Canon6D_NoFilter",
        "target": "IC 4604", "obs_date": "2023-07-15", "ota": "Unknown",
        "camera": "Canon6D", "filter": "NoFilter", "gain": 1600,
        "temperature_c": 15.0, "exposure_sec": 120.0, "focal_length": None,
        "frame_count": 21, "total_integration_sec": 2520,
        "ra_deg": None, "dec_deg": None, "lights_path": None,
    })
    for p in ("1-1", "1-2", "2-1", "2-2"):
        upsert_session(db, {
            "session_id": make_session_id(
                "IC 4604", "2025-04-26", "FRA400", "Canon6D", "NoFilter", panel=p,
            ),
            "target": "IC 4604", "obs_date": "2025-04-26", "ota": "FRA400",
            "camera": "Canon6D", "filter": "NoFilter", "panel": p, "gain": 1600,
            "temperature_c": 15.0, "exposure_sec": 120.0, "focal_length": 400.0,
            "frame_count": 10, "total_integration_sec": 1200,
            "ra_deg": None, "dec_deg": None, "lights_path": None,
        })

    with open_db(db) as conn:
        rows = conn.execute(
            "SELECT session_id, panel FROM sessions WHERE target = 'IC 4604'"
        ).fetchall()

    assert len(rows) == 5
    assert sum(1 for r in rows if r["panel"] is None) == 1
    assert sorted(r["panel"] for r in rows if r["panel"]) == ["1-1", "1-2", "2-1", "2-2"]
    # Five rows, five ids — the single-pointing night does not collide with a panel.
    assert len({r["session_id"] for r in rows}) == 5


# ── editing the panel is an identity edit ────────────────────────────────────

def test_editing_panel_recomputes_id_and_path_and_queues_a_rename(tmp_path):
    db = tmp_path / "cat.db"
    init_db(db)
    old_path = str(session_dest_rel(
        "M 8", "2026-08-13", "Canon50mm", "ZWOASI585MCPro", "AstronomikL2", panel="1-1",
    ))
    upsert_session(db, {
        "session_id": make_session_id(
            "M 8", "2026-08-13", "Canon50mm", "ZWOASI585MCPro", "AstronomikL2",
            panel="1-1",
        ),
        "target": "M 8", "obs_date": "2026-08-13", "ota": "Canon50mm",
        "camera": "ZWOASI585MCPro", "filter": "AstronomikL2", "panel": "1-1",
        "gain": 200, "temperature_c": -9.5, "exposure_sec": 30.0,
        "focal_length": 51.0, "frame_count": 10, "total_integration_sec": 300,
        "ra_deg": None, "dec_deg": None, "lights_path": old_path,
    })

    with open_db(db) as conn:
        assert update_session_fields(
            conn, "M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2_P1-1",
            panel="1-2",
        )
        row = conn.execute(
            "SELECT session_id, panel, lights_path FROM sessions"
        ).fetchone()
        assert row["session_id"].endswith("_P1-2")
        assert row["lights_path"].endswith("/P1-2")
        assert row["panel"] == "1-2"
        # The webapi host has no NAS mount, so the folder move is owed back
        # on the Mac — same contract as every other identity edit.
        pending = list_pending_renames(conn)
        assert len(pending) == 1
        assert pending[0]["old_path"] == old_path
        assert pending[0]["new_path"].endswith("/P1-2")


def test_clearing_panel_returns_the_session_to_single_pointing(tmp_path):
    db = tmp_path / "cat.db"
    init_db(db)
    upsert_session(db, {
        "session_id": make_session_id(
            "M 8", "2026-08-13", "Canon50mm", "ZWOASI585MCPro", "AstronomikL2",
            panel="1-1",
        ),
        "target": "M 8", "obs_date": "2026-08-13", "ota": "Canon50mm",
        "camera": "ZWOASI585MCPro", "filter": "AstronomikL2", "panel": "1-1",
        "gain": 200, "temperature_c": -9.5, "exposure_sec": 30.0,
        "focal_length": 51.0, "frame_count": 10, "total_integration_sec": 300,
        "ra_deg": None, "dec_deg": None, "lights_path": None,
    })
    with open_db(db) as conn:
        update_session_fields(
            conn, "M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2_P1-1",
            panel=None,
        )
        row = conn.execute("SELECT session_id, panel FROM sessions").fetchone()
    # No trailing "_P" left behind.
    assert row["session_id"] == "M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2"
    assert row["panel"] is None


# ── ingest review ────────────────────────────────────────────────────────────

def test_recompute_session_entry_applies_a_panel_edit(tmp_path):
    """`recompute_entry` is the only correct way to apply an identity edit."""
    entry = {
        "session_id": "M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2",
        "target": "M 8", "obs_date": "2026-08-13", "ota": "Canon50mm",
        "camera": "ZWOASI585MCPro", "filter": "AstronomikL2",
        "panel": "2-1",
        "lights_rel_path": "stale", "files": [],
    }
    recompute_session_entry(entry, {}, tmp_path)
    assert entry["session_id"].endswith("_P2-1")
    assert entry["lights_rel_path"].endswith("/P2-1")


def test_recompute_session_entry_treats_blank_panel_as_none(tmp_path):
    # A cleared panel must not append a bare "_P" / "P" directory.
    entry = {
        "session_id": "x", "target": "M 8", "obs_date": "2026-08-13",
        "ota": "Canon50mm", "camera": "ZWOASI585MCPro", "filter": "AstronomikL2",
        "panel": "", "lights_rel_path": "stale", "files": [],
    }
    recompute_session_entry(entry, {}, tmp_path)
    assert entry["session_id"] == "M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2"
    assert not entry["lights_rel_path"].endswith("P")


# ── rescan ───────────────────────────────────────────────────────────────────

def test_rescan_canonicalizes_a_legacy_panel_in_the_target_column():
    """The pre-M1 shape: the panel landed in the target ("IC 4604_1-1").

    The disk-side scan now splits it out, so without canonicalizing the stored
    row the same way, every legacy panel row reads as an unrelated delete +
    create — which on apply drops its processed_state and guiding row.
    """
    legacy = _canonical_session_id({
        "target": "IC 4604_1-1", "obs_date": "2025-04-26",
        "ota": "FRA400", "camera": "Canon6D", "filter": "NoFilter",
    })
    fresh = _canonical_session_id({
        "target": "IC 4604", "obs_date": "2025-04-26",
        "ota": "FRA400", "camera": "Canon6D", "filter": "NoFilter",
        "panel": "1-1",
    })
    assert legacy == fresh


def test_rescan_canonical_id_unchanged_for_an_ordinary_session():
    assert _canonical_session_id({
        "target": "M 81", "obs_date": "2026-02-19",
        "ota": "FRA400", "camera": "ZWOASI585MCPro", "filter": "L-Pro",
    }) == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"


# ── the webapi must not silently drop the panel ──────────────────────────────

def test_session_in_model_carries_panel():
    """pydantic ignores unmodelled fields, so an omission here would be silent:
    a remote-backend commit would land eight rows with no panel at all."""
    from darkroom.webapi.app import SessionIn

    assert SessionIn(session_id="x", panel="1-1").panel == "1-1"
    assert SessionIn(session_id="x").panel is None
