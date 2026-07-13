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
