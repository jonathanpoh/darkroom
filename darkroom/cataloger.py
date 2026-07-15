#!/usr/bin/env python3
"""
FITS Astrophotography Session Catalog Tool

Scans FITS files and catalogs sessions into SQLite (browsed via the
darkroom.webapi web UI).
Two commands for ingestion:
  scan-all         — recursively catalog all light sessions
  scan-calibration — catalog calibration frames (darks, flats, bias)
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astropy.io import fits
from astropy.time import Time

from darkroom.parse import normalize_filter, parse_filter, parse_ota
from darkroom.names import (
    _format_gain,
    _normalize_camera,
    _normalize_target,
    _parse_coords,
    _round_exposure,
    make_session_id,  # re-exported for back-compat (moved to names.py in W4)
)


# Imaging sessions are identified by the local civil date the night started.
# Change this if observations are made from a different timezone.
LOCAL_TZ = ZoneInfo("Europe/Lisbon")


def compute_imaging_night(date_obs_utc: str) -> str | None:
    """Return YYYY-MM-DD for the local imaging night a UTC timestamp belongs to.

    Frames between local noon on day N and local noon on day N+1 all belong
    to the "night of day N". A session running 23:00 → 04:00 local is one
    night. Local-time hours < 12 → subtract one day.
    """
    if not date_obs_utc:
        return None
    try:
        t = Time(date_obs_utc, format="isot", scale="utc")
        utc_dt = t.datetime.replace(tzinfo=ZoneInfo("UTC"))
        local_dt = utc_dt.astimezone(LOCAL_TZ)
        if local_dt.hour < 12:
            return (local_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        return local_dt.strftime("%Y-%m-%d")
    except Exception:
        return None


# ============================================================================
# Session ID construction
# ============================================================================


_CALIB_FOLDER_NAMES = frozenset({"flats", "darks", "bias", "flatdarks", "flat darks"})

# Folders to prune entirely during os.walk (never descend into or collect from)
_SKIP_DIR_NAMES_LOWER = frozenset({
    "_processed",
    "reject", "rejects", "rejected",
    "bad",
    "delete",
    "masterbias", "masterdark",
})


def _parse_gain(header) -> int:
    """Return numeric gain/ISO from FITS header, 0 if absent or non-numeric (e.g. 'Auto')."""
    for key in ("GAIN", "ISO"):
        val = header.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
    return 0


def _target_from_path(lights_path: Path) -> str:
    """Extract target name from NAS folder path.

    Looks for the component immediately after any '*Deep Sky Objects' folder
    (e.g. '01_Deep Sky Objects', '04_Deep Sky Objects'). Falls back based on
    directory depth:
      old layout: Target/Date_OTA_Camera_Filter/Lights     → parts[-3]
      new layout: Target/Date_OTA_Camera/Lights/Filter     → parts[-4]
    """
    parts = lights_path.parts
    for i, part in enumerate(parts):
        if "Deep Sky Objects" in part and i + 1 < len(parts):
            return parts[i + 1]
    # New layout has one extra level (filter subdir under Lights/)
    if lights_path.parent.name == "Lights" and len(parts) >= 4:
        return parts[-4]
    if len(parts) >= 3:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""


def _filter_from_path(lights_path: Path) -> str | None:
    """Extract filter from path, handling three layouts:

    New:    Target/Date_OTA_Camera/Lights/FilterName   → filter = dir name
    Old-L:  Target/Date_OTA_Camera_Filter/Lights       → filter = last _ of parent
    Old-D:  Target/Date_OTA_Camera_Filter              → filter = last _ of folder name
                (FITS directly in session folder, no Lights subdir)

    Aliases are applied so e.g. 'LPro' normalises to 'L-Pro'.
    """
    if lights_path.parent.name == "Lights":
        raw = lights_path.name
    elif lights_path.name == "Lights":
        parts = lights_path.parent.name.split("_")
        raw = parts[-1] if len(parts) >= 4 else None
    else:
        parts = lights_path.name.split("_")
        raw = parts[-1] if len(parts) >= 4 else None
    return normalize_filter(raw) if raw else None


def find_lights_folders(root: Path) -> list[Path]:
    """Recursively find dirs containing .fit/.fits files, skipping @eaDir and calibration folders.

    Walks the directory tree from root, collecting any directory that
    directly contains at least one .fit or .fits file. Synology metadata
    folders (@eaDir) and calibration frame folders (Flats, Darks, Bias,
    FlatDarks) are skipped.

    This handles three coexisting folder structures:
    1. Canonical: Target/Date_Equipment_Filter/Lights/
    2. Partial: Target/Date_Equipment_Filter/Lights/
    3. Old: Target/Lights - Label/

    Args:
        root: Root directory to search (typically the astrophotography folder)

    Returns:
        List of Path objects for directories containing FITS files
    """
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune directories we never want to descend into or collect from
        dirnames[:] = [
            d for d in dirnames
            if d != "@eaDir" and d.lower() not in _SKIP_DIR_NAMES_LOWER
        ]
        if Path(dirpath).name.lower() in _CALIB_FOLDER_NAMES:
            continue
        if any(f.lower().endswith((".fit", ".fits")) for f in filenames):
            result.append(Path(dirpath))
    return result




# ============================================================================
# SQLite Catalog Functions
# ============================================================================


# Valid values for sessions.processed_state (W1: structured processed status,
# replacing the overloaded free-text processed_status column). F1 adds
# 'in_progress' — archive-derived evidence that stacking/editing has started
# (xisf masters/intermediates) but no final export exists yet.
PROCESSED_STATES = frozenset({"unprocessed", "in_progress", "processed", "skipped"})

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_SESSIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions (
        id                       INTEGER PRIMARY KEY,
        session_id               TEXT NOT NULL UNIQUE,
        target                   TEXT NOT NULL,
        obs_date                 TEXT NOT NULL,
        ota                      TEXT,
        camera                   TEXT,
        filter                   TEXT,
        gain                     INTEGER,
        temperature_c            REAL,
        exposure_sec             REAL,
        focal_length             REAL,
        frame_count              INTEGER,
        total_integration_sec    INTEGER,
        total_integration_hours  REAL GENERATED ALWAYS AS (total_integration_sec / 3600.0) VIRTUAL,
        ra_deg                   REAL,
        dec_deg                  REAL,
        lights_path              TEXT,
        processed_status         TEXT,
        processed_state          TEXT NOT NULL DEFAULT 'unprocessed',
        processed_path           TEXT,
        processed_date           TEXT,
        notes                    TEXT,
        created_at               TEXT,
        updated_at               TEXT
    )
"""

