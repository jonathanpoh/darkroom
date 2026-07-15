"""darkroom.catalog_db — read/write catalog API for the future web UI (W4).

Deliberately astropy-free at import time: this module only imports stdlib +
darkroom.names, so a web process can `import darkroom.catalog_db` without
paying astropy's import cost (mirrors the constraint documented in
darkroom/catalog.py and darkroom/names.py, W5). The schema is owned by
darkroom.cataloger.init_db — that import happens lazily, inside open_db,
so it's never paid at module load either.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from darkroom.names import _normalize_target, make_session_id, session_dest_rel

# Columns a UI is allowed to edit via update_session_fields. Deliberately
# excludes id, session_id (derived, not directly settable), frame_count,
# total_integration_sec/hours, processed_status (legacy), created_at/updated_at
# (managed by this module), and lights_path — excluded from direct editing but
# recomputed server-side (via session_dest_rel) when an identity field changes.
_EDITABLE_FIELDS = frozenset({
    "target", "obs_date", "ota", "camera", "filter",
    "gain", "temperature_c", "exposure_sec", "focal_length",
    "ra_deg", "dec_deg", "notes",
    "processed_state", "processed_path", "processed_date",
})

# Identity components: changing any of these changes the derived session_id.
_IDENTITY_FIELDS = ("target", "obs_date", "ota", "camera", "filter")


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (creating/migrating if needed) the catalog DB with Row access + WAL.

    If db_path doesn't exist yet, lazily imports darkroom.cataloger to run
    init_db first so the schema is guaranteed present — this is the only
    place cataloger (and therefore astropy) gets imported, and only when
    the DB is actually missing.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        from darkroom.cataloger import init_db

        init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _build_where(
    *,
    target: str | None = None,
    obs_date: str | None = None,
    session_id: str | None = None,
    camera: str | None = None,
    ota: str | None = None,
    filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    processed_state: str | None = None,
) -> tuple[str, list]:
    """Build a parameterized WHERE clause shared by query_sessions/count_sessions."""
    clauses: list[str] = []
    params: list = []
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if target is not None:
        # Forgiving match: canonicalise spacing (e.g. 'M81' -> 'M 81') and
        # compare case-insensitively, matching darkroom.catalog.query_sessions.
        clauses.append("target = ? COLLATE NOCASE")
        params.append(_normalize_target(target))
    if obs_date is not None:
        clauses.append("obs_date = ?")
        params.append(obs_date)
    if camera is not None:
        clauses.append("camera = ?")
        params.append(camera)
    if ota is not None:
        clauses.append("ota = ?")
        params.append(ota)
    if filter is not None:
        clauses.append("filter = ?")
        params.append(filter)
    if date_from is not None:
        clauses.append("obs_date >= ?")
        params.append(date_from)
    if date_to is not None:
        clauses.append("obs_date <= ?")
        params.append(date_to)
    if processed_state is not None:
        clauses.append("processed_state = ?")
        params.append(processed_state)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_sessions(
    conn: sqlite3.Connection,
    *,
    target: str | None = None,
    obs_date: str | None = None,
    session_id: str | None = None,
    camera: str | None = None,
    ota: str | None = None,
    filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    processed_state: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Return sessions matching the given filters, ordered by obs_date then session_id.

    All filters are AND-ed together; omitted (None) filters are not applied.
    `target` is normalised and matched case-insensitively. `date_from`/`date_to`
    are inclusive bounds on obs_date. Pagination (`LIMIT`/`OFFSET`) is only
    applied when `limit` is not None.
    """
    where, params = _build_where(
        target=target, obs_date=obs_date, session_id=session_id,
        camera=camera, ota=ota, filter=filter,
        date_from=date_from, date_to=date_to, processed_state=processed_state,
    )
    sql = f"SELECT * FROM sessions {where} ORDER BY obs_date, session_id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_sessions(
    conn: sqlite3.Connection,
    *,
    target: str | None = None,
    obs_date: str | None = None,
    session_id: str | None = None,
    camera: str | None = None,
    ota: str | None = None,
    filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    processed_state: str | None = None,
) -> int:
    """Return the count of sessions matching the given filters (same as query_sessions)."""
    where, params = _build_where(
        target=target, obs_date=obs_date, session_id=session_id,
        camera=camera, ota=ota, filter=filter,
        date_from=date_from, date_to=date_to, processed_state=processed_state,
    )
    row = conn.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()
    return row[0]


