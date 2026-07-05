"""End-to-end parity: HttpBackend against the real webapi app vs LocalBackend.

Every test runs the same operations through a LocalBackend on its own SQLite
file and through an HttpBackend whose requests go through the full FastAPI
stack (TestClient is an httpx.Client subclass, injected via HttpBackend's
`client` kwarg), each backed by a separate DB — then asserts identical
results. This is the proof that the W9 transport wrapper moves no logic.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from darkroom.catalog_client import HttpBackend, LocalBackend
from darkroom.webapi.app import create_app

from tests.test_catalog_client import _cal_set, _session

_VOLATILE = ("created_at", "updated_at")


def _strip(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in _VOLATILE} for r in rows]


@pytest.fixture
def backends(tmp_path):
    """(LocalBackend, HttpBackend) pair on separate fresh DBs."""
    local = LocalBackend(tmp_path / "local.db")
    app = create_app(tmp_path / "server.db", "tok")
    tc = TestClient(app, headers={"Authorization": "Bearer tok"})
    http = HttpBackend("http://testserver", client=tc)
    yield local, http
    http.close()


def test_session_roundtrip_parity(backends):
    local, http = backends
    s1 = _session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro")
    s2 = _session(
        "M81_20260220_FRA400_ZWOASI585MCPro_L-Pro", obs_date="2026-02-20"
    )
    for b in (local, http):
        b.upsert_session(s1)
        b.upsert_session(s2)

    assert _strip(local.query_sessions()) == _strip(http.query_sessions())
    assert _strip(local.query_sessions(target="M81")) == _strip(
        http.query_sessions(target="M81")
    )
    assert local.query_sessions(target="NGC 7000") == []
    assert http.query_sessions(target="NGC 7000") == []
    assert local.count_sessions() == http.count_sessions() == 2
    assert (
        local.count_sessions(obs_date="2026-02-20")
        == http.count_sessions(obs_date="2026-02-20")
        == 1
    )


def test_calibration_set_parity(backends):
    local, http = backends
    dark = _cal_set("Dark_ZWOASI585MCPro_gain200_180s")
    flat = _cal_set(
        "Flat_FRA400_ZWOASI585MCPro_L-Pro_2026-02-20",
        frame_type="Flat",
        filter="L-Pro",
        exposure_sec=2.0,
        capture_date="2026-02-20",
        folder_path="00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20",
    )
    for b in (local, http):
        b.upsert_calibration_set(dark)
        b.upsert_calibration_set(flat)

    for kwargs in (
        {},
        {"frame_type": "Dark"},
        {"frame_type": "Flat", "camera": "ZWOASI585MCPro"},
        {"frame_type": "Flat", "filter": "L-Pro"},
        {"frame_type": "Bias"},
    ):
        assert _strip(local.query_calibration_sets(**kwargs)) == _strip(
            http.query_calibration_sets(**kwargs)
        ), kwargs


def test_set_processed_state_parity(backends):
    local, http = backends
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    for b in (local, http):
        b.upsert_session(_session(sid))
        assert (
            b.set_processed_state(
                sid, state="processed", processed_date="2026-05-15"
            )
            is True
        )
        assert b.set_processed_state("NoSuchSession_x", state="processed") is False
        with pytest.raises(ValueError):
            b.set_processed_state(sid, state="not-a-state")

    lrow = local.query_sessions(session_id=sid)[0]
    hrow = http.query_sessions(session_id=sid)[0]
    assert lrow["processed_state"] == hrow["processed_state"] == "processed"
    assert lrow["processed_date"] == hrow["processed_date"] == "2026-05-15"


def test_update_session_fields_parity(backends):
    local, http = backends
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    for b in (local, http):
        b.upsert_session(_session(sid))
        assert b.update_session_fields(sid, notes="two-panel mosaic") is True
        assert b.update_session_fields("NoSuchSession_x", notes="x") is False
        with pytest.raises(ValueError):
            b.update_session_fields(sid, frame_count=999)  # not editable

    assert (
        local.query_sessions(session_id=sid)[0]["notes"]
        == http.query_sessions(session_id=sid)[0]["notes"]
        == "two-panel mosaic"
    )


def test_upsert_idempotency_parity(backends):
    local, http = backends
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    for b in (local, http):
        b.upsert_session(_session(sid, frame_count=100))
        b.upsert_session(_session(sid, frame_count=132))

    assert local.count_sessions() == http.count_sessions() == 1
    assert (
        local.query_sessions()[0]["frame_count"]
        == http.query_sessions()[0]["frame_count"]
        == 132
    )


def test_limit_offset_parity(backends):
    local, http = backends
    for i in range(5):
        s = _session(
            f"M81_2026021{i}_FRA400_ZWOASI585MCPro_L-Pro",
            obs_date=f"2026-02-1{i}",
        )
        local.upsert_session(s)
        http.upsert_session(s)

    assert _strip(local.query_sessions(limit=2)) == _strip(
        http.query_sessions(limit=2)
    )
    assert _strip(local.query_sessions(limit=2, offset=3)) == _strip(
        http.query_sessions(limit=2, offset=3)
    )
    assert len(http.query_sessions(limit=2, offset=3)) == 2