# Legacy (pre-W3) column set, in a stable order, used to migrate an old
# session_id-PK table into the new id-PK table via CREATE ... SELECT. Only the
# columns that actually exist in the old table (after the additive migrations
# below have run) are copied — this keeps the rebuild safe against the various
# historical shapes this table has had.
_LEGACY_SESSION_COLUMNS = [
    "session_id", "target", "obs_date", "ota", "camera", "filter",
    "gain", "temperature_c", "exposure_sec", "focal_length",
    "frame_count", "total_integration_sec", "ra_deg", "dec_deg",
    "lights_path", "processed_status", "notes", "created_at", "updated_at",
]


def _backfill_processed_state(conn: sqlite3.Connection) -> None:
    """One-time parse of legacy free-text processed_status into structured columns.

    Only ever called from the id-column table rebuild (see init_db), so this
    runs exactly once per database — never on a DB that's already been
    migrated — which is what makes it safe to derive processed_state from
    processed_status without clobbering values set afterwards via
    set_processed_state.
    """
    rows = conn.execute(
        "SELECT session_id, processed_status, notes FROM sessions "
        "WHERE processed_status IS NOT NULL AND TRIM(processed_status) != ''"
    ).fetchall()
    for session_id, processed_status, notes in rows:
        text = processed_status.strip()
        new_notes = None  # None = leave notes untouched
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            state, path, date = "processed", None, text
        elif "_Processed/" in text:
            state, path = "processed", text
            m = _DATE_RE.search(text)
            date = m.group(0) if m else None
        elif text.lower().startswith("skip"):
            state, path, date = "skipped", None, None
            if not notes or not notes.strip():
                new_notes = text
        else:
            state, path = "processed", text
            m = _DATE_RE.search(text)
            date = m.group(0) if m else None

        if new_notes is not None:
            conn.execute(
                "UPDATE sessions SET processed_state = ?, processed_path = ?, "
                "processed_date = ?, notes = ? WHERE session_id = ?",
                (state, path, date, new_notes, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET processed_state = ?, processed_path = ?, "
                "processed_date = ? WHERE session_id = ?",
                (state, path, date, session_id),
            )


def init_db(db_path: Path) -> None:
    """Initialize SQLite database with sessions and calibration_sets tables.

    Creates the database if it doesn't exist, and creates tables with
    idempotent IF NOT EXISTS clauses. Existing databases are migrated forward
    additively (new columns) and, once, via a full table rebuild for the
    session_id -> id primary-key change (W3) — both paths converge to the
    same final schema as a brand-new database.

    Args:
        db_path: Path to SQLite database file
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            _SESSIONS_SCHEMA
            + """
            ;
            CREATE TABLE IF NOT EXISTS calibration_sets (
                set_id        TEXT PRIMARY KEY,
                frame_type    TEXT NOT NULL,
                camera        TEXT,
                ota           TEXT,
                filter        TEXT,
                gain          INTEGER,
                exposure_sec  REAL,
                temperature_c REAL,
                frame_count   INTEGER,
                capture_date  TEXT,
                folder_path   TEXT,
                is_master     INTEGER DEFAULT 0,
                created_at    TEXT,
                updated_at    TEXT
            );
            -- U2: archive folder moves owed to the NAS after identity edits
            -- changed a session's lights_path. The webapi host has no NAS
            -- mount, so renames are recorded here and executed later on the
            -- Mac via `darkroom catalog apply-renames`. One row per session
            -- (UNIQUE session_row_id): old_path stays pinned to what's still
            -- on disk while new_path tracks the latest catalog value.
            CREATE TABLE IF NOT EXISTS pending_renames (
                id              INTEGER PRIMARY KEY,
                session_row_id  INTEGER NOT NULL UNIQUE,
                session_id      TEXT NOT NULL,
                old_path        TEXT NOT NULL,
                new_path        TEXT NOT NULL,
                created_at      TEXT,
                updated_at      TEXT
            );
        """
        )
        # Additive migrations for existing (pre-W3) sessions tables. These must
        # run before the id-column rebuild below so the rebuild's column
        # detection sees the fully-migrated legacy column set.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "focal_length" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN focal_length REAL")
        if "created_at" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
        conn.execute(
            "UPDATE sessions SET created_at = datetime('now'), updated_at = datetime('now') "
            "WHERE created_at IS NULL"
        )

        # W3: rebuild the table to promote `id` to the primary key and demote
        # session_id to a UNIQUE column. SQLite can't ALTER a primary key in
        # place. Gated on `id` being absent so this runs exactly once — a
        # fresh DB already has `id` from the CREATE TABLE above, and a
        # previously-rebuilt DB will too.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "id" not in cols:
            legacy_cols = [c for c in _LEGACY_SESSION_COLUMNS if c in cols]
            col_list = ", ".join(legacy_cols)
            conn.execute("DROP TABLE IF EXISTS sessions_new")
            conn.execute(_SESSIONS_SCHEMA.replace("sessions", "sessions_new", 1))
            conn.execute(
                f"INSERT INTO sessions_new ({col_list}) SELECT {col_list} FROM sessions"
            )
            conn.execute("DROP TABLE sessions")
            conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
            # W1: one-time backfill of the structured columns from the old
            # free-text processed_status. Must happen only here, right after
            # the rebuild — never on a DB that's already gone through this.
            _backfill_processed_state(conn)

        # Indexes are (re)created here, after the rebuild above (which drops
        # them along with the old table) — safe to run every time.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_target ON sessions(target)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_obs_date ON sessions(obs_date)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_processed_state ON sessions(processed_state)"
        )

        # W2: NULL is the empty/unknown sentinel for filter, not "". Safe to
        # run unconditionally — writers no longer produce "" going forward,
        # so this is a no-op once existing rows are cleaned up.
        conn.execute("UPDATE sessions SET filter = NULL WHERE filter = ''")

        cal_cols = {r[1] for r in conn.execute("PRAGMA table_info(calibration_sets)")}
        if "is_master" not in cal_cols:
            conn.execute("ALTER TABLE calibration_sets ADD COLUMN is_master INTEGER DEFAULT 0")
        if "created_at" not in cal_cols:
            conn.execute("ALTER TABLE calibration_sets ADD COLUMN created_at TEXT")
        if "updated_at" not in cal_cols:
            conn.execute("ALTER TABLE calibration_sets ADD COLUMN updated_at TEXT")
        conn.execute(
            "UPDATE calibration_sets SET created_at = datetime('now'), updated_at = datetime('now') "
            "WHERE created_at IS NULL"
        )


def upsert_session(db_path: Path, session: dict) -> None:
    """Insert or update a session in the database.

    Uses SQLite's upsert (INSERT ... ON CONFLICT) syntax. On conflict by
    session_id, updates all fields EXCEPT processed_status, which is
    preserved to protect manually-set values during re-scans.

    Args:
        db_path: Path to SQLite database file
        session: Dictionary with keys matching the sessions table schema
    """
    session = dict(session)
    session["camera"] = _normalize_camera(session.get("camera"))
    session["exposure_sec"] = _round_exposure(session.get("exposure_sec"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    session.setdefault("created_at", now)
    session["updated_at"] = now
    # Legacy free-text column — new callers rely on processed_state (default
    # 'unprocessed') instead, so this is only populated for backward compat.
    session.setdefault("processed_status", None)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, target, obs_date, ota, camera, filter,
                gain, temperature_c, exposure_sec, focal_length,
                frame_count, total_integration_sec, ra_deg, dec_deg,
                lights_path, processed_status, notes, created_at, updated_at
            ) VALUES (
                :session_id, :target, :obs_date, :ota, :camera, :filter,
                :gain, :temperature_c, :exposure_sec, :focal_length,
                :frame_count, :total_integration_sec, :ra_deg, :dec_deg,
                :lights_path, :processed_status, :notes, :created_at, :updated_at
            )
            ON CONFLICT(session_id) DO UPDATE SET
                target                = excluded.target,
                obs_date              = excluded.obs_date,
                ota                   = excluded.ota,
                camera                = excluded.camera,
                filter                = excluded.filter,
                gain                  = excluded.gain,
                temperature_c         = excluded.temperature_c,
                exposure_sec          = excluded.exposure_sec,
                focal_length          = excluded.focal_length,
                frame_count           = excluded.frame_count,
                total_integration_sec = excluded.total_integration_sec,
                ra_deg                = excluded.ra_deg,
                dec_deg               = excluded.dec_deg,
                lights_path           = excluded.lights_path,
                notes                 = excluded.notes,
                updated_at            = excluded.updated_at
            """,
            session,
        )


