"""Tests for the W9 phase-2 Jinja2 catalog edit UI (darkroom.webapi.ui)."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from darkroom import catalog_db
from darkroom.cataloger import upsert_calibration_set, upsert_session
from darkroom.webapi import auth
from darkroom.webapi.app import create_app
from darkroom.webapi.auth import hash_password
from darkroom.webapi.ui import _build_aggregate, reset_login_rate_limit, _target_suggestions

TOKEN = "testtoken"
UI_PASSWORD = "test-password"
UI_HASH = hash_password(UI_PASSWORD)  # scrypt is slow — hash once at module level


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    reset_login_rate_limit()
    yield
    reset_login_rate_limit()


def _embedded_data(html: str) -> list[dict]:
    """Pull the `const DATA = [...]` JSON blob out of a rendered safelight page."""
    m = re.search(r"const DATA = (.*?);\n", html, re.DOTALL)
    assert m, "page did not embed a `const DATA = ...;` script"
    return json.loads(m.group(1))


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


def _cal_set(set_id, frame_type="Flat", camera="ZWOASI585MCPro", ota="FRA400", **extra):
    base = {
        "set_id": set_id,
        "frame_type": frame_type,
        "camera": camera,
        "ota": ota,
        "filter": "L-Pro",
        "gain": 200,
        "exposure_sec": 0.02,
        "temperature_c": -20.0,
        "frame_count": 30,
        "capture_date": "2026-02-19",
        "folder_path": "00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-19",
    }
    base.update(extra)
    return base


def make_client(tmp_path) -> tuple[TestClient, "Path"]:
    db_path = tmp_path / "catalog.db"
    app = create_app(db_path, TOKEN, UI_HASH)
    return TestClient(app), db_path


def login(client: TestClient) -> None:
    resp = client.post(
        "/login", data={"password": UI_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert resp.status_code == 303
    cookie = resp.cookies.get("darkroom_token")
    assert cookie is not None
    assert auth.verify_cookie(UI_HASH, cookie)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_index_unauthenticated_redirects_to_login(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_wrong_password_rerenders_error(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.post("/login", data={"password": "wrong", "next": "/"})
    assert resp.status_code == 400
    assert "Invalid password" in resp.text
    assert "darkroom_token" not in resp.cookies


def test_login_correct_password_sets_cookie_and_index_renders(tmp_path):
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


def test_raw_api_token_as_cookie_does_not_authenticate(tmp_path):
    client, _ = make_client(tmp_path)
    client.cookies.set("darkroom_token", TOKEN)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_tampered_cookie_redirects_to_login(tmp_path):
    client, _ = make_client(tmp_path)
    login(client)
    good_cookie = client.cookies.get("darkroom_token")
    expiry, sig = good_cookie.split(".", 1)
    tampered = f"{expiry}.{'f' * len(sig)}"
    client.cookies.set("darkroom_token", tampered)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_expired_cookie_redirects_to_login(tmp_path):
    client, _ = make_client(tmp_path)
    expired = auth.mint_cookie(UI_HASH, max_age_seconds=-1)
    client.cookies.set("darkroom_token", expired)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_query_param_token_no_longer_logs_in(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get(f"/login?token={TOKEN}", follow_redirects=False)
    assert resp.status_code == 200
    assert "darkroom_token" not in resp.cookies
    # Confirm we're actually still logged out.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_sliding_refresh_resets_cookie_on_authenticated_hit(tmp_path):
    client, _ = make_client(tmp_path)
    login(client)
    first_cookie = client.cookies.get("darkroom_token")

    resp = client.get("/")
    assert resp.status_code == 200
    refreshed_cookie = resp.cookies.get("darkroom_token")
    assert refreshed_cookie is not None
    assert auth.verify_cookie(UI_HASH, refreshed_cookie)


def test_login_rate_limit_blocks_after_five_failures(tmp_path):
    client, _ = make_client(tmp_path)
    for _ in range(5):
        resp = client.post("/login", data={"password": "wrong", "next": "/"})
        assert resp.status_code == 400

    resp = client.post("/login", data={"password": "wrong", "next": "/"})
    assert resp.status_code == 429


def test_login_rate_limit_blocks_correct_password_while_throttled(tmp_path):
    client, _ = make_client(tmp_path)
    for _ in range(5):
        client.post("/login", data={"password": "wrong", "next": "/"})

    resp = client.post("/login", data={"password": UI_PASSWORD, "next": "/"})
    assert resp.status_code == 429
    assert "darkroom_token" not in resp.cookies


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
    assert "ZWOASI585MCPro" in text  # static shell references app.js, which renders these client-side
    assert '<script src="/static/app.js"></script>' in text

    data = _embedded_data(text)
    by_target = {t["target"]: t for t in data}
    assert set(by_target) == {"M 81", "NGC 7000"}

    m81 = by_target["M 81"]
    assert m81["n"] == 2
    assert m81["last"] == "2026-02-20"
    assert {n["date"] for n in m81["nights"]} == {"2026-02-19", "2026-02-20"}
    assert all(n["ota"] == "FRA400" and n["camera"] == "ZWOASI585MCPro" for n in m81["nights"])

    ngc = by_target["NGC 7000"]
    assert ngc["n"] == 1
    assert ngc["hours"] == {"L-Extreme": pytest.approx(5.0)}


def test_index_embeds_aggregate_with_cname_hours_and_states(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    upsert_session(
        db_path,
        _session(
            "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme",
            obs_date="2026-02-20",
            filter="L-Extreme",
        ),
    )
    login(client)

    resp = client.get("/")
    assert resp.status_code == 200
    data = _embedded_data(resp.text)
    m81 = next(t for t in data if t["target"] == "M 81")

    assert m81["cname"] == "Bode's Galaxy"
    assert set(m81["hours"]) == {"L-Pro", "L-Extreme"}
    assert m81["hours"]["L-Pro"] == pytest.approx(5.0)
    assert m81["hours"]["L-Extreme"] == pytest.approx(5.0)
    assert m81["total_h"] == pytest.approx(10.0)
    assert m81["states"] == {"unprocessed": 2}


# ---------------------------------------------------------------------------
# target detail view
# ---------------------------------------------------------------------------


def test_target_detail_scoped_to_one_target(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
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

    resp = client.get("/targets/M%2081")
    assert resp.status_code == 200
    assert '<script src="/static/app.js"></script>' in resp.text
    assert "DETAIL_TARGET" in resp.text

    data = _embedded_data(resp.text)
    assert len(data) == 1
    assert data[0]["target"] == "M 81"
    assert "NGC 7000" not in resp.text  # scoped strictly to the requested target


def test_target_detail_unknown_target_404(tmp_path):
    client, _ = make_client(tmp_path)
    login(client)
    resp = client.get("/targets/M%2099999")
    assert resp.status_code == 404


def test_target_detail_unauthenticated_redirects(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    resp = client.get("/targets/M%2081", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# static assets
# ---------------------------------------------------------------------------


def test_static_css_and_font_served_without_auth(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/static/safelight.css")
    assert resp.status_code == 200
    resp = client.get("/static/fonts/D-DIN.woff2")
    assert resp.status_code == 200


def test_login_page_renders_without_auth(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "DARKR" in resp.text
    assert 'name="password"' in resp.text


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


# ---------------------------------------------------------------------------
# session delete (POST /sessions/{session_id}/delete)
# ---------------------------------------------------------------------------


def test_delete_unauthenticated_redirects_to_login(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))

    resp = client.post(f"/sessions/{sid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")

    # Row untouched.
    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, session_id=sid)
    finally:
        conn.close()
    assert len(rows) == 1


def test_delete_redirects_to_target_when_other_sessions_remain(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    upsert_session(
        db_path,
        _session(
            "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro",
            obs_date="2026-02-20",
        ),
    )
    login(client)

    resp = client.post(f"/sessions/{sid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/targets/M%2081"

    conn = catalog_db.open_db(db_path)
    try:
        gone = catalog_db.query_sessions(conn, session_id=sid)
        remaining = catalog_db.query_sessions(conn, target="M 81")
    finally:
        conn.close()
    assert gone == []
    assert len(remaining) == 1


def test_delete_last_session_of_target_redirects_to_index(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    resp = client.post(f"/sessions/{sid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, session_id=sid)
    finally:
        conn.close()
    assert rows == []


def test_delete_unknown_session_404(tmp_path):
    client, _ = make_client(tmp_path)
    login(client)
    resp = client.post("/sessions/does-not-exist/delete", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# filter-assignment cleanup queue (U2 phase 2, GET /queue, POST .../fix)
# ---------------------------------------------------------------------------


def test_queue_unauthenticated_redirects_to_login(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/queue", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_queue_lists_null_and_unknown_filter_but_not_known_filter(tmp_path):
    client, db_path = make_client(tmp_path)
    null_sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(db_path, _session(null_sid, filter=None, obs_date="2026-02-19"))
    unknown_sid = "M81_20260220_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(unknown_sid, filter="UnknownFilter", obs_date="2026-02-20"),
    )
    known_sid = "M81_20260221_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(known_sid, obs_date="2026-02-21", filter="L-Pro"))
    login(client)

    resp = client.get("/queue")
    assert resp.status_code == 200
    assert null_sid in resp.text
    assert unknown_sid in resp.text
    assert known_sid not in resp.text
    assert "2 sessions need review" in resp.text  # total_count header


def test_queue_suspicious_value_section(tmp_path):
    client, db_path = make_client(tmp_path)
    garbage_sid = "IC4604_20260219_FRA400_ZWOASI585MCPro_IC4604_1-1"
    upsert_session(
        db_path,
        _session(garbage_sid, target="IC 4604", filter="IC4604_1-1", obs_date="2026-02-19"),
    )
    login(client)

    resp = client.get("/queue")
    assert resp.status_code == 200
    assert garbage_sid in resp.text
    assert "Suspicious value" in resp.text
    assert "IC4604_1-1" in resp.text


def test_queue_unknown_ota_badge(tmp_path):
    client, db_path = make_client(tmp_path)
    bad_ota_sid = "M81_20260219_Unknown_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(bad_ota_sid, ota="Unknown", filter=None, obs_date="2026-02-19"),
    )
    ok_ota_sid = "M81_20260220_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(ok_ota_sid, ota="FRA400", filter=None, obs_date="2026-02-20"),
    )
    login(client)

    resp = client.get("/queue")
    assert resp.status_code == 200
    # Both suspect rows render; only the bad-OTA one carries the badge.
    bad_block = resp.text.split(bad_ota_sid, 1)[1].split("qrow", 1)[0]
    ok_block = resp.text.split(ok_ota_sid, 1)[1].split("qrow", 1)[0]
    assert "unknown OTA" in bad_block
    assert "unknown OTA" not in ok_block


def test_queue_neighbour_filter_hint(tmp_path):
    client, db_path = make_client(tmp_path)
    suspect_sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(suspect_sid, filter=None, obs_date="2026-02-19"),
    )
    neighbour_sid = "M81_20260221_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(
        db_path,
        _session(neighbour_sid, filter="L-Pro", obs_date="2026-02-21"),
    )
    login(client)

    resp = client.get("/queue")
    assert resp.status_code == 200
    assert "L-Pro" in resp.text
    assert "±2d" in resp.text


def test_queue_flat_hint(tmp_path):
    client, db_path = make_client(tmp_path)
    suspect_sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(suspect_sid, filter=None, obs_date="2026-02-19"),
    )
    upsert_calibration_set(
        db_path,
        _cal_set(
            "Flat_FRA400_ZWOASI585MCPro_L-Extreme_20260222",
            filter="L-Extreme",
            capture_date="2026-02-22",
        ),
    )
    login(client)

    resp = client.get("/queue")
    assert resp.status_code == 200
    assert "flats:" in resp.text
    assert "L-Extreme" in resp.text
    assert "2026-02-22" in resp.text


def test_queue_fix_valid_filter_updates_row_and_creates_pending_rename(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(sid, filter=None, obs_date="2026-02-19"),
    )
    login(client)

    resp = client.post(
        f"/queue/{sid}/fix", data={"filter": "L-Extreme"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/queue"

    conn = catalog_db.open_db(db_path)
    try:
        old_rows = catalog_db.query_sessions(conn, session_id=sid)
        new_sid = sid.replace("UnknownFilter", "L-Extreme")
        new_rows = catalog_db.query_sessions(conn, session_id=new_sid)
        pending = catalog_db.list_pending_renames(conn)
    finally:
        conn.close()
    assert old_rows == []
    assert len(new_rows) == 1
    assert new_rows[0]["filter"] == "L-Extreme"
    assert len(pending) == 1
    assert pending[0]["session_id"] == new_sid

    # Fixed row drops out of the queue on reload.
    resp = client.get("/queue")
    assert new_sid not in resp.text


def test_queue_fix_invalid_filter_rejected(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(sid, filter=None, obs_date="2026-02-19"),
    )
    login(client)

    resp = client.post(f"/queue/{sid}/fix", data={"filter": "NotARealFilter"})
    assert resp.status_code == 400

    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, session_id=sid)
    finally:
        conn.close()
    assert rows[0]["filter"] is None


def test_queue_fix_collision_surfaced_not_raised(tmp_path):
    client, db_path = make_client(tmp_path)
    existing_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(existing_sid, filter="L-Pro", obs_date="2026-02-19"))
    suspect_sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(suspect_sid, filter=None, obs_date="2026-02-19"),
    )
    login(client)

    # Fixing suspect_sid's filter to L-Pro recomputes a session_id that
    # collides with existing_sid — update_session_fields raises ValueError,
    # which must be surfaced as an error banner, not a 500.
    resp = client.post(f"/queue/{suspect_sid}/fix", data={"filter": "L-Pro"})
    assert resp.status_code == 400
    assert suspect_sid in resp.text

    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, session_id=suspect_sid)
    finally:
        conn.close()
    assert rows[0]["filter"] is None  # untouched


def test_queue_pending_renames_banner_shown_when_nonempty(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_UnknownFilter"
    upsert_session(
        db_path,
        _session(sid, filter=None, obs_date="2026-02-19"),
    )
    login(client)

    resp = client.get("/queue")
    assert "folder renames pending" not in resp.text

    client.post(f"/queue/{sid}/fix", data={"filter": "L-Extreme"})

    resp = client.get("/queue")
    assert "<b>1</b> folder rename" in resp.text
    assert "darkroom catalog apply-renames" in resp.text


# ---------------------------------------------------------------------------
# SQM site weighting (_build_aggregate, sites) — Phase 4
# ---------------------------------------------------------------------------

HOME_SITE = {
    "name": "Home",
    "lat": 0.0,
    "lon": 0.0,
    "radius_m": 1000.0,
    "bortle": 6,
    "sqm": 19.5,
    "is_home": True,
}
AWAY_SITE = {
    "name": "Dark Site",
    "lat": 10.0,
    "lon": 10.0,
    "radius_m": 1000.0,
    "bortle": 3,
    "sqm": 22.0,
    "is_home": False,
}


def test_build_aggregate_no_sites_arg_all_weight_one():
    rows = [_session("sid1", site_lat=10.0, site_lon=10.0, processed_state="unprocessed")]
    agg = _build_aggregate(rows)
    night = agg[0]["nights"][0]
    assert night["w"] == 1.0
    assert night["wh"] == pytest.approx(night["h"])
    assert agg[0]["total_wh"] == pytest.approx(agg[0]["total_h"])


def test_build_aggregate_home_and_away_sites_weight_by_sqm_ratio():
    home_sid = "sidHome"
    away_sid = "sidAway"
    rows = [
        _session(home_sid, obs_date="2026-02-19", site_lat=0.0001, site_lon=0.0001, processed_state="unprocessed"),
        _session(away_sid, obs_date="2026-02-20", site_lat=10.0001, site_lon=10.0001, processed_state="unprocessed"),
    ]
    sites = [HOME_SITE, AWAY_SITE]
    agg = _build_aggregate(rows, sites)
    nights = {n["sid"]: n for n in agg[0]["nights"]}

    home_night = nights[home_sid]
    assert home_night["site"] == "Home"
    assert home_night["w"] == pytest.approx(1.0)
    assert home_night["wh"] == pytest.approx(home_night["h"])

    away_night = nights[away_sid]
    assert away_night["site"] == "Dark Site"
    assert away_night["w"] == pytest.approx(10.0)
    assert away_night["wh"] == pytest.approx(10.0 * away_night["h"])

    expected_total_wh = home_night["wh"] + away_night["wh"]
    assert agg[0]["total_wh"] == pytest.approx(expected_total_wh)
    assert agg[0]["total_h"] == pytest.approx(home_night["h"] + away_night["h"])


def test_build_aggregate_away_site_missing_sqm_weight_one():
    away_no_sqm = dict(AWAY_SITE, sqm=None)
    rows = [_session("sid1", site_lat=10.0001, site_lon=10.0001, processed_state="unprocessed")]
    agg = _build_aggregate(rows, [HOME_SITE, away_no_sqm])
    night = agg[0]["nights"][0]
    assert night["site"] == "Dark Site"
    assert night["w"] == 1.0
    assert night["wh"] == pytest.approx(night["h"])


def test_build_aggregate_no_home_site_weight_one():
    rows = [_session("sid1", site_lat=10.0001, site_lon=10.0001, processed_state="unprocessed")]
    agg = _build_aggregate(rows, [AWAY_SITE])  # no is_home site at all
    night = agg[0]["nights"][0]
    assert night["site"] == "Dark Site"
    assert night["w"] == 1.0
    assert night["wh"] == pytest.approx(night["h"])
    assert agg[0]["total_wh"] == pytest.approx(agg[0]["total_h"])


def test_build_aggregate_null_coords_no_site_weight_one():
    rows = [_session("sid1", site_lat=None, site_lon=None, processed_state="unprocessed")]
    agg = _build_aggregate(rows, [HOME_SITE, AWAY_SITE])
    night = agg[0]["nights"][0]
    assert night["site"] is None
    assert night["w"] == 1.0
    assert night["wh"] == pytest.approx(night["h"])


def test_index_page_embeds_weighted_hours_and_site_name(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(
        db_path,
        _session(sid, obs_date="2026-02-20", site_lat=10.0001, site_lon=10.0001),
    )
    conn = catalog_db.open_db(db_path)
    try:
        catalog_db.add_site(conn, **HOME_SITE)
        catalog_db.add_site(conn, **AWAY_SITE)
    finally:
        conn.close()
    login(client)

    resp = client.get("/")
    assert resp.status_code == 200
    data = _embedded_data(resp.text)
    m81 = next(t for t in data if t["target"] == "M 81")
    night = m81["nights"][0]
    assert "wh" in night
    assert night["site"] == "Dark Site"
    assert "total_wh" in m81
    assert m81["total_wh"] == pytest.approx(10.0 * m81["total_h"])
    assert "Dark Site" in resp.text


def test_target_page_embeds_weighted_hours_and_site_name(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(
        db_path,
        _session(sid, obs_date="2026-02-20", site_lat=10.0001, site_lon=10.0001),
    )
    conn = catalog_db.open_db(db_path)
    try:
        catalog_db.add_site(conn, **HOME_SITE)
        catalog_db.add_site(conn, **AWAY_SITE)
    finally:
        conn.close()
    login(client)

    resp = client.get("/targets/M%2081")
    assert resp.status_code == 200
    data = _embedded_data(resp.text)
    night = data[0]["nights"][0]
    assert night["wh"] == pytest.approx(10.0 * night["h"])
    assert night["site"] == "Dark Site"
    assert data[0]["total_wh"] == pytest.approx(10.0 * data[0]["total_h"])


def test_index_page_renders_200_with_sites_present_no_matching_session(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    conn = catalog_db.open_db(db_path)
    try:
        catalog_db.add_site(conn, **HOME_SITE)
        catalog_db.add_site(conn, **AWAY_SITE)
    finally:
        conn.close()
    login(client)

    resp = client.get("/")
    assert resp.status_code == 200
    data = _embedded_data(resp.text)
    m81 = next(t for t in data if t["target"] == "M 81")
    # session has no site_lat/site_lon -> no site match -> weight 1.0
    assert m81["nights"][0]["site"] is None
    assert m81["total_wh"] == pytest.approx(m81["total_h"])


# ---------------------------------------------------------------------------
# _target_suggestions (pure function, U2 phase 3)
# ---------------------------------------------------------------------------


def test_target_suggestions_panel_suffix():
    result = _target_suggestions(["IC 4604_1-1", "IC 4604_1-1", "IC 4604_1-2"])
    by_target = {s["target"]: s for s in result}
    assert by_target["IC 4604_1-1"]["suggested"] == "IC 4604"
    assert by_target["IC 4604_1-1"]["count"] == 2
    assert by_target["IC 4604_1-2"]["suggested"] == "IC 4604"
    assert by_target["IC 4604_1-2"]["count"] == 1


def test_target_suggestions_panel_suffix_suggested_even_if_base_absent():
    # "NGC 6960" isn't itself a target in the input list — still suggested.
    result = _target_suggestions(["NGC 6960_1-1"])
    assert result == [{"target": "NGC 6960_1-1", "suggested": "NGC 6960", "count": 1}]


def test_target_suggestions_duplicated_designation():
    result = _target_suggestions(["M 82 M 82", "M 82 M 82"])
    assert result == [{"target": "M 82 M 82", "suggested": "M 82", "count": 2}]


def test_target_suggestions_two_designations_only_if_base_exists():
    # "M 81" isn't itself a known target here -> ambiguous, no suggestion.
    assert _target_suggestions(["M 81 M 82"]) == []

    # "M 81" IS a known target -> suggest merging "M 81 M 82" into it.
    result = _target_suggestions(["M 81 M 82", "M 81"])
    by_target = {s["target"]: s for s in result}
    assert by_target["M 81 M 82"]["suggested"] == "M 81"
    assert "M 81" not in by_target  # M 81 itself isn't suspect


def test_target_suggestions_normalization_drift():
    result = _target_suggestions(["m81"])
    assert result == [{"target": "m81", "suggested": "M 81", "count": 1}]


def test_target_suggestions_skips_clean_targets():
    assert _target_suggestions(["M 81", "NGC 7380"]) == []


# ---------------------------------------------------------------------------
# target merge/rename (U2 phase 3, /queue Targets section, POST /queue/targets/rename)
# ---------------------------------------------------------------------------


def test_queue_shows_target_suggestions(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(
        db_path,
        _session(
            "IC46041_1_20260219_FRA400_ZWOASI585MCPro_L-Pro",
            target="IC 4604_1-1", obs_date="2026-02-19",
        ),
    )
    login(client)

    resp = client.get("/queue")
    assert resp.status_code == 200
    assert "IC 4604_1-1" in resp.text
    assert "Merge into IC 4604" in resp.text


def test_queue_targets_rename_unauthenticated_redirects(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.post(
        "/queue/targets/rename",
        data={"old_target": "M 81", "new_target": "M 82"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_queue_targets_rename_success_banner(tmp_path):
    client, db_path = make_client(tmp_path)
    sid1 = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    sid2 = "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme"
    upsert_session(db_path, _session(sid1, target="M 81", obs_date="2026-02-19"))
    upsert_session(
        db_path,
        _session(sid2, target="M 81", obs_date="2026-02-20", filter="L-Extreme"),
    )
    login(client)

    resp = client.post(
        "/queue/targets/rename", data={"old_target": "M 81", "new_target": "M 82"}
    )
    assert resp.status_code == 200
    assert "renamed 2 sessions of M 81" in resp.text
    assert "M 82" in resp.text

    conn = catalog_db.open_db(db_path)
    try:
        rows = catalog_db.query_sessions(conn, target="M 82")
    finally:
        conn.close()
    assert len(rows) == 2


def test_queue_targets_rename_unknown_target_error_banner(tmp_path):
    client, _ = make_client(tmp_path)
    login(client)

    resp = client.post(
        "/queue/targets/rename",
        data={"old_target": "Nonexistent", "new_target": "M 82"},
    )
    assert resp.status_code == 404
    assert "Nonexistent" in resp.text


def test_queue_targets_rename_partial_failure_lists_per_session_errors(tmp_path):
    client, db_path = make_client(tmp_path)
    sidA, sidB, sidC = "sidA", "sidB", "sidC"
    upsert_session(
        db_path,
        _session(sidA, target="IC 4604_1-1", obs_date="2026-02-19", filter="L-Pro"),
    )
    upsert_session(
        db_path,
        _session(sidB, target="IC 4604_2-1", obs_date="2026-02-19", filter="L-Pro"),
    )
    upsert_session(
        db_path,
        _session(sidC, target="IC 4604_1-1", obs_date="2026-02-20", filter="L-Pro"),
    )
    login(client)

    # Merge the _2-1 panel into the base first, landing a row that the
    # second merge will collide with.
    resp = client.post(
        "/queue/targets/rename",
        data={"old_target": "IC 4604_2-1", "new_target": "IC 4604"},
    )
    assert resp.status_code == 200

    resp = client.post(
        "/queue/targets/rename",
        data={"old_target": "IC 4604_1-1", "new_target": "IC 4604"},
    )
    assert resp.status_code == 200  # partial success: one renamed, one errored
    assert "renamed 1 session of IC 4604_1-1" in resp.text
    assert sidA in resp.text
    assert "failed to merge" in resp.text
