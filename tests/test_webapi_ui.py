"""Tests for the W9 phase-2 Jinja2 catalog edit UI (darkroom.webapi.ui)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from darkroom import catalog_db
from darkroom.cataloger import upsert_session
from darkroom.webapi.app import create_app

TOKEN = "testtoken"


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


def make_client(tmp_path) -> tuple[TestClient, "Path"]:
    db_path = tmp_path / "catalog.db"
    app = create_app(db_path, TOKEN)
    return TestClient(app), db_path


def login(client: TestClient) -> None:
    resp = client.post("/login", data={"token": TOKEN, "next": "/"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.cookies.get("darkroom_token") == TOKEN


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_index_unauthenticated_redirects_to_login(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_wrong_token_rerenders_error(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.post("/login", data={"token": "wrong", "next": "/"})
    assert resp.status_code == 400
    assert "Invalid token" in resp.text
    assert "darkroom_token" not in resp.cookies


def test_login_correct_token_sets_cookie_and_index_renders(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))

    login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "M 81" in resp.text


def test_api_routes_require_bearer_not_cookie(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    login(client)

    # Cookie alone (no Authorization header) must not authorize /api.
    resp = client.get("/api/sessions")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# index view
# ---------------------------------------------------------------------------


def test_index_groups_by_target_shows_camera_and_ota(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    upsert_session(
        db_path,
        _session(
            "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro",
            obs_date="2026-02-20",
        ),
    )
    upsert_session(
        db_path,
        _session(
            "NGC7000_20260221_FRA400_ZWOASI585MCPro_L-Extreme",
            target="NGC 7000",
            obs_date="2026-02-21",
            filter="L-Extreme",
        ),
    )
    login(client)

    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert "M 81" in text
    assert "NGC 7000" in text
    assert "ZWOASI585MCPro" in text
    assert "FRA400" in text
    # M 81 group should list its two sessions, most recent obs_date first.
    m81_pos = text.index("M 81")
    d20_pos = text.index("2026-02-20")
    d19_pos = text.index("2026-02-19")
    assert m81_pos < d20_pos < d19_pos


# ---------------------------------------------------------------------------
# one-click state change
# ---------------------------------------------------------------------------


def test_state_change_updates_db_and_redirects(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    resp = client.post(
        f"/sessions/{sid}/state",
        data={"state": "processed", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, session_id=sid)
    finally:
        conn.close()
    assert rows[0]["processed_state"] == "processed"


def test_state_change_invalid_state_400(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    resp = client.post(f"/sessions/{sid}/state", data={"state": "bogus", "next": "/"})
    assert resp.status_code == 400


def test_state_change_unauthenticated_redirects(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))

    resp = client.post(
        f"/sessions/{sid}/state", data={"state": "processed", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# edit view
# ---------------------------------------------------------------------------


def test_edit_page_unknown_session_404(tmp_path):
    client, _ = make_client(tmp_path)
    login(client)
    resp = client.get("/sessions/does-not-exist")
    assert resp.status_code == 404


def test_edit_notes_updates_field(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert sid in resp.text

    form = {
        "target": "M 81",
        "obs_date": "2026-02-19",
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": "200",
        "temperature_c": "-20.0",
        "exposure_sec": "180.0",
        "focal_length": "400.0",
        "ra_deg": "148.89",
        "dec_deg": "69.07",
        "notes": "checked out fine",
        "processed_state": "unprocessed",
        "processed_path": "",
        "processed_date": "",
    }
    resp = client.post(f"/sessions/{sid}", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/sessions/{sid}"

    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, session_id=sid)
    finally:
        conn.close()
    assert rows[0]["notes"] == "checked out fine"


def test_edit_identity_field_renames_session_id(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    form = {
        "target": "M 81",
        "obs_date": "2026-02-19",
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Extreme",
        "gain": "200",
        "temperature_c": "-20.0",
        "exposure_sec": "180.0",
        "focal_length": "400.0",
        "ra_deg": "148.89",
        "dec_deg": "69.07",
        "notes": "",
        "processed_state": "unprocessed",
        "processed_path": "",
        "processed_date": "",
    }
    resp = client.post(f"/sessions/{sid}", data=form, follow_redirects=False)
    assert resp.status_code == 303
    new_location = resp.headers["location"]
    assert new_location != f"/sessions/{sid}"
    assert "L-Extreme" in new_location

    conn = catalog_db.open_db(db_path)
    try:
        old_rows = catalog_db.query_sessions(conn, session_id=sid)
        new_sid = new_location.rsplit("/", 1)[-1]
        new_rows = catalog_db.query_sessions(conn, session_id=new_sid)
    finally:
        conn.close()
    assert old_rows == []
    assert len(new_rows) == 1
    assert new_rows[0]["filter"] == "L-Extreme"


def test_edit_invalid_processed_state_400(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    form = {
        "target": "M 81",
        "obs_date": "2026-02-19",
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": "200",
        "temperature_c": "-20.0",
        "exposure_sec": "180.0",
        "focal_length": "400.0",
        "ra_deg": "148.89",
        "dec_deg": "69.07",
        "notes": "",
        "processed_state": "bogus-state",
        "processed_path": "",
        "processed_date": "",
    }
    resp = client.post(f"/sessions/{sid}", data=form)
    assert resp.status_code == 400