def upsert_calibration_set(db_path: Path, cal_set: dict) -> None:
    """Insert or update a calibration set in the database.

    Uses SQLite's upsert syntax. On conflict by set_id, updates frame_count,
    capture_date, and folder_path (the fields most likely to change on rescan).

    Args:
        db_path: Path to SQLite database file
        cal_set: Dictionary with keys matching the calibration_sets table schema
    """
    cal_set = dict(cal_set)
    cal_set["camera"] = _normalize_camera(cal_set.get("camera"))
    cal_set["exposure_sec"] = _round_exposure(cal_set.get("exposure_sec"))
    cal_set.setdefault("is_master", 0)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cal_set.setdefault("created_at", now)
    cal_set["updated_at"] = now
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO calibration_sets (
                set_id, frame_type, camera, ota, filter,
                gain, exposure_sec, temperature_c, frame_count,
                capture_date, folder_path, is_master, created_at, updated_at
            ) VALUES (
                :set_id, :frame_type, :camera, :ota, :filter,
                :gain, :exposure_sec, :temperature_c, :frame_count,
                :capture_date, :folder_path, :is_master, :created_at, :updated_at
            )
            ON CONFLICT(set_id) DO UPDATE SET
                filter       = excluded.filter,
                frame_count  = excluded.frame_count,
                capture_date = excluded.capture_date,
                folder_path  = excluded.folder_path,
                is_master    = excluded.is_master,
                updated_at   = excluded.updated_at
            """,
            cal_set,
        )


def mark_processed(db_path: Path, session_id: str, status: str) -> bool:
    """Update the legacy free-text processed_status column. Returns True if found.

    Legacy: kept for backward compat (only `cataloger.py:finish_command`'s
    per-session path — reachable via `python -m darkroom.cataloger finish`,
    not the live `darkroom finish` — still calls this). New code should use
    `set_processed_state`, which writes the structured processed_state /
    processed_path / processed_date columns instead.

    Args:
        db_path: Path to SQLite database file
        session_id: Session ID to update
        status: New processed_status value (e.g., "2026-03-01", "/path/to/output", "skipped - tracking stars")

    Returns:
        True if the session was found and updated, False otherwise
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET processed_status = ? WHERE session_id = ?",
            (status, session_id),
        )
    return cursor.rowcount > 0


