import subprocess
import sys
from pathlib import Path

import pytest

from darkroom.cataloger import init_db, upsert_session, set_processed_state
from darkroom.catalog_db import (
    open_db,
    query_sessions,
    count_sessions,
    update_session_fields,
    delete_session,
    list_pending_renames,
    ack_pending_rename,
    rename_target,
    list_sites,
    add_site,
    update_site_fields,
)


def _session(
    session_id,
    target="M 81",
    obs_date="2026-02-19",
    ota="FRA400",
    camera="ZWOASI585MCPro",
    filter="L-Pro",
    gain=200,
    frame_count=100,
    **extra,
):
    base = {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": ota,
        "camera": camera,
        "filter": filter,
        "gain": gain,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": frame_count,
        "total_integration_sec": frame_count * 180,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": f"01_Deep Sky Objects/{target}/{obs_date}_{ota}_{camera}/Lights/{filter}",
        "notes": "",
    }
    base.update(extra)
    return base


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    init_db(db)
    upsert_session(db, _session(
        "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        target="M 81", obs_date="2026-02-19", ota="FRA400",
        camera="ZWOASI585MCPro", filter="L-Pro", frame_count=132,
    ))
    upsert_session(db, _session(
        "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme",
        target="M 81", obs_date="2026-02-20", ota="FRA400",
        camera="ZWOASI585MCPro", filter="L-Extreme", frame_count=60,
    ))
    upsert_session(db, _session(
        "NGC7380_20251001_FMA180_Canon6D_L-Extreme",
        target="NGC 7380", obs_date="2025-10-01", ota="FMA180",
        camera="Canon6D", filter="L-Extreme", frame_count=40,
    ))
    upsert_session(db, _session(
        "NGC7380_20251005_FMA180_Canon6D_NoFilter",
        target="NGC 7380", obs_date="2025-10-05", ota="FMA180",
        camera="Canon6D", filter="NoFilter", frame_count=20,
    ))
    set_processed_state(
        db, "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        state="processed", processed_date="2026-03-01",
        processed_path="01_Deep Sky Objects/M 81/_Processed/2026-03-01",
        notes="looks great",
    )
    set_processed_state(db, "NGC7380_20251001_FMA180_Canon6D_L-Extreme", state="skipped")
    return db


# ---------------------------------------------------------------------------
# open_db
# ---------------------------------------------------------------------------

