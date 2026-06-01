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

    def test_accepting_suggestion_unchanged_is_approved(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="processed_dir",
                              source_path="/s/p", proposed_path="/s/p_Processed")
        # Submit the same pre-filled path back — accepting the suggestion as-is.
        c.post(f"/item/{item_id}/approve",
               data={"proposed_path": "/s/p_Processed"})
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "approved"

    def test_editing_suggestion_is_modified(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="processed_dir",
                              source_path="/s/p", proposed_path="/s/p_Processed")
        c.post(f"/item/{item_id}/approve",
               data={"proposed_path": "/s/p_DIFFERENT"})
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "modified"

    def test_approve_advances_within_same_category(self, client):
        c, conn = client
        # calibration_in_target sorts alphabetically before processed_dir;
        # approving a processed_dir item must NOT jump to the calibration queue.
        cal = upsert_item(conn, category="calibration_in_target",
                          source_path="/s/cal", proposed_path="/s/cal2")
        proc1 = upsert_item(conn, category="processed_dir",
                            source_path="/s/p1", proposed_path="/s/p1x")
        proc2 = upsert_item(conn, category="processed_dir",
                            source_path="/s/p2", proposed_path="/s/p2x")

        resp = c.post(f"/item/{proc1}/approve", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/item/{proc2}"

    def test_advance_to_queue_when_category_exhausted(self, client):
        c, conn = client
        upsert_item(conn, category="calibration_in_target",
                    source_path="/s/cal", proposed_path="/s/cal2")
        only = upsert_item(conn, category="processed_dir",
                           source_path="/s/p1", proposed_path="/s/p1x")

        resp = c.post(f"/item/{only}/approve", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/queue?category=processed_dir&status=pending"

    def test_skip_advances_within_same_category(self, client):
        c, conn = client
        upsert_item(conn, category="calibration_in_target",
                    source_path="/s/cal", proposed_path="/s/cal2")
        proc1 = upsert_item(conn, category="processed_dir",
                            source_path="/s/p1", proposed_path="/s/p1x")
        proc2 = upsert_item(conn, category="processed_dir",
                            source_path="/s/p2", proposed_path="/s/p2x")

        resp = c.post(f"/item/{proc1}/skip", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/item/{proc2}"


class TestDashboardCounts:
    def test_modified_counted_as_approved(self, client):
        c, conn = client
        # An item the user edited (status "modified") must still appear on the
        # dashboard — counted as ready-to-commit, same as the Commit page.
        item_id = upsert_item(conn, category="processed_dir",
                              source_path="/s/p", proposed_path="/s/p1")
        update_status(conn, item_id, "modified")
        resp = c.get("/")
        # The processed_dir row must render (0 pending but 1 ready) ...
        assert "processed_dir" in resp.text
        # ... and the commit page agrees there's 1 item ready.
        assert "/s/p" in c.get("/commit").text


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


class TestCommitExecute:
    def test_execute_is_get_for_eventsource(self, client):
        # The browser drives this via EventSource, which only issues GET.
        # A POST-only route would 405 and silently apply nothing.
        c, conn = client
        resp = c.get("/commit/execute")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_flat_restructure_applied(self, db_and_archive):
        conn, db_path, archive = db_and_archive
        app = create_app(db_path=db_path, archive_root=archive)
        c = TestClient(app)

        src = archive / "00_Calibration" / "Flats" / "20240110_FMA180_Canon6D_L-Pro"
        src.mkdir(parents=True)
        (src / "flat.fit").write_bytes(b"x")
        dst = archive / "00_Calibration" / "Flats" / "FMA180_Canon6D_L-Pro" / "2024-01-10"

        item_id = upsert_item(conn, category="flat_restructure",
                              source_path=str(src), proposed_path=str(dst))
        update_status(conn, item_id, "approved")

        resp = c.get("/commit/execute")
        assert "success" in resp.text
        assert dst.exists()
        assert not src.exists()
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "applied"

    def test_missing_value_blocks_correction(self, db_and_archive):
        conn, db_path, archive = db_and_archive
        app = create_app(db_path=db_path, archive_root=archive)
        c = TestClient(app)

        src = archive / "04_Deep Sky Objects" / "M 81" / "2024-01-01_FRA400_ZWOASI585MCPro"
        src.mkdir(parents=True)
        dst = archive / "_corrected" / "x"

        # missing_object with empty proposed_value must be blocked, not a no-op copy
        item_id = upsert_item(conn, category="missing_object",
                              source_path=str(src), proposed_path=str(dst))
        update_status(conn, item_id, "approved")

        resp = c.get("/commit/execute")
        assert "error" in resp.text
        assert not dst.exists()
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "error"

    def test_partial_suggestion_blocked(self, db_and_archive):
        conn, db_path, archive = db_and_archive
        app = create_app(db_path=db_path, archive_root=archive)
        c = TestClient(app)

        src = archive / "04 Deep Sky Objects" / "M 42" / "2023-11-23" / "Flats"
        src.mkdir(parents=True)
        # proposed_path still contains a placeholder — must not be committed
        dst = archive / "00_Calibration" / "Flats" / "FRA400_Cam_{FILTER?}" / "2024-01-10"

        item_id = upsert_item(conn, category="calibration_in_target",
                              source_path=str(src), proposed_path=str(dst))
        update_status(conn, item_id, "approved")

        resp = c.get("/commit/execute")
        assert "error" in resp.text
        assert not dst.exists()
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "error"

    def test_deepest_path_committed_first(self, db_and_archive):
        conn, db_path, archive = db_and_archive
        app = create_app(db_path=db_path, archive_root=archive)
        c = TestClient(app)

        # A legacy session and a calibration folder nested inside it.
        session = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23"
        flats = session / "Flats"
        (session / "Lights").mkdir(parents=True)
        flats.mkdir(parents=True)
        (flats / "flat.fit").write_bytes(b"x")

        calib_dst = archive / "00_Calibration" / "Flats" / "FMA180_Canon6D" / "2023-11-23"
        session_dst = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23_FMA180_Canon6D"

        cal_id = upsert_item(conn, category="calibration_in_target",
                             source_path=str(flats), proposed_path=str(calib_dst))
        update_status(conn, cal_id, "approved")
        ses_id = upsert_item(conn, category="legacy_session",
                             source_path=str(session), proposed_path=str(session_dst))
        update_status(conn, ses_id, "approved")

        resp = c.get("/commit/execute")
        # Both succeed because the nested Flats move runs before the parent rename
        assert resp.text.count("success") == 2
        assert calib_dst.exists()
        assert session_dst.exists()