def set_processed_state(
    db_path: Path,
    session_id: str,
    *,
    state: str,
    processed_date: str | None = None,
    processed_path: str | None = None,
    notes: str | None = None,
) -> bool:
    """Update the structured processed_state (+ date/path/notes) for a session.

    This is the source of truth going forward (W1), replacing the overloaded
    free-text `processed_status` column for all live writers.

    Args:
        db_path: Path to SQLite database file
        session_id: Session ID to update
        state: One of 'unprocessed', 'processed', 'skipped'
        processed_date: Optional YYYY-MM-DD
        processed_path: Optional archive-relative _Processed path
        notes: Optional note; only overwrites existing notes when passed
            (None leaves notes untouched)

    Returns:
        True if the session was found and updated, False otherwise

    Raises:
        ValueError: if `state` is not one of the three valid enum values
    """
    if state not in PROCESSED_STATES:
        raise ValueError(
            f"Invalid processed state: {state!r} (must be one of {sorted(PROCESSED_STATES)})"
        )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if notes is not None:
        sql = (
            "UPDATE sessions SET processed_state = ?, processed_date = ?, "
            "processed_path = ?, notes = ?, updated_at = ? WHERE session_id = ?"
        )
        params = (state, processed_date, processed_path, notes, now, session_id)
    else:
        sql = (
            "UPDATE sessions SET processed_state = ?, processed_date = ?, "
            "processed_path = ?, updated_at = ? WHERE session_id = ?"
        )
        params = (state, processed_date, processed_path, now, session_id)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(sql, params)
    return cursor.rowcount > 0


