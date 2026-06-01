from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS triage_items (
    id               INTEGER PRIMARY KEY,
    category         TEXT NOT NULL,
    source_path      TEXT UNIQUE NOT NULL,
    proposed_path    TEXT,
    proposed_value   TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    user_notes       TEXT,
    fits_metadata    TEXT,
    simbad_cache     TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    triage_item_id  INTEGER REFERENCES triage_items(id),
    action_type     TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    dest_path       TEXT NOT NULL,
    result          TEXT,
    error_msg       TEXT,
    source_sha256   TEXT,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    reverted_at     TEXT
);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def upsert_item(
    conn: sqlite3.Connection,
    *,
    category: str,
    source_path: str,
    proposed_path: str | None = None,
    proposed_value: str | None = None,
    fits_metadata: dict | None = None,
    simbad_cache: dict | None = None,
) -> int:
    meta_json = json.dumps(fits_metadata) if fits_metadata else None
    simbad_json = json.dumps(simbad_cache) if simbad_cache else None
    cur = conn.execute(
        """
        INSERT INTO triage_items
            (category, source_path, proposed_path, proposed_value, fits_metadata, simbad_cache)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            category      = excluded.category,
            proposed_path = excluded.proposed_path,
            proposed_value= excluded.proposed_value,
            fits_metadata = excluded.fits_metadata,
            simbad_cache  = excluded.simbad_cache,
            updated_at    = datetime('now')
        WHERE status = 'pending'
        """,
        (category, source_path, proposed_path, proposed_value, meta_json, simbad_json),
    )
    conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute(
        "SELECT id FROM triage_items WHERE source_path = ?", (source_path,)
    ).fetchone()
    return row["id"]


def update_status(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    *,
    user_notes: str | None = None,
    proposed_path: str | None = None,
    proposed_value: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE triage_items
        SET status        = ?,
            user_notes    = COALESCE(?, user_notes),
            proposed_path = COALESCE(?, proposed_path),
            proposed_value= COALESCE(?, proposed_value),
            updated_at    = datetime('now')
        WHERE id = ?
        """,
        (status, user_notes, proposed_path, proposed_value, item_id),
    )
    conn.commit()


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM triage_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("fits_metadata", "simbad_cache"):
        if d[key]:
            d[key] = json.loads(d[key])
    return d


def list_items(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM triage_items {where} ORDER BY category, id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        for key in ("fits_metadata", "simbad_cache"):
            if d[key]:
                d[key] = json.loads(d[key])
        result.append(d)
    return result


def count_items(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    status: str | None = None,
) -> int:
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT COUNT(*) FROM triage_items {where}", params
    ).fetchone()[0]


def log_action(
    conn: sqlite3.Connection,
    *,
    triage_item_id: int,
    action_type: str,
    source_path: str,
    dest_path: str,
    source_sha256: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO audit_log
            (triage_item_id, action_type, source_path, dest_path, source_sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        (triage_item_id, action_type, source_path, dest_path, source_sha256),
    )
    conn.commit()
    return cur.lastrowid


def complete_action(
    conn: sqlite3.Connection, log_id: int, result: str, error_msg: str | None = None
) -> None:
    conn.execute(
        "UPDATE audit_log SET result = ?, error_msg = ? WHERE id = ?",
        (result, error_msg, log_id),
    )
    conn.commit()


def list_audit(
    conn: sqlite3.Connection, limit: int = 100, offset: int = 0
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY applied_at DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def get_audit_entry(conn: sqlite3.Connection, log_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM audit_log WHERE id = ?", (log_id,)
    ).fetchone()
    return dict(row) if row else None


def mark_reverted(conn: sqlite3.Connection, log_id: int) -> None:
    conn.execute(
        """
        UPDATE audit_log
        SET result = 'reverted', reverted_at = datetime('now')
        WHERE id = ?
        """,
        (log_id,),
    )
    conn.commit()
