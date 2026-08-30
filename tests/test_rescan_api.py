"""Tests for F8 rescan-proposal storage, the JSON API, and CatalogBackend wiring.

darkroom/rescan.py and darkroom/catalog_cli.py (the scan engine + CLI half of
F8) belong to a different agent/worktree and are not exercised here — this
file only covers f8-web's half: schema, catalog_db CRUD, webapi/app.py's
/api/rescan-proposals routes, and catalog_client's Local/HttpBackend.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from darkroom import catalog_db
from darkroom.cataloger import init_db, upsert_session
from darkroom.catalog_client import LocalBackend
from darkroom.webapi.app import create_app
from darkroom.webapi.auth import hash_password

TOKEN = "testtoken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
UI_PASSWORD = "test-password"
UI_HASH = hash_password(UI_PASSWORD)  # scrypt is slow — hash once at module level


def make_client(tmp_path) -> TestClient:
    app = create_app(tmp_path / "catalog.db", TOKEN, UI_HASH)
    return TestClient(app)


def _session(session_id, **extra):
    base = {
        "session_id": session_id,
        "target": "M 81",
        "obs_date": "2026-02-19",
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": 110,
        "total_integration_sec": 19800,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro",
        "notes": "",
    }
    base.update(extra)
    return base


def _proposal(session_id="M 81_2026-02-19_FRA400_ZWOASI585MCPro_L-Pro", **extra):
    base = {
        "session_id": session_id,
        "kind": "update",
        "tier": "safe",
        "target": "M 81",
        "obs_date": "2026-02-19",
        "lights_path": "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro",
        "changes": {
            "frame_count": {"current": 110, "proposed": 74},
            "total_integration_sec": {"current": 19800, "proposed": 13320},
        },
        "detected_at": "2026-08-30T12:00:00Z",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# schema — additive migration is a no-op on an already-live DB
# ---------------------------------------------------------------------------


def test_init_db_creates_rescan_proposals_table_and_index(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
    assert "rescan_proposals" in tables
    assert "idx_rescan_proposals_status" in indexes


def test_init_db_rerun_on_live_db_is_a_noop(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    backend = LocalBackend(db)
    backend.upsert_session(_session("S1"))
    backend.replace_rescan_proposals([_proposal()])
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM rescan_proposals").fetchall()

    # Re-running init_db (as every LocalBackend write path does via
    # _ensure_schema, and as the webapi does at app construction) must not
    # touch existing rescan_proposals rows or raise on the already-present
    # table/index.
    init_db(db)
    init_db(db)

    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT * FROM rescan_proposals").fetchall()
    assert before == after


# ---------------------------------------------------------------------------
# catalog_db CRUD
# ---------------------------------------------------------------------------


def test_replace_rescan_proposals_inserts_pending_rows(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        n = catalog_db.replace_rescan_proposals(conn, [_proposal()])
        rows = catalog_db.list_rescan_proposals(conn)
    assert n == 1
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["changes"] == _proposal()["changes"]


def test_replace_rescan_proposals_invalid_kind_raises_and_writes_nothing(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        catalog_db.replace_rescan_proposals(conn, [_proposal()])
        with pytest.raises(ValueError):
            catalog_db.replace_rescan_proposals(conn, [_proposal(kind="bogus")])
        # The failed batch must not have wiped the previously-pending set.
        rows = catalog_db.list_rescan_proposals(conn)
    assert len(rows) == 1


def test_replace_rescan_proposals_invalid_tier_raises(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError):
            catalog_db.replace_rescan_proposals(conn, [_proposal(tier="bogus")])


def test_replace_rescan_proposals_leaves_applied_and_dismissed_alone(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        catalog_db.replace_rescan_proposals(
            conn, [_proposal(session_id="A"), _proposal(session_id="B")]
        )
        rows = {r["session_id"]: r["id"] for r in catalog_db.list_rescan_proposals(conn)}
        catalog_db.resolve_rescan_proposal(conn, rows["A"], "applied")
        catalog_db.resolve_rescan_proposal(conn, rows["B"], "dismissed")

        # A rescan supersedes the pending set — but A/B are no longer pending.
        catalog_db.replace_rescan_proposals(conn, [_proposal(session_id="C")])

        all_rows = catalog_db.list_rescan_proposals(conn, status=None)
    by_session = {r["session_id"]: r["status"] for r in all_rows}
    assert by_session == {"A": "applied", "B": "dismissed", "C": "pending"}


def test_list_rescan_proposals_status_filter_and_none(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        catalog_db.replace_rescan_proposals(conn, [_proposal(session_id="A")])
        pending_id = catalog_db.list_rescan_proposals(conn)[0]["id"]
        catalog_db.resolve_rescan_proposal(conn, pending_id, "dismissed")

        assert catalog_db.list_rescan_proposals(conn, status="pending") == []
        dismissed = catalog_db.list_rescan_proposals(conn, status="dismissed")
        assert len(dismissed) == 1
        assert len(catalog_db.list_rescan_proposals(conn, status=None)) == 1


def test_list_rescan_proposals_newest_first(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        catalog_db.replace_rescan_proposals(conn, [_proposal(session_id="A")])
        # A second replace call supersedes A's pending row with B's.
        catalog_db.replace_rescan_proposals(conn, [_proposal(session_id="B")])
        rows = catalog_db.list_rescan_proposals(conn)
    assert [r["session_id"] for r in rows] == ["B"]


def test_resolve_rescan_proposal_already_resolved_returns_false(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        catalog_db.replace_rescan_proposals(conn, [_proposal()])
        pid = catalog_db.list_rescan_proposals(conn)[0]["id"]
        assert catalog_db.resolve_rescan_proposal(conn, pid, "applied") is True
        assert catalog_db.resolve_rescan_proposal(conn, pid, "dismissed") is False


def test_get_rescan_proposal_unknown_id_returns_none(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert catalog_db.get_rescan_proposal(conn, 9999) is None


# ---------------------------------------------------------------------------
# apply_rescan_proposal — the three kinds
# ---------------------------------------------------------------------------


def test_apply_rescan_proposal_update_writes_proposed_fields(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    upsert_session(db, _session("S1"))
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        proposal = _proposal(session_id="S1", kind="update", tier="safe")
        catalog_db.apply_rescan_proposal(conn, db, proposal)
        row = conn.execute(
            "SELECT frame_count, total_integration_sec FROM sessions WHERE session_id = ?",
            ("S1",),
        ).fetchone()
    assert row["frame_count"] == 74
    assert row["total_integration_sec"] == 13320


def test_apply_rescan_proposal_delete_removes_session(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    upsert_session(db, _session("S1"))
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        proposal = {
            "session_id": "S1", "kind": "delete", "tier": "review",
            "changes": {
                "target": {"current": "M 81", "proposed": None},
            },
        }
        catalog_db.apply_rescan_proposal(conn, db, proposal)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", ("S1",)
        ).fetchone()
    assert row is None


def test_apply_rescan_proposal_create_upserts_new_session(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        proposal = {
            "session_id": "S-new", "kind": "create", "tier": "review",
            "changes": {
                "target": {"current": None, "proposed": "M 82"},
                "obs_date": {"current": None, "proposed": "2026-08-01"},
                "frame_count": {"current": None, "proposed": 50},
            },
        }
        catalog_db.apply_rescan_proposal(conn, db, proposal)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", ("S-new",)
        ).fetchone()
    assert row is not None
    assert row["target"] == "M 82"
    assert row["obs_date"] == "2026-08-01"
    assert row["frame_count"] == 50


def test_apply_rescan_proposal_unknown_kind_raises(tmp_path):
    db = tmp_path / "catalog.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError):
            catalog_db.apply_rescan_proposal(
                conn, db, {"session_id": "S1", "kind": "bogus", "changes": {}}
            )


# ---------------------------------------------------------------------------
# webapi routes
# ---------------------------------------------------------------------------


def test_post_rescan_proposals_no_auth_401(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/rescan-proposals", json=[_proposal()])
    assert resp.status_code == 401


def test_post_then_get_rescan_proposals_roundtrip(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/rescan-proposals", json=[_proposal()], headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 1}

    resp = client.get("/api/rescan-proposals", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "pending"
    assert body[0]["tier"] == "safe"


def test_post_rescan_proposals_invalid_kind_400(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/rescan-proposals", json=[_proposal(kind="bogus")], headers=AUTH
    )
    assert resp.status_code == 400


def test_get_rescan_proposals_status_filter(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/rescan-proposals", json=[_proposal()], headers=AUTH)
    proposal_id = client.get("/api/rescan-proposals", headers=AUTH).json()[0]["id"]
    client.post(f"/api/rescan-proposals/{proposal_id}/dismiss", headers=AUTH)

    assert client.get(
        "/api/rescan-proposals", params={"status": "pending"}, headers=AUTH
    ).json() == []
    dismissed = client.get(
        "/api/rescan-proposals", params={"status": "dismissed"}, headers=AUTH
    ).json()
    assert len(dismissed) == 1
    # Bare GET with no status filter returns every row, regardless of status.
    assert len(client.get("/api/rescan-proposals", headers=AUTH).json()) == 1


def test_post_rescan_apply_update_writes_session_and_marks_applied(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/sessions", json=_session("S1"), headers=AUTH)
    assert resp.status_code == 204
    client.post(
        "/api/rescan-proposals",
        json=[_proposal(session_id="S1", kind="update", tier="safe")],
        headers=AUTH,
    )
    proposal_id = client.get("/api/rescan-proposals", headers=AUTH).json()[0]["id"]

    resp = client.post(f"/api/rescan-proposals/{proposal_id}/apply", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"applied": True}

    sessions = client.get("/api/sessions", params={"session_id": "S1"}, headers=AUTH).json()
    assert sessions[0]["frame_count"] == 74
    assert sessions[0]["total_integration_sec"] == 13320

    resolved = client.get(
        "/api/rescan-proposals", params={"status": "applied"}, headers=AUTH
    ).json()
    assert len(resolved) == 1
    assert resolved[0]["resolved_at"] is not None


def test_post_rescan_apply_delete_removes_session(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/sessions", json=_session("S1"), headers=AUTH)
    client.post(
        "/api/rescan-proposals",
        json=[{
            "session_id": "S1", "kind": "delete", "tier": "review",
            "changes": {"target": {"current": "M 81", "proposed": None}},
        }],
        headers=AUTH,
    )
    proposal_id = client.get("/api/rescan-proposals", headers=AUTH).json()[0]["id"]

    resp = client.post(f"/api/rescan-proposals/{proposal_id}/apply", headers=AUTH)
    assert resp.status_code == 200

    sessions = client.get("/api/sessions", params={"session_id": "S1"}, headers=AUTH).json()
    assert sessions == []


def test_post_rescan_apply_create_upserts_new_session(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/rescan-proposals",
        json=[{
            "session_id": "S-new", "kind": "create", "tier": "review",
            "changes": {
                "target": {"current": None, "proposed": "M 82"},
                "obs_date": {"current": None, "proposed": "2026-08-01"},
                "frame_count": {"current": None, "proposed": 50},
            },
        }],
        headers=AUTH,
    )
    proposal_id = client.get("/api/rescan-proposals", headers=AUTH).json()[0]["id"]

    resp = client.post(f"/api/rescan-proposals/{proposal_id}/apply", headers=AUTH)
    assert resp.status_code == 200

    sessions = client.get(
        "/api/sessions", params={"session_id": "S-new"}, headers=AUTH
    ).json()
    assert len(sessions) == 1
    assert sessions[0]["target"] == "M 82"


def test_post_rescan_apply_nonexistent_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/rescan-proposals/999/apply", headers=AUTH)
    assert resp.status_code == 404


def test_post_rescan_apply_already_resolved_404(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/sessions", json=_session("S1"), headers=AUTH)
    client.post(
        "/api/rescan-proposals",
        json=[_proposal(session_id="S1", kind="update", tier="safe")],
        headers=AUTH,
    )
    proposal_id = client.get("/api/rescan-proposals", headers=AUTH).json()[0]["id"]
    client.post(f"/api/rescan-proposals/{proposal_id}/apply", headers=AUTH)

    resp = client.post(f"/api/rescan-proposals/{proposal_id}/apply", headers=AUTH)
    assert resp.status_code == 404


def test_post_rescan_dismiss_marks_row_and_leaves_session_untouched(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/sessions", json=_session("S1"), headers=AUTH)
    client.post(
        "/api/rescan-proposals",
        json=[_proposal(session_id="S1", kind="update", tier="safe")],
        headers=AUTH,
    )
    proposal_id = client.get("/api/rescan-proposals", headers=AUTH).json()[0]["id"]

    resp = client.post(f"/api/rescan-proposals/{proposal_id}/dismiss", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"dismissed": True}

    sessions = client.get("/api/sessions", params={"session_id": "S1"}, headers=AUTH).json()
    assert sessions[0]["frame_count"] == 110  # untouched — dismiss never writes

    dismissed = client.get(
        "/api/rescan-proposals", params={"status": "dismissed"}, headers=AUTH
    ).json()
    assert len(dismissed) == 1


def test_post_rescan_dismiss_nonexistent_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/rescan-proposals/999/dismiss", headers=AUTH)
    assert resp.status_code == 404


def test_post_rescan_dismiss_no_auth_401(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/rescan-proposals/1/dismiss")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# CatalogBackend wiring (LocalBackend + HttpBackend, per the F8 contract)
# ---------------------------------------------------------------------------


def test_local_backend_replace_and_list_rescan_proposals(tmp_path):
    backend = LocalBackend(tmp_path / "catalog.db")
    n = backend.replace_rescan_proposals([_proposal()])
    assert n == 1
    rows = backend.list_rescan_proposals()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_local_backend_apply_and_dismiss_rescan_proposal(tmp_path):
    backend = LocalBackend(tmp_path / "catalog.db")
    backend.upsert_session(_session("S1"))
    backend.replace_rescan_proposals(
        [_proposal(session_id="S1", kind="update", tier="safe")]
    )
    proposal_id = backend.list_rescan_proposals()[0]["id"]

    assert backend.apply_rescan_proposal(proposal_id) is True
    sessions = backend.query_sessions(session_id="S1")
    assert sessions[0]["frame_count"] == 74

    # Already applied — a second apply/dismiss call is a no-op returning False.
    assert backend.apply_rescan_proposal(proposal_id) is False
    assert backend.dismiss_rescan_proposal(proposal_id) is False


def test_local_backend_dismiss_rescan_proposal_unknown_id_false(tmp_path):
    backend = LocalBackend(tmp_path / "catalog.db")
    assert backend.dismiss_rescan_proposal(999) is False


def test_http_backend_rescan_proposal_roundtrip(tmp_path):
    app = create_app(tmp_path / "catalog.db", TOKEN, UI_HASH)
    client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})

    from darkroom.catalog_client import HttpBackend

    backend = HttpBackend("http://testserver", client=client)
    backend.upsert_session(_session("S1"))
    n = backend.replace_rescan_proposals(
        [_proposal(session_id="S1", kind="update", tier="safe")]
    )
    assert n == 1

    proposals = backend.list_rescan_proposals()
    assert len(proposals) == 1
    proposal_id = proposals[0]["id"]

    assert backend.apply_rescan_proposal(proposal_id) is True
    assert backend.query_sessions(session_id="S1")[0]["frame_count"] == 74
    assert backend.apply_rescan_proposal(999) is False
    assert backend.dismiss_rescan_proposal(999) is False