def mark_processed_command(args):
    """Handle mark command — set structured processed_state (+ date/path/notes).

    Writes through the catalog backend (W9): local SQLite by default, or the
    webapi server when catalog_url / DARKROOM_CATALOG_URL is configured.
    """
    from darkroom.catalog_client import LocalBackend, resolve_backend

    backend = resolve_backend(getattr(args, "catalog", None) or args.db)
    if isinstance(backend, LocalBackend) and not backend.db_path.exists():
        print(f"Error: Database not found: {backend.db_path}", file=sys.stderr)
        sys.exit(1)
    try:
        found = backend.set_processed_state(
            args.session_id,
            state=args.state,
            processed_date=getattr(args, "date", None),
            processed_path=getattr(args, "path", None),
            notes=getattr(args, "notes", None),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not found:
        print(f"Error: Session not found: {args.session_id}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated: {args.session_id} → processed_state = {args.state!r}")


def _find_latest_processed_date(processed_root: Path) -> str:
    """Return the most recent YYYY-MM-DD subdir name inside processed_root.

    Scans processed_root for subdirectories whose names match the YYYY-MM-DD
    pattern. Exits with an error message if the root doesn't exist or no dated
    subdirectories are found. Prints a notice if multiple dates are present and
    returns the most recent one (alphabetical sort is correct for ISO dates).

    Args:
        processed_root: Path to the _Processed/ directory.

    Returns:
        Most recent date string in YYYY-MM-DD format.
    """
    if not processed_root.exists():
        sys.exit(f"Error: _Processed directory not found: {processed_root}")
    date_dirs = sorted(
        d.name
        for d in processed_root.iterdir()
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
    )
    if not date_dirs:
        sys.exit(f"Error: No dated subdirectory (YYYY-MM-DD) found in {processed_root}")
    if len(date_dirs) > 1:
        print(f"Multiple processed dates found: {date_dirs}; using most recent: {date_dirs[-1]}")
    return date_dirs[-1]


def mark_processed_by_target(db_path: Path, target: str, status: str) -> int:
    """Mark all sessions matching target (case-insensitive) as processed.

    Writes the structured columns (W1) rather than the legacy processed_status:
    sets processed_state='processed' and processed_date=status (status here is
    always a YYYY-MM-DD, per the finish_command caller).

    Args:
        db_path: Path to SQLite database file.
        target: Target name to match (e.g. "M 81"). Spacing is canonicalised
            ('M81' → 'M 81') and the comparison is case-insensitive.
        status: Processed date, YYYY-MM-DD (e.g. "2026-05-15").

    Returns:
        Number of rows updated.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET processed_state = 'processed', processed_date = ?, "
            "updated_at = ? WHERE target = ? COLLATE NOCASE",
            (status, now, _normalize_target(target)),
        )
        return cursor.rowcount


def finish_command(args) -> None:
    """Handle finish command — mark target or sessions as processed."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    if args.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print(f"Error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        sys.exit(1)

    # Canonicalise the user-supplied target so the archive folder path and the
    # catalog lookup both use the stored form (e.g. 'M81' → 'M 81').
    target = _normalize_target(args.target) if args.target else args.target

    if args.date:
        date_str = args.date
    else:
        processed_root = (
            Path(args.archive) / "01_Deep Sky Objects" / target / "_Processed"
        )
        date_str = _find_latest_processed_date(processed_root)

    if args.session:
        updated = 0
        for sid in args.session:
            if mark_processed(db_path, sid, date_str):
                print(f"Updated: {sid} → processed_status = {date_str!r}")
                updated += 1
            else:
                print(f"Warning: session not found: {sid}", file=sys.stderr)
        if updated == 0:
            sys.exit(1)
        print(f"\nDone: {updated}/{len(args.session)} session(s) updated")
    else:
        count = mark_processed_by_target(db_path, target, date_str)
        if count == 0:
            print(
                f"Warning: no sessions found for target {target!r}",
                file=sys.stderr,
            )
        else:
            print(
                f"Updated {count} session(s) for target {target!r}"
                f" → processed_status = {date_str!r}"
            )


class FITSHeaderExtractor:
    """Extract metadata from FITS file headers."""

    @staticmethod
    def extract_metadata(fits_path: Path) -> dict | None:
        try:
            with fits.open(fits_path) as hdul:
                header = hdul[0].header
                ra_raw = header.get("RA") or header.get("OBJCTRA") or header.get("CRVAL1")
                dec_raw = header.get("DEC") or header.get("OBJCTDEC") or header.get("CRVAL2")
                ra_deg, dec_deg = _parse_coords(ra_raw, dec_raw)
                return {
                    "filename_stem": fits_path.stem,
                    "file_path": str(fits_path),
                    "date_obs": header.get("DATE-OBS", ""),
                    "exposure": float(header.get("EXPOSURE", header.get("EXPTIME", 0.0))),
                    "camera": header.get("INSTRUME", "Unknown"),
                    "gain": _parse_gain(header),
                    "temperature": float(header.get("CCD-TEMP", header.get("SET-TEMP", 0.0))),
                    "object": header.get("OBJECT", ""),
                    "filter_header": header.get("FILTER", None),
                    "imagetyp": header.get("IMAGETYP", None),
                    "focallen": header.get("FOCALLEN", None),
                    "ra_deg": ra_deg,
                    "dec_deg": dec_deg,
                }
        except Exception as e:
            print(f"Warning: Could not read {fits_path}: {e}", file=sys.stderr)
            return None


class SessionAnalyzer:
    """Analyze groups of FITS metadata dicts to extract per-night session records."""

    @staticmethod
    def analyze_sessions(metadata_list: list[dict], lights_path: Path) -> list[dict]:
        """Group frames by imaging night and return one session dict per night.

        A "night" is defined by the local civil date it started: frames between
        local noon on day N and local noon on day N+1 belong to night N.
        Frames without a resolvable DATE-OBS are skipped with a warning.
        """
        if not metadata_list:
            return []

        # Group frames by imaging night
        groups: dict[str, list[dict]] = {}
        for meta in metadata_list:
            night = compute_imaging_night(meta.get("date_obs", ""))
            if night is None:
                print(
                    f"Warning: skipping {meta['file_path']} — no resolvable DATE-OBS",
                    file=sys.stderr,
                )
                continue
            groups.setdefault(night, []).append(meta)

        sessions = []
        for night, frames in sorted(groups.items()):
            first = frames[0]

            # Filter: filename-first, header fallback — scoped to this night's frames
            filter_ = None
            for f in frames:
                filter_ = parse_filter(f["filename_stem"])
                if filter_ is not None:
                    break
            if filter_ is None:
                filter_ = first.get("filter_header") or _filter_from_path(lights_path) or None

            focallen = first.get("focallen")
            sessions.append({
                "target": _normalize_target(first["object"] or _target_from_path(lights_path)),
                "obs_date": night,
                "ota": parse_ota(focallen),
                "camera": first["camera"],
                "filter": filter_,
                "gain": first["gain"],
                "temperature_c": first["temperature"],
                "exposure_sec": first["exposure"],
                "focal_length": float(focallen) if focallen is not None else None,
                "frame_count": len(frames),
                "total_integration_sec": int(sum(f["exposure"] for f in frames)),
                "ra_deg": first.get("ra_deg"),
                "dec_deg": first.get("dec_deg"),
                "lights_path": str(lights_path),
                "notes": "",
            })
        return sessions


_FRAME_TYPE_KEYWORDS = {
    "dark": "Dark",
    "flat": "Flat",
    "bias": "Bias",
    "flatdark": "FlatDark",
}

# ASIAir stores flat darks in the same folder as science darks with no distinguishing
# IMAGETYP. Exposure time is the only reliable separator: science darks are 120s+,
# flat darks are sub-second to low-single-digit seconds.
_FLAT_DARK_THRESHOLD_SEC = 10.0


def _infer_frame_type(fits_path: Path, imagetyp: str | None) -> str:
    """Infer frame type from IMAGETYP header or folder name."""
    if imagetyp:
        lower = imagetyp.lower().replace(" ", "")
        for key, val in _FRAME_TYPE_KEYWORDS.items():
            if key in lower:
                return val
    folder_lower = fits_path.parent.name.lower()
    if "flatdark" in folder_lower:
        return "FlatDark"
    if "flat" in folder_lower:
        return "Flat"
    if "dark" in folder_lower:
        return "Dark"
    if "bias" in folder_lower:
        return "Bias"
    return "Unknown"


_MASTER_PREFIX_RE = re.compile(r"^master(dark|bias|flat)", re.IGNORECASE)
_MASTER_EXPOSURE_RE = re.compile(r"_(\d+(?:\.\d+)?)s(?:_|$)", re.IGNORECASE)
_MASTER_TEMP_RE = re.compile(r"_(-?\d+)C(?:_|$)", re.IGNORECASE)
_MASTER_GAIN_RE = re.compile(r"_gain(\d+)", re.IGNORECASE)
_MASTER_ISO_RE = re.compile(r"_ISO(\d+)", re.IGNORECASE)

_MASTER_TYPE_MAP = {"dark": "Dark", "bias": "Bias", "flat": "Flat"}


def _parse_master_filename(stem: str, camera: str) -> dict | None:
    """Parse metadata from a master calibration filename (no FITS header needed).

    Returns None if the stem doesn't match a recognised master pattern.
    Camera is supplied from the directory structure (grandparent of Masters/).
    """
    m = _MASTER_PREFIX_RE.match(stem)
    if not m:
        return None
    frame_type = _MASTER_TYPE_MAP.get(m.group(1).lower())
    if not frame_type:
        return None

    exp_m = _MASTER_EXPOSURE_RE.search(stem)
    exposure = _round_exposure(float(exp_m.group(1))) if exp_m else None

    temp_m = _MASTER_TEMP_RE.search(stem)
    temp = int(temp_m.group(1)) if temp_m else None

    gain_m = _MASTER_GAIN_RE.search(stem)
    iso_m = _MASTER_ISO_RE.search(stem)
    if gain_m:
        gain = int(gain_m.group(1))
    elif iso_m:
        gain = int(iso_m.group(1))
    else:
        gain = 0

    return {
        "frame_type": frame_type,
        "camera": camera,
        "gain": gain,
        "exposure_sec": exposure,
        "temperature_c": float(temp) if temp is not None else None,
    }


class CalibrationCataloger:
    @staticmethod
    def scan(calibration_root: Path) -> list[dict]:
        """Recursively find and group calibration FITS files and master .xisf files."""
        groups: dict[tuple, dict] = {}

        masters: list[dict] = []

        for dirpath, dirnames, filenames in os.walk(calibration_root):
            dirnames[:] = [d for d in dirnames if d != "@eaDir"]
            cur_dir = Path(dirpath)
            in_masters_dir = cur_dir.name.lower() == "masters"

            for fname in filenames:
                fpath = cur_dir / fname
                flower = fname.lower()

                # Master .xisf files live in Masters/ subdirs — parse from filename.
                if flower.endswith(".xisf") and in_masters_dir:
                    # Camera is the grandparent dir name (e.g. Darks/ZWOASI585MCPro/Masters/)
                    camera = _normalize_camera(cur_dir.parent.name)
                    parsed = _parse_master_filename(fpath.stem, camera)
                    if parsed:
                        masters.append({**parsed, "folder_path": str(fpath)})
                    continue

                if not flower.endswith((".fit", ".fits")):
                    continue

                meta = FITSHeaderExtractor.extract_metadata(fpath)
                if not meta:
                    continue

                frame_type = _infer_frame_type(fpath, meta.get("imagetyp"))
                camera = _normalize_camera(meta["camera"])
                gain = meta["gain"]
                exposure = _round_exposure(meta["exposure"])
                temp = round(meta["temperature"])
                folder = str(fpath.parent)
                obs_date = ""
                if meta.get("date_obs"):
                    try:
                        t = Time(meta["date_obs"], format="isot")
                        obs_date = t.datetime.strftime("%Y-%m-%d")
                    except Exception:
                        pass

                # ASIAir mixes flat darks and science darks in the same Dark/ folder.
                # Reclassify by exposure: anything under the threshold is a flat dark.
                if frame_type == "Dark" and exposure < _FLAT_DARK_THRESHOLD_SEC:
                    frame_type = "FlatDark"

                # Filter: only meaningful for flats and flat darks; extract from filename.
                filter_ = None
                if frame_type in ("Flat", "FlatDark"):
                    filter_ = parse_filter(Path(fname).stem)
                    if filter_ is None:
                        filter_ = meta.get("filter_header") or None

                key = (frame_type, camera, gain, exposure, temp, obs_date, folder)
                if key not in groups:
                    groups[key] = {
                        "frame_type": frame_type,
                        "camera": camera,
                        "gain": gain,
                        "exposure_sec": exposure,
                        "temperature_c": float(temp),
                        "capture_date": obs_date,
                        "folder_path": folder,
                        "ota": parse_ota(meta.get("focallen")),
                        "filter": filter_,
                        "count": 0,
                    }
                groups[key]["count"] += 1

        cal_sets = []
        for group in groups.values():
            camera_slug = _normalize_camera(group["camera"])
            temp_str = f"{int(group['temperature_c'])}C"
            # set_id omits folder deliberately — same params from different folders merge on re-scan,
            # keeping the most recent folder_path. This is intentional for portability.
            set_id = (
                f"{group['frame_type']}_{camera_slug}"
                f"_{group['exposure_sec']:.3g}s_{_format_gain(group['camera'], group['gain'])}"
                f"_{temp_str}_{group['capture_date']}"
            )
            cal_sets.append({
                "set_id": set_id,
                "frame_type": group["frame_type"],
                "camera": group["camera"],
                "ota": group["ota"],
                "filter": group["filter"],
                "gain": group["gain"],
                "exposure_sec": group["exposure_sec"],
                "temperature_c": group["temperature_c"],
                "frame_count": group["count"],
                "capture_date": group["capture_date"],
                "folder_path": group["folder_path"],
                "is_master": 0,
            })

        for m in masters:
            camera = m["camera"]
            camera_slug = _normalize_camera(camera)
            gain_str = _format_gain(camera, m["gain"])
            temp_str = f"{int(m['temperature_c'])}C" if m["temperature_c"] is not None else "unknownC"
            exp_str = f"{m['exposure_sec']:.3g}s" if m["exposure_sec"] is not None else "0s"
            set_id = f"{m['frame_type']}Master_{camera_slug}_{exp_str}_{gain_str}_{temp_str}"
            cal_sets.append({
                "set_id": set_id,
                "frame_type": m["frame_type"],
                "camera": camera,
                "ota": None,
                "filter": None,
                "gain": m["gain"],
                "exposure_sec": m["exposure_sec"],
                "temperature_c": m["temperature_c"],
                "frame_count": 1,
                "capture_date": "",
                "folder_path": m["folder_path"],
                "is_master": 1,
            })

        return cal_sets


def scan_calibration_command(args):
    calibration_root = Path(args.calibration_path).resolve()
    archive_root = calibration_root.parent
    db_path = Path(args.db)

    if not calibration_root.exists():
        print(f"Error: Calibration folder not found: {calibration_root}", file=sys.stderr)
        sys.exit(1)

    init_db(db_path)
    cal_sets = CalibrationCataloger.scan(calibration_root)

    if not cal_sets:
        print("No calibration frames found.")
        sys.exit(0)

    for cal_set in cal_sets:
        try:
            cal_set["folder_path"] = str(
                Path(cal_set["folder_path"]).relative_to(archive_root)
            )
        except ValueError:
            pass
        upsert_calibration_set(db_path, cal_set)
        print(f"  {cal_set['set_id']}  ({cal_set['frame_count']} frames)")

    print(f"\nDone: {len(cal_sets)} calibration sets cataloged")
    print(f"Database: {db_path}")




def scan_all_command(args):
    """Handle scan-all command (recursive scan of all targets/dates).

    Walks the directory tree from root to find any folder containing FITS files,
    extracts metadata from each group, builds a session record with collision-resistant
    session_id, and writes to SQLite database.

    Handles three coexisting NAS folder structures:
    1. Canonical: Target/Date_Equipment_Filter/Lights/
    2. Partial: Target/Date_Equipment_Filter/  (Lights optional)
    3. Old: Target/Lights - Label/
    """
    root = Path(args.root_path).resolve()
    archive_root = root.parent
    db_path = Path(args.db)

    if not root.exists():
        print(f"Error: Root folder not found: {root}", file=sys.stderr)
        sys.exit(1)

    init_db(db_path)
    lights_folders = find_lights_folders(root)

    if not lights_folders:
        print("No FITS files found.")
        sys.exit(0)

    print(f"Found {len(lights_folders)} folder(s) containing FITS files")

    added = 0
    skipped = 0
    for lights_path in sorted(lights_folders):
        fits_files = sorted(
            f for f in lights_path.iterdir()
            if f.is_file() and f.suffix.lower() in (".fit", ".fits")
        )
        metadata_list = [FITSHeaderExtractor.extract_metadata(f) for f in fits_files]
        metadata_list = [m for m in metadata_list if m]

        if not metadata_list:
            print(f"  Skipped (no readable FITS): {lights_path}")
            skipped += 1
            continue

        sessions = SessionAnalyzer.analyze_sessions(metadata_list, lights_path)
        if not sessions:
            print(f"  Skipped (no resolvable nights): {lights_path}")
            skipped += 1
            continue

        for session in sessions:
            session_id = make_session_id(
                session["target"], session["obs_date"],
                session["ota"], session["camera"], session["filter"],
            )
            session["session_id"] = session_id
            session["lights_path"] = str(lights_path.relative_to(archive_root))
            upsert_session(db_path, session)
            print(f"  {session_id}  ({session['frame_count']} frames, {session['total_integration_sec']}s)")
            added += 1

    print(f"\nDone: {added} sessions cataloged, {skipped} skipped")
    print(f"Database: {db_path}")


def migrate_archive_command(args) -> None:
    """Move sessions from old filter-in-folder layout to new Lights/<filter>/ layout.

    Old: 01_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>_<Filter>/Lights/*.fit
    New: 01_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>/Lights/<Filter>/*.fit
    """
    from darkroom.ingest import camera_slug, session_dest_rel

    archive = Path(args.archive)
    db_path = Path(args.db)
    dry_run = getattr(args, "dry_run", False)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT session_id, target, obs_date, ota, camera, filter, lights_path FROM sessions"
    ).fetchall()

    migrated = 0
    skipped = 0

    for row in rows:
        row = dict(row)
        old_rel = Path(row["lights_path"])

        if old_rel.parent.name == "Lights":
            # Already new format
            continue
        if old_rel.name != "Lights":
            print(f"  [SKIP] Unrecognized path format: {row['lights_path']}", file=sys.stderr)
            skipped += 1
            continue

        new_rel = session_dest_rel(
            row["target"], row["obs_date"], row["ota"], row["camera"], row["filter"]
        )

        old_abs = archive / old_rel
        new_abs = archive / new_rel

        if not old_abs.exists():
            print(f"  [SKIP] Not found on disk: {old_abs}", file=sys.stderr)
            skipped += 1
            continue

        fits_files = sorted(
            f for f in old_abs.iterdir()
            if f.is_file() and f.suffix.lower() in (".fit", ".fits")
        )

        if dry_run:
            print(f"  MOVE  {old_abs}")
            print(f"     -> {new_abs}  ({len(fits_files)} file(s))")
            print(f"        UPDATE lights_path WHERE session_id='{row['session_id']}'")
        else:
            new_abs.mkdir(parents=True, exist_ok=True)
            for f in fits_files:
                f.rename(new_abs / f.name)
            try:
                old_abs.rmdir()
            except OSError:
                print(f"  [WARN] Could not remove {old_abs}", file=sys.stderr)
            try:
                old_abs.parent.rmdir()
            except OSError:
                pass  # Old session folder still has other filter dirs — expected
            con.execute(
                "UPDATE sessions SET lights_path = ? WHERE session_id = ?",
                (str(new_rel), row["session_id"]),
            )
            con.commit()
            print(f"  OK    {row['session_id']}")

        migrated += 1

    con.close()

    suffix = " (dry run)" if dry_run else ""
    print(f"\nMigrated {migrated} session(s){suffix}, {skipped} skipped.")