def test_open_db_row_factory(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    row = conn.execute("SELECT * FROM sessions LIMIT 1").fetchone()
    assert row["target"] is not None  # Row supports key access
    conn.close()


def test_open_db_enables_wal(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_open_db_creates_schema_for_missing_path(tmp_path):
    db = tmp_path / "fresh.db"
    assert not db.exists()
    conn = open_db(db)
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    assert rows == []
    conn.close()
    assert db.exists()


# ---------------------------------------------------------------------------
# query_sessions
# ---------------------------------------------------------------------------

def test_query_sessions_no_filter_returns_all(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn)
    assert len(rows) == 4


def test_query_sessions_by_target_case_insensitive_and_normalized(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, target="m81")
    assert len(rows) == 2
    assert all(r["target"] == "M 81" for r in rows)

    rows = query_sessions(conn, target="M 81")
    assert len(rows) == 2


def test_query_sessions_by_camera(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, camera="Canon6D")
    assert len(rows) == 2
    assert all(r["camera"] == "Canon6D" for r in rows)


def test_query_sessions_by_ota(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, ota="FMA180")
    assert len(rows) == 2
    assert all(r["ota"] == "FMA180" for r in rows)


def test_query_sessions_by_filter_equality(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, filter="L-Extreme")
    assert len(rows) == 2
    assert all(r["filter"] == "L-Extreme" for r in rows)


def test_query_sessions_by_processed_state(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, processed_state="processed")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"

    rows = query_sessions(conn, processed_state="skipped")
    assert len(rows) == 1

    rows = query_sessions(conn, processed_state="unprocessed")
    assert len(rows) == 2


def test_query_sessions_date_range_inclusive(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, date_from="2026-02-19", date_to="2026-02-20")
    assert len(rows) == 2
    rows = query_sessions(conn, date_from="2026-02-20", date_to="2026-02-20")
    assert len(rows) == 1
    assert rows[0]["obs_date"] == "2026-02-20"


def test_query_sessions_by_session_id(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, session_id="NGC7380_20251005_FMA180_Canon6D_NoFilter")
    assert len(rows) == 1


def test_query_sessions_combined_filters(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, target="NGC 7380", ota="FMA180", processed_state="skipped")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "NGC7380_20251001_FMA180_Canon6D_L-Extreme"


def test_query_sessions_no_match(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn, target="M 999")
    assert rows == []


def test_query_sessions_pagination(tmp_path):
    conn = open_db(make_db(tmp_path))
    all_rows = query_sessions(conn)
    assert len(all_rows) == 4

    page1 = query_sessions(conn, limit=2, offset=0)
    page2 = query_sessions(conn, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert [r["session_id"] for r in page1] + [r["session_id"] for r in page2] == \
        [r["session_id"] for r in all_rows]


def test_query_sessions_ordering_stable(tmp_path):
    conn = open_db(make_db(tmp_path))
    rows = query_sessions(conn)
    dates = [r["obs_date"] for r in rows]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# count_sessions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {},
    {"target": "M 81"},
    {"camera": "Canon6D"},
    {"ota": "FMA180"},
    {"filter": "L-Extreme"},
    {"processed_state": "processed"},
    {"processed_state": "unprocessed"},
    {"date_from": "2026-02-19", "date_to": "2026-02-20"},
    {"target": "NGC 7380", "processed_state": "skipped"},
])
def test_count_sessions_matches_query_length(tmp_path, kwargs):
    conn = open_db(make_db(tmp_path))
    assert count_sessions(conn, **kwargs) == len(query_sessions(conn, **kwargs))


def test_count_sessions_ignores_limit_offset(tmp_path):
    conn = open_db(make_db(tmp_path))
    # count_sessions has no limit/offset kwargs at all; verify total count
    # doesn't change regardless of how query_sessions is paginated.
    assert count_sessions(conn) == 4
    assert len(query_sessions(conn, limit=1, offset=0)) == 1
    assert count_sessions(conn) == 4


# ---------------------------------------------------------------------------
# update_session_fields
# ---------------------------------------------------------------------------

def test_update_non_identity_field_preserves_session_id_and_state(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    ok = update_session_fields(conn, sid, notes="updated note")
    assert ok is True

    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    assert row["session_id"] == sid
    assert row["notes"] == "updated note"
    assert row["processed_state"] == "processed"
    assert row["processed_date"] == "2026-03-01"


def test_update_identity_field_recomputes_session_id_and_carries_state(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    old_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"

    count_before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    ok = update_session_fields(conn, old_sid, filter="L-Extreme")
    assert ok is True

    new_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Extreme"

    count_after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count_after == count_before  # no orphan/duplicate row

    old_row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (old_sid,)
    ).fetchone()
    assert old_row is None  # old session_id is gone

    new_row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (new_sid,)
    ).fetchone()
    assert new_row is not None
    assert new_row["filter"] == "L-Extreme"
    # Anti-orphan guarantee: status/history carried on the same row.
    assert new_row["processed_state"] == "processed"
    assert new_row["processed_path"] == "01_Deep Sky Objects/M 81/_Processed/2026-03-01"
    assert new_row["processed_date"] == "2026-03-01"
    assert new_row["notes"] == "looks great"


def test_update_identity_field_target_recomputes_session_id(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    old_sid = "NGC7380_20251005_FMA180_Canon6D_NoFilter"
    ok = update_session_fields(conn, old_sid, target="NGC 1234")
    assert ok is True
    new_sid = "NGC1234_20251005_FMA180_Canon6D_NoFilter"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (new_sid,)).fetchone()
    assert row is not None
    assert row["target"] == "NGC 1234"


def test_update_unknown_field_raises(tmp_path):
    conn = open_db(make_db(tmp_path))
    with pytest.raises(ValueError):
        update_session_fields(conn, "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro", bogus_field=1)


def test_update_invalid_processed_state_raises(tmp_path):
    conn = open_db(make_db(tmp_path))
    with pytest.raises(ValueError):
        update_session_fields(
            conn, "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro",
            processed_state="not_a_real_state",
        )


def test_update_in_progress_state_accepted(tmp_path):
    # F1: 'in_progress' is a valid archive-derived state alongside the
    # original three ('unprocessed', 'processed', 'skipped').
    conn = open_db(make_db(tmp_path))
    sid = "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme"
    ok = update_session_fields(conn, sid, processed_state="in_progress")
    assert ok is True
    row = conn.execute("SELECT processed_state FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    assert row["processed_state"] == "in_progress"


def test_update_unknown_session_id_returns_false(tmp_path):
    conn = open_db(make_db(tmp_path))
    ok = update_session_fields(conn, "does_not_exist", notes="x")
    assert ok is False


def test_update_identity_change_colliding_with_existing_row_raises(tmp_path):
    conn = open_db(make_db(tmp_path))
    # Renaming the M81 L-Pro session's filter to L-Extreme collides with the
    # existing M81_20260220 session only if dates matched; construct a real
    # collision by editing the L-Extreme session's obs_date to match the
    # L-Pro session's identity components exactly.
    sid_a = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    sid_b = "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme"
    # First make sid_b's filter match sid_a's filter, keeping its own obs_date
    # distinct — no collision yet.
    update_session_fields(conn, sid_b, filter="L-Pro")
    # Now editing sid_b's obs_date to match sid_a's obs_date would produce the
    # exact same session_id as sid_a -> must raise.
    with pytest.raises(ValueError):
        update_session_fields(conn, "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro", obs_date="2026-02-19")


def test_update_identity_field_recomputes_lights_path(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    old_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"

    ok = update_session_fields(conn, old_sid, filter="L-Extreme")
    assert ok is True

    new_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Extreme"
    row = conn.execute(
        "SELECT lights_path FROM sessions WHERE session_id = ?", (new_sid,)
    ).fetchone()
    assert row["lights_path"] == (
        "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Extreme"
    )


def test_update_target_spacing_only_recomputes_lights_path_same_session_id(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    # Target stored without a space: same session_id slug as 'M 81', but a
    # different archive folder name.
    sid = "M81_20260301_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db, _session(sid, target="M81", obs_date="2026-03-01"))
    conn = open_db(db)

    ok = update_session_fields(conn, sid, target="M 81")
    assert ok is True

    row = conn.execute(
        "SELECT session_id, lights_path FROM sessions WHERE session_id = ?", (sid,)
    ).fetchone()
    assert row is not None  # session_id unchanged (slug strips spaces)
    assert row["lights_path"] == (
        "01_Deep Sky Objects/M 81/2026-03-01_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    )


def test_update_identity_field_leaves_null_lights_path_null(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    sid = "M81_20260301_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db, _session(sid, obs_date="2026-03-01", lights_path=None))
    conn = open_db(db)

    ok = update_session_fields(conn, sid, filter="L-Extreme")
    assert ok is True

    new_sid = "M81_20260301_FRA400_ZWOASI585MCPro_L-Extreme"
    row = conn.execute(
        "SELECT lights_path FROM sessions WHERE session_id = ?", (new_sid,)
    ).fetchone()
    assert row["lights_path"] is None


def test_update_non_identity_field_leaves_lights_path_untouched(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    before = conn.execute(
        "SELECT lights_path FROM sessions WHERE session_id = ?", (sid,)
    ).fetchone()["lights_path"]

    update_session_fields(conn, sid, notes="just a note")

    after = conn.execute(
        "SELECT lights_path FROM sessions WHERE session_id = ?", (sid,)
    ).fetchone()["lights_path"]
    assert after == before


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------

def test_delete_session_removes_row(tmp_path):
    conn = open_db(make_db(tmp_path))
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    assert delete_session(conn, sid) is True
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    assert row is None


def test_delete_session_unknown_id_returns_false(tmp_path):
    conn = open_db(make_db(tmp_path))
    assert delete_session(conn, "does_not_exist") is False


# ---------------------------------------------------------------------------
# pending_renames ledger (U2)
# ---------------------------------------------------------------------------


def test_identity_edit_records_pending_rename(tmp_path):
    conn = open_db(make_db(tmp_path))
    old_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    old_path = conn.execute(
        "SELECT lights_path FROM sessions WHERE session_id = ?", (old_sid,)
    ).fetchone()["lights_path"]

    ok = update_session_fields(conn, old_sid, filter="L-Extreme")
    assert ok is True
    new_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Extreme"

    rows = list_pending_renames(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == new_sid
    assert row["old_path"] == old_path
    assert row["new_path"] == (
        "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Extreme"
    )
    assert row["created_at"] is not None
    assert row["updated_at"] is not None


def test_second_identity_edit_coalesces_pending_rename(tmp_path):
    conn = open_db(make_db(tmp_path))
    old_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    original_path = conn.execute(
        "SELECT lights_path FROM sessions WHERE session_id = ?", (old_sid,)
    ).fetchone()["lights_path"]

    update_session_fields(conn, old_sid, filter="L-Extreme")
    mid_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Extreme"

    # Second identity edit on the coalesced row.
    update_session_fields(conn, mid_sid, filter="NoFilter")
    final_sid = "M81_20260219_FRA400_ZWOASI585MCPro_NoFilter"

    rows = list_pending_renames(conn)
    assert len(rows) == 1  # still one row — coalesced, not appended
    row = rows[0]
    assert row["session_id"] == final_sid
    assert row["old_path"] == original_path  # pinned to the original disk path
    assert row["new_path"] == (
        "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/NoFilter"
    )


def test_identity_edit_back_to_original_deletes_pending_rename(tmp_path):
    conn = open_db(make_db(tmp_path))
    old_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"

    update_session_fields(conn, old_sid, filter="L-Extreme")
    assert len(list_pending_renames(conn)) == 1

    mid_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Extreme"
    update_session_fields(conn, mid_sid, filter="L-Pro")  # back to original

    assert list_pending_renames(conn) == []
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (old_sid,)
    ).fetchone()
    assert row is not None  # session itself still exists, just no pending rename


def test_non_identity_edit_records_no_pending_rename(tmp_path):
    conn = open_db(make_db(tmp_path))
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    update_session_fields(conn, sid, notes="just a note")
    assert list_pending_renames(conn) == []


def test_identity_edit_with_null_lights_path_records_no_pending_rename(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    sid = "M81_20260301_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db, _session(sid, obs_date="2026-03-01", lights_path=None))
    conn = open_db(db)

    update_session_fields(conn, sid, filter="L-Extreme")
    assert list_pending_renames(conn) == []


def test_delete_session_removes_pending_rename(tmp_path):
    conn = open_db(make_db(tmp_path))
    old_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    update_session_fields(conn, old_sid, filter="L-Extreme")
    new_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Extreme"
    assert len(list_pending_renames(conn)) == 1

    assert delete_session(conn, new_sid) is True
    assert list_pending_renames(conn) == []


def test_list_pending_renames_ordered_by_id(tmp_path):
    conn = open_db(make_db(tmp_path))
    update_session_fields(
        conn, "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro", filter="L-Extreme"
    )
    update_session_fields(
        conn, "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme", filter="L-Pro"
    )
    rows = list_pending_renames(conn)
    assert len(rows) == 2
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


def test_ack_pending_rename_removes_row_and_returns_true(tmp_path):
    conn = open_db(make_db(tmp_path))
    update_session_fields(
        conn, "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro", filter="L-Extreme"
    )
    rename_id = list_pending_renames(conn)[0]["id"]

    assert ack_pending_rename(conn, rename_id) is True
    assert list_pending_renames(conn) == []


def test_ack_pending_rename_unknown_id_returns_false(tmp_path):
    conn = open_db(make_db(tmp_path))
    assert ack_pending_rename(conn, 999999) is False


# ---------------------------------------------------------------------------
# rename_target (U2 phase 3)
# ---------------------------------------------------------------------------


def test_rename_target_renames_all_rows_session_id_and_lights_path(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    sid1 = "sid1"
    sid2 = "sid2"
    upsert_session(
        db, _session(sid1, target="M 82 M 82", obs_date="2026-02-19", filter="L-Pro")
    )
    upsert_session(
        db, _session(sid2, target="M 82 M 82", obs_date="2026-02-20", filter="L-Extreme")
    )
    conn = open_db(db)

    result = rename_target(conn, "M 82 M 82", "M 82")

    assert result == {"renamed": 2, "errors": [], "total": 2}

    rows = query_sessions(conn, target="M 82")
    assert len(rows) == 2
    ids = {r["session_id"] for r in rows}
    assert ids == {
        "M82_20260219_FRA400_ZWOASI585MCPro_L-Pro",
        "M82_20260220_FRA400_ZWOASI585MCPro_L-Extreme",
    }
    for r in rows:
        assert r["target"] == "M 82"
        assert "01_Deep Sky Objects/M 82/" in r["lights_path"]

    pending = list_pending_renames(conn)
    assert len(pending) == 2
    assert {p["session_id"] for p in pending} == ids


def test_rename_target_collision_partial_success(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    # Two mosaic panels of IC 4604, one shot on the same night as each other
    # under different panel-suffixed target names — merging both into the
    # shared base target collapses that pair to an identical session
    # identity, but a third session (different date) doesn't collide.
    sidA = "sidA"
    sidB = "sidB"
    sidC = "sidC"
    upsert_session(
        db, _session(sidA, target="IC 4604_1-1", obs_date="2026-02-19", filter="L-Pro")
    )
    upsert_session(
        db, _session(sidB, target="IC 4604_2-1", obs_date="2026-02-19", filter="L-Pro")
    )
    upsert_session(
        db, _session(sidC, target="IC 4604_1-1", obs_date="2026-02-20", filter="L-Pro")
    )
    conn = open_db(db)

    r1 = rename_target(conn, "IC 4604_2-1", "IC 4604")
    assert r1 == {"renamed": 1, "errors": [], "total": 1}

    r2 = rename_target(conn, "IC 4604_1-1", "IC 4604")
    assert r2["total"] == 2
    assert r2["renamed"] == 1
    assert len(r2["errors"]) == 1
    assert r2["errors"][0]["session_id"] == sidA
    assert "already used" in r2["errors"][0]["error"]

    # sidA's row is untouched — the collision aborted just that row.
    untouched = query_sessions(conn, session_id=sidA)
    assert len(untouched) == 1
    assert untouched[0]["target"] == "IC 4604_1-1"

    merged = query_sessions(conn, target="IC 4604")
    assert {r["session_id"] for r in merged} == {
        "IC4604_20260219_FRA400_ZWOASI585MCPro_L-Pro",  # sidB, from r1
        "IC4604_20260220_FRA400_ZWOASI585MCPro_L-Pro",  # sidC, from r2
    }


def test_rename_target_case_and_spacing_insensitive_match(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db, _session(sid, target="M 81", obs_date="2026-02-19"))
    conn = open_db(db)

    result = rename_target(conn, "m81", "M 82")
    assert result == {"renamed": 1, "errors": [], "total": 1}
    assert len(query_sessions(conn, target="M 82")) == 1


def test_rename_target_noop_when_normalized_equal(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db, _session(sid, target="M 81", obs_date="2026-02-19"))
    conn = open_db(db)

    result = rename_target(conn, "M81", "M 81")
    assert result == {"renamed": 0, "errors": [], "total": 0}

    rows = query_sessions(conn, session_id=sid)
    assert rows[0]["target"] == "M 81"


def test_rename_target_empty_new_target_raises(tmp_path):
    conn = open_db(make_db(tmp_path))
    with pytest.raises(ValueError):
        rename_target(conn, "M 81", "   ")


def test_rename_target_fixes_denormalized_stored_target(tmp_path):
    """Normalization-drift fix: old and new share a normalized form, but the
    stored rows are denormalized ('SH2-103' vs canonical 'Sh2-103') — the
    rename must still reach them instead of short-circuiting as a no-op."""
    db = tmp_path / "test.db"
    init_db(db)
    sid = "Sh2-103_20250622_FMA180_Canon6D_NoFilter"
    upsert_session(db, _session(
        sid, target="Sh2-103", obs_date="2025-06-22", ota="FMA180",
        camera="Canon6D", filter="NoFilter",
    ))
    conn = open_db(db)
    # Denormalize the stored value directly — upserts normalize on the way in.
    conn.execute("UPDATE sessions SET target = 'SH2-103' WHERE session_id = ?", (sid,))
    conn.commit()

    result = rename_target(conn, "SH2-103", "Sh2-103")
    assert result["renamed"] == 1
    assert result["errors"] == []
    assert result["total"] == 1

    rows = query_sessions(conn, target="Sh2-103")
    assert len(rows) == 1
    assert rows[0]["target"] == "Sh2-103"

    # Rows already in canonical form are skipped, so re-running is a no-op.
    again = rename_target(conn, "SH2-103", "Sh2-103")
    assert again == {"renamed": 0, "errors": [], "total": 0}


# ---------------------------------------------------------------------------
# sites (S1)
# ---------------------------------------------------------------------------

def test_add_site_and_list_sites_round_trip(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="Palmela", lat=38.563, lon=-8.881)
    sites = list_sites(conn)
    assert len(sites) == 1
    assert sites[0]["name"] == "Palmela"
    assert sites[0]["lat"] == 38.563
    assert sites[0]["lon"] == -8.881
    assert sites[0]["radius_m"] == 1000
    assert sites[0]["is_home"] == 0


def test_add_site_duplicate_name_raises(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="Palmela", lat=38.563, lon=-8.881)
    with pytest.raises(ValueError):
        add_site(conn, name="Palmela", lat=38.6, lon=-8.9)


def test_update_site_fields_sets_sqm_and_bortle(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="Palmela", lat=38.563, lon=-8.881)
    assert update_site_fields(conn, "Palmela", sqm=20.9, bortle=4) is True
    sites = list_sites(conn)
    assert sites[0]["sqm"] == 20.9
    assert sites[0]["bortle"] == 4


def test_update_site_fields_renames(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="Palmela", lat=38.563, lon=-8.881)
    assert update_site_fields(conn, "Palmela", name="Palmela Dark Site") is True
    names = {s["name"] for s in list_sites(conn)}
    assert names == {"Palmela Dark Site"}


def test_update_site_fields_unknown_field_raises(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="Palmela", lat=38.563, lon=-8.881)
    with pytest.raises(ValueError):
        update_site_fields(conn, "Palmela", not_a_field=1)


def test_update_site_fields_missing_site_returns_false(tmp_path):
    conn = open_db(make_db(tmp_path))
    assert update_site_fields(conn, "NoSuchSite", sqm=20.0) is False


def test_add_site_home_reassignment(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="A", lat=38.563, lon=-8.881, is_home=True)
    add_site(conn, name="B", lat=38.444, lon=-8.378, is_home=True)
    sites = {s["name"]: s for s in list_sites(conn)}
    assert sites["A"]["is_home"] == 0
    assert sites["B"]["is_home"] == 1


def test_update_site_fields_home_reassignment(tmp_path):
    conn = open_db(make_db(tmp_path))
    add_site(conn, name="A", lat=38.563, lon=-8.881, is_home=True)
    add_site(conn, name="B", lat=38.444, lon=-8.378, is_home=False)
    update_site_fields(conn, "A", is_home=True)  # A is already home: still true
    sites = {s["name"]: s for s in list_sites(conn)}
    assert sites["A"]["is_home"] == 1
    assert sites["B"]["is_home"] == 0

    update_site_fields(conn, "B", is_home=True)
    sites = {s["name"]: s for s in list_sites(conn)}
    assert sites["A"]["is_home"] == 0
    assert sites["B"]["is_home"] == 1


def test_update_session_fields_site_lat_lon_round_trip(tmp_path):
    db = make_db(tmp_path)
    conn = open_db(db)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    assert update_session_fields(conn, sid, site_lat=38.563, site_lon=-8.881) is True
    rows = query_sessions(conn, session_id=sid)
    assert rows[0]["site_lat"] == 38.563
    assert rows[0]["site_lon"] == -8.881


# ---------------------------------------------------------------------------
# import isolation
# ---------------------------------------------------------------------------

def test_importing_catalog_db_does_not_pull_in_astropy():
    """Mirrors tests/test_catalog.py's isolation test — catalog_db must not
    pay astropy's import cost. Run in a subprocess for a clean sys.modules
    (other test files in this session import astropy-heavy modules)."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import darkroom.catalog_db, sys; "
         "assert 'astropy' not in sys.modules, sorted(k for k in sys.modules if 'astropy' in k)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
