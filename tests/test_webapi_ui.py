"""Tests for the W9 phase-2 Jinja2 catalog edit UI (darkroom.webapi.ui)."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from darkroom import catalog_db
from darkroom.cataloger import upsert_session
from darkroom.webapi import auth
from darkroom.webapi.app import create_app
from darkroom.webapi.auth import hash_password
from darkroom.webapi.ui import reset_login_rate_limit

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