def main():
    parser = argparse.ArgumentParser(
        description="FITS astrophotography session cataloger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initial scan of all sessions
  %(prog)s scan-all "/Volumes/Astrophotography/01_Deep Sky Objects"

  # Scan calibration frames
  %(prog)s scan-calibration /Volumes/Astrophotography/00_Calibration

  # Mark a session's structured processed_state
  %(prog)s mark-processed M81_20260219_FRA400_ASI585MC_L-Pro processed --date 2026-03-01

  # Mark all sessions for a target as processed (date auto-detected from archive)
  %(prog)s finish --target "M 81" --archive /Volumes/Astrophotography

  # Mark specific sessions only
  %(prog)s finish --target "M 81" --archive /Volumes/Astrophotography \\
      --session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro

  # Fallback when _Processed/ is already cleaned up
  %(prog)s finish --target "M 81" --date 2026-05-15
        """,
    )
    parser.add_argument(
        "--db",
        default="astro_catalog.db",
        help="SQLite database file (default: astro_catalog.db)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # scan-all
    p_all = subparsers.add_parser("scan-all", help="Recursively catalog all light sessions")
    p_all.add_argument("root_path", help="Root folder to scan (e.g. '01_Deep Sky Objects')")

    # scan-calibration
    p_cal = subparsers.add_parser("scan-calibration", help="Catalog calibration frames")
    p_cal.add_argument("calibration_path", help="Path to calibration folder (e.g. 00_Calibration)")

    # mark-processed
    p_mark = subparsers.add_parser("mark-processed", help="Set structured processed_state for a session")
    p_mark.add_argument("session_id", help="Session ID (e.g. M81_20260219_FRA400_ASI585MC_L-Pro)")
    p_mark.add_argument("state", choices=sorted(PROCESSED_STATES), help="New processed_state")
    p_mark.add_argument("--date", metavar="YYYY-MM-DD", help="processed_date")
    p_mark.add_argument("--path", metavar="PATH", help="processed_path (archive-relative _Processed path)")
    p_mark.add_argument("--notes", metavar="TEXT", help="Notes (only overwrites existing notes when passed)")

    # finish
    p_finish = subparsers.add_parser(
        "finish",
        help="Mark a target or sessions as processed after WBPP + PixInsight",
    )
    p_finish.add_argument(
        "--target", required=True, metavar="NAME",
        help='Target name as stored in the catalog (e.g. "M 81")',
    )
    p_finish_date = p_finish.add_mutually_exclusive_group(required=True)
    p_finish_date.add_argument(
        "--archive", metavar="PATH",
        help=(
            "NAS archive root — navigates to "
            "<archive>/01_Deep Sky Objects/<target>/_Processed/ to detect the date "
            "(targets outside 01_Deep Sky Objects/ should use --date instead)"
        ),
    )
    p_finish_date.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Processed date override (use when _Processed/ has already been cleaned up)",
    )
    p_finish.add_argument(
        "--session", nargs="+", metavar="SESSION_ID",
        help="Specific session IDs to update (default: all sessions for --target)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "scan-all":
        scan_all_command(args)
    elif args.command == "scan-calibration":
        scan_calibration_command(args)
    elif args.command == "mark-processed":
        mark_processed_command(args)
    elif args.command == "finish":
        finish_command(args)


if __name__ == "__main__":
    main()
