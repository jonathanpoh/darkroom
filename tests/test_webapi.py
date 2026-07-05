from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from darkroom.webapi.app import create_app, create_app_from_env

TOKEN = "testtoken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_client(tmp_path) -> TestClient:
    app = create_app(tmp_path / "catalog.db", TOKEN)
    return TestClient(app)


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


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_get_sessions_no_auth_header_401(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/sessions")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


def test_get_sessions_wrong_token_401(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_get_sessions_right_token_200(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/sessions", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_post_sessions_no_auth_401(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/sessions", json=_session("abc"))
    assert resp.status_code == 401


def test_post_sessions_right_token_204(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/sessions", json=_session("abc"), headers=AUTH)
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# sessions: create + query
# ---------------------------------------------------------------------------


def test_post_then_get_sessions_roundtrip(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    resp = client.post("/api/sessions", json=_session(sid), headers=AUTH)
    assert resp.status_code == 204

    resp = client.get("/api/sessions", headers=AUTH)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["target"] == "M 81"


def test_get_sessions_target_filter_matches_and_misses(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    client.post("/api/sessions", json=_session(sid), headers=AUTH)

    resp = client.get("/api/sessions", params={"target": "M 81"}, headers=AUTH)
    assert len(resp.json()) == 1

    resp = client.get("/api/sessions", params={"target": "M 999"}, headers=AUTH)
    assert resp.json() == []


def test_get_sessions_count(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/sessions",
        json=_session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"),
        headers=AUTH,
    )
    client.post(
        "/api/sessions",
        json=_session(
            "M81_20260220_FRA400_ZWOASI585MCPro_L-Extreme",
            obs_date="2026-02-20", filter="L-Extreme",
        ),
        headers=AUTH,
    )

    resp = client.get("/api/sessions/count", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}

    resp = client.get("/api/sessions/count", params={"filter": "L-Extreme"}, headers=AUTH)
    assert resp.json() == {"count": 1}


# ---------------------------------------------------------------------------
# calibration sets
# ---------------------------------------------------------------------------


def _cal_set(set_id, **extra):
    base = {
        "set_id": set_id,
        "frame_type": "Dark",
        "camera": "ZWOASI585MCPro",
        "ota": None,
        "filter": None,
        "gain": 200,
        "exposure_sec": 180.0,
        "temperature_c": -20.0,
        "frame_count": 30,
        "capture_date": "2026-02-19",
        "folder_path": "00_Calibration/Darks/ZWOASI585MCPro",
        "is_master": None,
    }
    base.update(extra)
    return base


def test_post_then_get_calibration_sets(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/calibration-sets", json=_cal_set("dark1"), headers=AUTH)
    assert resp.status_code == 204

    resp = client.get("/api/calibration-sets", headers=AUTH)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["set_id"] == "dark1"


def test_get_calibration_sets_frame_type_and_camera_filters(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/calibration-sets", json=_cal_set("dark1"), headers=AUTH)
    client.post(
        "/api/calibration-sets",
        json=_cal_set("flat1", frame_type="Flat", filter="L-Pro"),
        headers=AUTH,
    )

    resp = client.get(
        "/api/calibration-sets", params={"frame_type": "Flat"}, headers=AUTH
    )
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["set_id"] == "flat1"

    resp = client.get(
        "/api/calibration-sets", params={"camera": "ZWOASI585MCPro"}, headers=AUTH
    )
    assert len(resp.json()) == 2

    resp = client.get(
        "/api/calibration-sets", params={"camera": "NoSuchCamera"}, headers=AUTH
    )
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PATCH /api/sessions/{session_id}
# ---------------------------------------------------------------------------


def test_patch_session_edit_notes(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    client.post("/api/sessions", json=_session(sid), headers=AUTH)

    resp = client.patch(
        f"/api/sessions/{sid}", json={"notes": "updated note"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": True}

    resp = client.get("/api/sessions", params={"session_id": sid}, headers=AUTH)
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["notes"] == "updated note"


def test_patch_session_unknown_field_400(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    client.post("/api/sessions", json=_session(sid), headers=AUTH)

    resp = client.patch(
        f"/api/sessions/{sid}", json={"bogus_field": 1}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_patch_session_nonexistent_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.patch(
        "/api/sessions/does_not_exist", json={"notes": "x"}, headers=AUTH
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "session not found"}


# ---------------------------------------------------------------------------
# POST /api/sessions/{session_id}/state
# ---------------------------------------------------------------------------


def test_post_state_valid_processed(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    client.post("/api/sessions", json=_session(sid), headers=AUTH)

    resp = client.post(
        f"/api/sessions/{sid}/state",
        json={"state": "processed", "processed_date": "2026-03-01"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": True}

    resp = client.get("/api/sessions", params={"session_id": sid}, headers=AUTH)
    row = resp.json()[0]
    assert row["processed_state"] == "processed"
    assert row["processed_date"] == "2026-03-01"


def test_post_state_invalid_state_400(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    client.post("/api/sessions", json=_session(sid), headers=AUTH)

    resp = client.post(
        f"/api/sessions/{sid}/state", json={"state": "not_a_real_state"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_post_state_missing_session_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/sessions/does_not_exist/state", json={"state": "processed"}, headers=AUTH
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "session not found"}


# ---------------------------------------------------------------------------
# upsert idempotency
# ---------------------------------------------------------------------------


def test_post_session_twice_is_idempotent_and_updates(tmp_path):
    client = make_client(tmp_path)
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    client.post("/api/sessions", json=_session(sid, frame_count=100), headers=AUTH)
    client.post("/api/sessions", json=_session(sid, frame_count=200), headers=AUTH)

    resp = client.get("/api/sessions", headers=AUTH)
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["frame_count"] == 200


# ---------------------------------------------------------------------------
# create_app_from_env
# ---------------------------------------------------------------------------


def test_create_app_from_env_missing_token_raises(monkeypatch):
    monkeypatch.delenv("DARKROOM_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        create_app_from_env()


def test_create_app_from_env_with_token_returns_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKROOM_API_TOKEN", "envtoken")
    monkeypatch.setenv("DARKROOM_CATALOG", str(tmp_path / "env_catalog.db"))
    app = create_app_from_env()
    assert app is not None

    client = TestClient(app)
    resp = client.get("/api/sessions", headers={"Authorization": "Bearer envtoken"})
    assert resp.status_code == 200
