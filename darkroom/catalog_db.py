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

from darkroom.names import _normalize_target, make_session_id

# Columns a UI is allowed to edit via update_session_fields. Deliberately
# excludes id, session_id (derived, not directly settable), frame_count,
# total_integration_sec/hours, lights_path, processed_status (legacy),
# created_at/updated_at (managed by this module).
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


def update_session_fields(conn: sqlite3.Connection, session_id: str, **fields) -> bool:
    """Update whitelisted fields on an existing session, in place.

    Rejects unknown field names with ValueError. Validates processed_state
    against darkroom.cataloger.PROCESSED_STATES (imported lazily) if present.

    Anti-orphan guarantee (W3): if any identity component (target, obs_date,
    ota, camera, filter) changes, session_id is recomputed from the merged
    old+new identity values and updated on the SAME row (single UPDATE by
    numeric id) — so processed_state/processed_path/processed_date/notes/
    created_at are carried forward rather than orphaned onto a stale row.

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
        "SELECT id, target, obs_date, ota, camera, filter FROM sessions WHERE session_id = ?",
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
