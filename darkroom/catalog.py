# darkroom/catalog.py
import sqlite3
from datetime import date, timedelta
from pathlib import Path


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def query_all_sessions(db: Path) -> list[dict]:
    """Return all sessions ordered by target then obs_date."""
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY target, obs_date"
        ).fetchall()
    return [dict(r) for r in rows]


def query_sessions(
    db: Path,
    *,
    target: str | None = None,
    obs_date: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """Return sessions matching the given filters, ordered by obs_date."""
    clauses, params = [], []
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if target is not None:
        clauses.append("target = ?")
        params.append(target)
    if obs_date is not None:
        clauses.append("obs_date = ?")
        params.append(obs_date)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect(db) as conn:
        rows = conn.execute(
            f"SELECT * FROM sessions {where} ORDER BY obs_date", params
        ).fetchall()
    return [dict(r) for r in rows]


def find_darks(
    db: Path, *, camera: str, gain: int, exposure_sec: float
) -> list[dict]:
    """Return Dark calibration sets matching camera+gain+exposure."""
    with _connect(db) as conn:
        rows = conn.execute(
            """SELECT * FROM calibration_sets
               WHERE frame_type = 'Dark' AND camera = ? AND gain = ? AND exposure_sec = ?""",
            (camera, gain, exposure_sec),
        ).fetchall()
    return [dict(r) for r in rows]


def find_flats(
    db: Path, *, camera: str, ota: str, filter_: str | None, obs_date: str
) -> list[dict]:
    """Return Flat calibration sets within ±1 day, ordered by date proximity."""
    d = date.fromisoformat(obs_date)
    lo = (d - timedelta(days=1)).isoformat()
    hi = (d + timedelta(days=1)).isoformat()
    if filter_ is None:
        filter_clause = "filter IS NULL"
        params = (camera, ota, lo, hi, obs_date)
    else:
        filter_clause = "filter = ?"
        params = (camera, ota, filter_, lo, hi, obs_date)
    with _connect(db) as conn:
        rows = conn.execute(
            f"""SELECT * FROM calibration_sets
                WHERE frame_type = 'Flat'
                  AND camera = ?
                  AND ota = ?
                  AND {filter_clause}
                  AND capture_date BETWEEN ? AND ?
                ORDER BY ABS(julianday(capture_date) - julianday(?))""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def find_flat_darks(
    db: Path, *, camera: str, flat_exposure_sec: float, flat_capture_date: str
) -> list[dict]:
    """Return FlatDark sets matching camera + exposure (±10%) + date (flat_date or flat_date+1)."""
    lo = flat_exposure_sec * 0.9
    hi = flat_exposure_sec * 1.1
    d = date.fromisoformat(flat_capture_date)
    d1 = (d + timedelta(days=1)).isoformat()
    with _connect(db) as conn:
        rows = conn.execute(
            """SELECT * FROM calibration_sets
               WHERE frame_type = 'FlatDark'
                 AND camera = ?
                 AND exposure_sec BETWEEN ? AND ?
                 AND capture_date IN (?, ?)""",
            (camera, lo, hi, flat_capture_date, d1),
        ).fetchall()
    return [dict(r) for r in rows]
