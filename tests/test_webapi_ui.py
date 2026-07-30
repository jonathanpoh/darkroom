"""Tests for the W9 phase-2 Jinja2 catalog edit UI (darkroom.webapi.ui)."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from darkroom import catalog_db
from darkroom.cataloger import (
    upsert_calibration_set,
    upsert_session,
    upsert_session_guiding,
)
from darkroom.webapi import auth
from darkroom.webapi.app import create_app
from darkroom.webapi.auth import hash_password
from darkroom.webapi.ui import (
    _build_aggregate,
    _guiding_summary,
    _is_spike_dominated,
    _target_suggestions,
    reset_login_rate_limit,
)

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
    upsert_session(
        db_path,
        _session(
            known_sid, obs_date="2026-02-21", filter="L-Pro",
            site_lat=38.563, site_lon=-8.881,
        ),
    )
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
        _session(
            sid, filter=None, obs_date="2026-02-19",
            site_lat=38.563, site_lon=-8.881,
        ),
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


# ---------------------------------------------------------------------------
# calibration-match indicator (F3)
# ---------------------------------------------------------------------------


def _calibrated(db_path, camera="ZWOASI585MCPro"):
    """A dark, a flat and a flat dark that all match _session()'s defaults."""
    upsert_calibration_set(db_path, _cal_set(
        "Dark_set", frame_type="Dark", camera=camera, ota=None, filter=None,
        exposure_sec=180.0, capture_date="2026-02-01",
        folder_path="00_Calibration/Darks/" + camera,
    ))
    upsert_calibration_set(db_path, _cal_set(
        "Flat_set", frame_type="Flat", camera=camera, capture_date="2026-02-20",
    ))
    upsert_calibration_set(db_path, _cal_set(
        "FlatDark_set", frame_type="FlatDark", camera=camera, ota=None, filter=None,
        capture_date="2026-02-20",
        folder_path="00_Calibration/FlatDarks/" + camera,
    ))