def query_calibration_sets(
    conn: sqlite3.Connection,
    *,
    frame_type: str | None = None,
    camera: str | None = None,
    ota: str | None = None,
    filter: str | None = None,
    gain: int | None = None,
    exposure_sec: float | None = None,
) -> list[dict]:
    """Return calibration sets matching the given equality filters.

    Omitted (None) filters are not applied — passing filter=None means "no
    filter constraint", not "filter IS NULL" (date-proximity and null-filter
    matching stay in darkroom.catalog's find_* helpers, which consume rows
    like these client-side). Ordered masters-first, then capture_date.
    """
    clauses: list[str] = []
    params: list = []
    for col, val in (
        ("frame_type", frame_type), ("camera", camera), ("ota", ota),
        ("filter", filter), ("gain", gain), ("exposure_sec", exposure_sec),
    ):
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM calibration_sets {where} "
        "ORDER BY is_master DESC, capture_date, set_id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _record_pending_rename(
    conn: sqlite3.Connection,
    session_row_id: int,
    session_id: str,
    old_path: str,
    new_path: str,
) -> None:
    """Insert/update/delete the pending_renames ledger row for one session (U2).

    - No existing row: INSERT one (created_at == updated_at).
    - Existing row, new_path == that row's old_path: the identity edit landed
      back on what's still on disk, so the rename is moot — DELETE the row.
    - Existing row, otherwise: UPDATE session_id/new_path/updated_at in place.
      old_path is deliberately left untouched — it's still what's on disk, and
      coalescing repeated edits must chain to a single move from the
      original on-disk path to the latest desired one.

    Participates in the caller's transaction — no commit here.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        "SELECT id, old_path FROM pending_renames WHERE session_row_id = ?",
        (session_row_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO pending_renames "
            "(session_row_id, session_id, old_path, new_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_row_id, session_id, old_path, new_path, now, now),
        )
        return

    if new_path == existing["old_path"]:
        conn.execute("DELETE FROM pending_renames WHERE id = ?", (existing["id"],))
        return

    conn.execute(
        "UPDATE pending_renames SET session_id = ?, new_path = ?, updated_at = ? "
        "WHERE id = ?",
        (session_id, new_path, now, existing["id"]),
    )


def list_pending_renames(conn: sqlite3.Connection) -> list[dict]:
    """Return all pending_renames rows, ordered by id."""
    rows = conn.execute("SELECT * FROM pending_renames ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def ack_pending_rename(conn: sqlite3.Connection, rename_id: int) -> bool:
    """Delete a pending_renames row by id. Returns True if a row was deleted."""
    cur = conn.execute("DELETE FROM pending_renames WHERE id = ?", (rename_id,))
    conn.commit()
    return cur.rowcount > 0


def update_session_fields(conn: sqlite3.Connection, session_id: str, **fields) -> bool:
    """Update whitelisted fields on an existing session, in place.

    Rejects unknown field names with ValueError. Validates processed_state
    against darkroom.cataloger.PROCESSED_STATES (imported lazily) if present.

    Anti-orphan guarantee (W3): if any identity component (target, obs_date,
    ota, camera, filter) changes, session_id is recomputed from the merged
    old+new identity values and updated on the SAME row (single UPDATE by
    numeric id) — so processed_state/processed_path/processed_date/notes/
    created_at are carried forward rather than orphaned onto a stale row.
    On the same identity change, lights_path is recomputed from
    session_dest_rel (a NULL lights_path stays NULL).

    Returns True if the session was found and updated, False if session_id
    doesn't match any row.

    Raises:
        ValueError: unknown field key, invalid processed_state, or a
            recomputed session_id that collides with a different row.
    """
    unknown = set(fields) - _EDITABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown/non-editable field(s): {sorted(unknown)} "
            f"(editable: {sorted(_EDITABLE_FIELDS)})"
        )

    if "processed_state" in fields:
        from darkroom.cataloger import PROCESSED_STATES

        if fields["processed_state"] not in PROCESSED_STATES:
            raise ValueError(
                f"Invalid processed state: {fields['processed_state']!r} "
                f"(must be one of {sorted(PROCESSED_STATES)})"
            )

    row = conn.execute(
        "SELECT id, target, obs_date, ota, camera, filter, lights_path "
        "FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return False

    row_id = row["id"]
    set_clauses = []
    params: list = []
    for key, value in fields.items():
        set_clauses.append(f"{key} = ?")
        params.append(value)

    identity_changed = any(f in fields for f in _IDENTITY_FIELDS)
    if identity_changed:
        merged = {f: (fields[f] if f in fields else row[f]) for f in _IDENTITY_FIELDS}
        new_session_id = make_session_id(
            merged["target"], merged["obs_date"], merged["ota"],
            merged["camera"], merged["filter"],
        )
        if new_session_id != session_id:
            collision = conn.execute(
                "SELECT id FROM sessions WHERE session_id = ? AND id != ?",
                (new_session_id, row_id),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    f"Cannot rename session_id to {new_session_id!r}: "
                    f"already used by a different session (id={collision['id']})"
                )
            set_clauses.append("session_id = ?")
            params.append(new_session_id)

        # lights_path is derived from the same identity components, so it can
        # change even when session_id doesn't (e.g. a spacing-only target edit
        # like 'M81' -> 'M 81' keeps the slug but renames the folder). A NULL
        # lights_path stays NULL — never invent a path for a row without one.
        if row["lights_path"] is not None:
            new_lights_path = str(session_dest_rel(
                merged["target"], merged["obs_date"], merged["ota"],
                merged["camera"], merged["filter"],
            ))
            if new_lights_path != row["lights_path"]:
                set_clauses.append("lights_path = ?")
                params.append(new_lights_path)
                # U2: the webapi host has no NAS mount, so it can't rename the
                # folder itself — record the move owed on the Mac side.
                # new_session_id is always defined here (identity_changed is
                # True in this branch) and already equals session_id when
                # the identity edit didn't change the derived slug.
                _record_pending_rename(
                    conn, row_id, new_session_id, row["lights_path"], new_lights_path,
                )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    set_clauses.append("updated_at = ?")
    params.append(now)

    params.append(row_id)
    conn.execute(
        f"UPDATE sessions SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )
    conn.commit()
    return True


def delete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """Delete a session row by session_id, along with any pending_renames row
    owed for it (U2 — a deleted session can't have a rename left dangling).
    Returns True if the session row was deleted, False if session_id matched
    nothing. Catalog rows only — never touches archive files."""
    row = conn.execute(
        "SELECT id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM pending_renames WHERE session_row_id = ?", (row["id"],))
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
    conn.commit()
    return cur.rowcount > 0
