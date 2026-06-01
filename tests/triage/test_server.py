import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from darkroom.triage.db import open_db, upsert_item, update_status
from darkroom.triage.server import create_app


@pytest.fixture
def db_and_archive(tmp_path):
    archive = tmp_path / "staging"
    (archive / "04_Deep Sky Objects").mkdir(parents=True)
    (archive / "00_Calibration").mkdir(parents=True)
    trash_root = archive / ".triage_trash"
    corrected_root = archive / "_corrected"
    cache_dir = archive / ".triage_cache"
    db_path = tmp_path / "triage.db"
    conn = open_db(db_path)
    return conn, db_path, archive


@pytest.fixture
def client(db_and_archive):
    conn, db_path, archive = db_and_archive
    app = create_app(db_path=db_path, archive_root=archive)
    return TestClient(app), conn


class TestDashboard:
    def test_returns_200(self, client):
        c, conn = client
        resp = c.get("/")
        assert resp.status_code == 200
        assert "triage" in resp.text.lower()


class TestQueue:
    def test_empty_queue(self, client):
        c, conn = client
        resp = c.get("/queue")
        assert resp.status_code == 200

    def test_shows_items(self, client):
        c, conn = client
        upsert_item(conn, category="flat_restructure",
                    source_path="/s/foo", proposed_path="/s/bar")
        resp = c.get("/queue")
        assert "flat_restructure" in resp.text


class TestItemDetail:
    def test_item_returns_200(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/x", proposed_path="/s/y")
        resp = c.get(f"/item/{item_id}")
        assert resp.status_code == 200

    def test_missing_item_returns_404(self, client):
        c, conn = client
        resp = c.get("/item/9999")
        assert resp.status_code == 404


class TestItemActions:
    def test_approve_sets_status(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/a", proposed_path="/s/b")
        resp = c.post(f"/item/{item_id}/approve")
        assert resp.status_code in (200, 303)
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "approved"

    def test_skip_sets_status(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/c", proposed_path="/s/d")
        c.post(f"/item/{item_id}/skip")
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "skipped"


class TestCommitPage:
    def test_commit_page_loads(self, client):
        c, conn = client
        resp = c.get("/commit")
        assert resp.status_code == 200

    def test_shows_approved_items(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/p", proposed_path="/s/q")
        update_status(conn, item_id, "approved")
        resp = c.get("/commit")
        assert "/s/p" in resp.text


class TestAuditPage:
    def test_audit_returns_200(self, client):
        c, conn = client
        resp = c.get("/audit")
        assert resp.status_code == 200