def test_target_page_embeds_calibration_per_night(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    _calibrated(db_path)
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["cal"]["darks"]["status"] == "ok"
    assert night["cal"]["flats"]["status"] == "ok"
    assert night["cal"]["flat_darks"]["status"] == "ok"


def test_target_page_flags_missing_darks(tmp_path):
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    _calibrated(db_path)
    # Same camera, wrong exposure — "missing", not "not used".
    conn = catalog_db.open_db(db_path)
    try:
        conn.execute(
            "UPDATE calibration_sets SET exposure_sec = 60.0 WHERE frame_type = 'Dark'"
        )
        conn.commit()
    finally:
        conn.close()
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["cal"]["darks"]["status"] == "missing"


def test_overview_does_not_compute_calibration(tmp_path):
    """The overview shows no calibration state, so it shouldn't pay to match
    every session on the page."""
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    _calibrated(db_path)
    login(client)

    assert "cal" not in _embedded_data(client.get("/").text)[0]["nights"][0]


def test_build_aggregate_shape_unchanged_without_cal_rows(tmp_path):
    """`cal_rows` is optional the same way `sites` is — fixtures and callers
    that don't pass it keep the previous night dict exactly."""
    rows = [_session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro")]
    rows[0]["processed_state"] = "unprocessed"
    assert "cal" not in _build_aggregate(rows)[0]["nights"][0]


def test_session_page_renders_calibration_panel(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _calibrated(db_path)
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert "Calibration match" in html
    assert "Flat_set" in html  # the matched set is named, not just a status
    assert "+1 day (morning after)" in html


def test_session_page_calibration_survives_a_validation_error(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _calibrated(db_path)
    login(client)

    resp = client.post(f"/sessions/{sid}", data={"gain": "not-a-number"})
    assert resp.status_code == 400
    assert "Calibration match" in resp.text


def test_unknown_ota_session_is_not_reported_as_calibrated(tmp_path):
    """An Unknown OTA can't be matched against flats — the indicator must say
    so rather than matching every scope's flats."""
    client, db_path = make_client(tmp_path)
    upsert_session(
        db_path,
        _session("M81_20260219_Unknown_ZWOASI585MCPro_L-Pro", ota="Unknown"),
    )
    _calibrated(db_path)
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["cal"]["flats"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# guiding indicator (F4)
# ---------------------------------------------------------------------------


def _guided(db_path, session_id, **extra):
    """A session_guiding row for `session_id`, good-band by default."""
    row = {
        "session_id": session_id,
        "rms_ra_arcsec": 0.63,
        "rms_dec_arcsec": 0.67,
        "rms_total_arcsec": 0.92,
        "peak_arcsec": 3.41,
        "p95_arcsec": 1.88,
        "guide_frames": 4210,
        "excluded_frames": 180,
        "dropped_frames": 12,
        "star_lost_events": 3,
        "dither_count": 24,
        "guided_sec": 12600,
        "coverage": 0.94,
        "pixel_scale_arcsec": 6.45,
        "guide_camera": "ZWO ASI120MM Mini",
        "guide_exposure_ms": 2000,
        "source_logs": ["PHD2_GuideLog_2026-02-19_220000.txt"],
    }
    row.update(extra)
    upsert_session_guiding(db_path, row)


def test_target_page_embeds_guiding_per_night(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid)
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["guiding"]["rms"] == pytest.approx(0.92)
    assert night["guiding"]["ra"] == pytest.approx(0.63)
    assert night["guiding"]["dec"] == pytest.approx(0.67)
    assert night["guiding"]["peak"] == pytest.approx(3.41)
    assert night["guiding"]["p95"] == pytest.approx(1.88)
    assert night["guiding"]["cov"] == pytest.approx(0.94)
    assert night["guiding"]["lost"] == 3
    assert night["guiding"]["dropped"] == 12
    # source_logs is stored as a JSON array; the client gets a real list.
    assert night["guiding"]["logs"] == ["PHD2_GuideLog_2026-02-19_220000.txt"]


def test_target_page_night_without_guiding_is_null_not_missing(tmp_path):
    """Most sessions have no guide log. The key must still be there, as null —
    the renderer shows an em-dash, which means "not measured", not "bad"."""
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert "guiding" in night
    assert night["guiding"] is None


def test_target_page_guiding_only_lands_on_its_own_session(tmp_path):
    client, db_path = make_client(tmp_path)
    guided_sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    other_sid = "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(guided_sid))
    upsert_session(db_path, _session(other_sid, obs_date="2026-02-20"))
    _guided(db_path, guided_sid)
    login(client)

    nights = {
        n["sid"]: n
        for n in _embedded_data(client.get("/targets/M%2081").text)[0]["nights"]
    }
    assert nights[guided_sid]["guiding"]["rms"] == pytest.approx(0.92)
    assert nights[other_sid]["guiding"] is None


def test_overview_does_not_compute_guiding(tmp_path):
    """Same as calibration: the overview shows no guiding, so it shouldn't
    query for it."""
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid)
    login(client)

    assert "guiding" not in _embedded_data(client.get("/").text)[0]["nights"][0]


def test_build_aggregate_shape_unchanged_without_guiding_rows(tmp_path):
    rows = [_session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro")]
    rows[0]["processed_state"] = "unprocessed"
    assert "guiding" not in _build_aggregate(rows)[0]["nights"][0]


def test_guiding_summary_treats_a_row_without_rms_as_not_measured(tmp_path):
    assert _guiding_summary(None) is None
    assert _guiding_summary({"session_id": "s1", "rms_total_arcsec": None}) is None


def test_session_page_renders_guiding_panel(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid)
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert "Guiding" in html
    assert "0.92" in html
    assert "94% of the session" in html
    assert "PHD2_GuideLog_2026-02-19_220000.txt" in html
    assert "partial log" not in html  # 94% coverage is not partial


def test_session_page_flags_partial_coverage(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid, coverage=0.42)
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert "42% of the session" in html
    assert "partial log" in html


def test_session_page_without_guiding_omits_the_panel(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert "Total RMS" not in html


def test_session_page_guiding_survives_a_validation_error(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid)
    login(client)

    resp = client.post(f"/sessions/{sid}", data={"gain": "not-a-number"})
    assert resp.status_code == 400
    assert "Total RMS" in resp.text


# ---------------------------------------------------------------------------
# spike-dominated marker (F4) — presentation only, nothing stored changes
# ---------------------------------------------------------------------------

# Real sessions from the live catalog, kept as the fixtures for this rule.
SPIKED = dict(rms_total_arcsec=19.18, p95_arcsec=2.11, peak_arcsec=351.0)  # NGC 6888 2026-07-20
UNIFORMLY_BAD = dict(rms_total_arcsec=35.30, p95_arcsec=28.30, peak_arcsec=96.4)  # M 45 2025-09-22


def test_is_spike_dominated_rule_and_guards():
    assert _is_spike_dominated(19.18, 2.11) is True        # rms/p95 = 9.1
    assert _is_spike_dominated(35.30, 28.30) is False       # 1.2 — uniformly bad
    assert _is_spike_dominated(0.92, 1.88) is False         # 0.5 — clean
    assert _is_spike_dominated(4.0, 2.0) is True            # exactly 2× counts
    assert _is_spike_dominated(19.18, 0.0) is False         # p95 <= 0 guard
    assert _is_spike_dominated(19.18, None) is False
    assert _is_spike_dominated(None, 2.11) is False


def test_night_payload_flags_a_spike_dominated_session(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid, **SPIKED)
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["guiding"]["spike"] is True
    # the value and its band inputs are untouched — only the annotation is new
    assert night["guiding"]["rms"] == pytest.approx(19.18)
    assert night["guiding"]["p95"] == pytest.approx(2.11)
    assert night["guiding"]["peak"] == pytest.approx(351.0)


def test_night_payload_does_not_flag_a_uniformly_bad_session(tmp_path):
    """High RMS *and* high p95 is a genuinely bad night — it must keep reading bad."""
    client, db_path = make_client(tmp_path)
    sid = "M45_20250922_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid, target="M 45", obs_date="2025-09-22"))
    _guided(db_path, sid, **UNIFORMLY_BAD)
    login(client)

    night = _embedded_data(client.get("/targets/M%2045").text)[0]["nights"][0]
    assert night["guiding"]["spike"] is False


def test_night_payload_does_not_flag_a_clean_session(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid)  # 0.92 RMS / 1.88 p95
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["guiding"]["spike"] is False


def test_night_without_a_guiding_row_still_renders_the_em_dash(tmp_path):
    """No guide log means "not measured", not "spiked" and not "bad": the payload
    stays null and the renderer's null branch is the em-dash."""
    client, db_path = make_client(tmp_path)
    upsert_session(db_path, _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    login(client)

    night = _embedded_data(client.get("/targets/M%2081").text)[0]["nights"][0]
    assert night["guiding"] is None
    js = client.get("/static/app.js").text
    assert 'if (!g || g.rms == null) return `<span class="guide none">—</span>`;' in js
    # the marker is a straight read of the server flag — no threshold in the JS
    assert "if (g.spike)" in js
    assert re.search(r"g\.spike \? .*class=\\?\"spike\\?\"", js)


def test_session_page_marks_a_spike_dominated_session(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "NGC6888_20260720_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid, target="NGC 6888", obs_date="2026-07-20"))
    _guided(db_path, sid, **SPIKED)
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert 'class="spike"' in html
    assert "spike-dominated: most frames near 2.11" in html
    assert "worst 351.00" in html
    assert "a few bad subs rather than a bad night" in html
    assert "19.18" in html  # the RMS itself is unchanged


def test_session_page_does_not_mark_a_uniformly_bad_session(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M45_20250922_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid, target="M 45", obs_date="2025-09-22"))
    _guided(db_path, sid, **UNIFORMLY_BAD)
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert "35.30" in html
    assert 'class="spike"' not in html
    assert "spike-dominated" not in html


def test_session_page_does_not_mark_a_clean_session(tmp_path):
    client, db_path = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    upsert_session(db_path, _session(sid))
    _guided(db_path, sid)
    login(client)

    html = client.get(f"/sessions/{sid}").text
    assert 'class="spike"' not in html
    assert "spike-dominated" not in html
