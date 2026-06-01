import json
import sqlite3
from pathlib import Path

import pytest

from darkroom.triage.db import (
    open_db,
    upsert_item,
    update_status,
    get_item,
    list_items,
    count_items,
    log_action,
    complete_action,
    list_audit,
    get_audit_entry,
    mark_reverted,
)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "triage.db")
    yield c
    c.close()


class TestOpenDb:
    def test_creates_tables(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "triage_items" in tables
        assert "audit_log" in tables

    def test_idempotent(self, tmp_path):
        db = tmp_path / "t.db"
        c1 = open_db(db)
        c1.close()
        c2 = open_db(db)  # must not raise
        c2.close()


class TestUpsertItem:
    def test_inserts_new_item(self, conn):
        item_id = upsert_item(
            conn,
            category="flat_restructure",
            source_path="/staging/00_Calibration/Flats/20240110_FMA180_Canon6D_L-Pro",
            proposed_path="/staging/00_Calibration/Flats/FMA180_Canon6D_L-Pro/2024-01-10",
        )
        assert item_id > 0

    def test_returns_id_on_conflict(self, conn):
        src = "/staging/foo"
        id1 = upsert_item(conn, category="flat_restructure", source_path=src)
        id2 = upsert_item(conn, category="flat_restructure", source_path=src)
        assert id1 == id2

    def test_does_not_overwrite_non_pending(self, conn):
        src = "/staging/bar"
        item_id = upsert_item(conn, category="flat_restructure", source_path=src,
                              proposed_path="/staging/old")
        update_status(conn, item_id, "approved")
        upsert_item(conn, category="flat_restructure", source_path=src,
                    proposed_path="/staging/new")
        item = get_item(conn, item_id)
        assert item["proposed_path"] == "/staging/old"  # preserved

    def test_stores_fits_metadata_as_json(self, conn):
        meta = {"OBJECT": "M 81", "RA": 148.888}
        item_id = upsert_item(conn, category="missing_object", source_path="/p",
                              fits_metadata=meta)
        item = get_item(conn, item_id)
        assert item["fits_metadata"]["OBJECT"] == "M 81"


class TestUpdateStatus:
    def test_sets_status(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/s1")
        update_status(conn, item_id, "approved")
        assert get_item(conn, item_id)["status"] == "approved"

    def test_sets_notes_and_proposed_path(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/s2",
                              proposed_path="/orig")
        update_status(conn, item_id, "modified",
                      user_notes="changed dest",
                      proposed_path="/new")
        item = get_item(conn, item_id)
        assert item["user_notes"] == "changed dest"
        assert item["proposed_path"] == "/new"


class TestListAndCount:
    def test_list_filters_by_category(self, conn):
        upsert_item(conn, category="flat_restructure", source_path="/a")
        upsert_item(conn, category="legacy_session", source_path="/b")
        items = list_items(conn, category="flat_restructure")
        assert len(items) == 1
        assert items[0]["source_path"] == "/a"

    def test_count_by_status(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/c")
        update_status(conn, item_id, "approved")
        assert count_items(conn, status="approved") == 1
        assert count_items(conn, status="pending") == 0


class TestAuditLog:
    def test_log_and_complete(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/d")
        log_id = log_action(conn, triage_item_id=item_id,
                            action_type="rename",
                            source_path="/d", dest_path="/e",
                            source_sha256="abc123")
        assert log_id > 0
        entry = get_audit_entry(conn, log_id)
        assert entry["result"] is None  # pre-write sentinel

        complete_action(conn, log_id, "success")
        entry = get_audit_entry(conn, log_id)
        assert entry["result"] == "success"

    def test_mark_reverted(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/f")
        log_id = log_action(conn, triage_item_id=item_id,
                            action_type="move", source_path="/f", dest_path="/g")
        complete_action(conn, log_id, "success")
        mark_reverted(conn, log_id)
        entry = get_audit_entry(conn, log_id)
        assert entry["result"] == "reverted"
        assert entry["reverted_at"] is not None

    def test_list_audit_order(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/h")
        log_action(conn, triage_item_id=item_id, action_type="move",
                   source_path="/h", dest_path="/i")
        log_action(conn, triage_item_id=item_id, action_type="mkdir",
                   source_path="/h", dest_path="/j")
        entries = list_audit(conn)
        assert len(entries) == 2
        # newest first
        assert entries[0]["dest_path"] == "/j"
