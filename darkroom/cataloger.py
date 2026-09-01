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
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from astropy.io import fits
from astropy.time import Time

from darkroom.parse import (
    fits_files,
    normalize_filter,
    panel_from_dirname,
    parse_filter,
    parse_ota,
    parse_panel,
    reclassify_flat_dark,
)
from darkroom.names import (
    KNOWN_FILTERS,
    PROCESSED_STATES,  # re-exported: catalog_db and the CLI import it from here
    _format_gain,
    _normalize_camera,
    _normalize_target,
    _parse_coords,
    _round_exposure,
    make_session_id,  # re-exported for back-compat (moved to names.py in W4)
    normalize_session_fields,
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


def parse_date_obs(date_obs_utc: str) -> datetime | None:
    """Parse a FITS DATE-OBS (always UTC on this rig) into an aware UTC datetime.

    Same astropy `isot`/`utc` handling as compute_imaging_night, so the two
    agree on what a frame's timestamp means. Returns None for anything
    unparseable — this runs per-frame, so failures stay silent.
    """
    if not date_obs_utc:
        return None
    try:
        t = Time(str(date_obs_utc).strip(), format="isot", scale="utc")
        return t.datetime.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def capture_date_of(date_obs_utc: str) -> str:
    """Calendar date (YYYY-MM-DD, UTC) of a FITS DATE-OBS, or "" if unparseable.

    The calibration-set `capture_date`: both the ASIAir-side scan
    (darkroom.scanner) and the archive-side one (CalibrationCataloger) used to
    hand-roll this astropy parse; they now share parse_date_obs.
    """
    dt = parse_date_obs(date_obs_utc)
    return dt.strftime("%Y-%m-%d") if dt else ""


def _now() -> str:
    """UTC timestamp in the created_at/updated_at column format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _format_utc(dt: datetime) -> str:
    """Render an aware datetime as second-resolution ISO UTC, no offset suffix.

    Matches the shape FITS DATE-OBS already uses ("2026-07-28T21:55:17") so
    stored spans sort lexicographically against each other and parse with
    datetime.fromisoformat.
    """
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def compute_session_span(frames) -> tuple[str | None, str | None]:
    """Return (start_utc, end_utc) ISO strings for an iterable of light frames.

    *frames* is an iterable of (date_obs, exposure_sec) pairs, in any order.
    start_utc is the earliest DATE-OBS; end_utc is the latest DATE-OBS plus
    *that* frame's exposure, so the span covers the final sub-exposure rather
    than stopping when it started.

    Frames are sorted here rather than trusted in file-iteration order — the
    scan paths collect them by directory walk, which is not chronological.
    (F4: this span is what guide-log segments get intersected against.)
    """
    parsed = []
    for date_obs, exposure in frames:
        dt = parse_date_obs(date_obs)
        if dt is None:
            continue
        try:
            exp = float(exposure or 0.0)
        except (TypeError, ValueError):
            exp = 0.0
        parsed.append((dt, exp))
    if not parsed:
        return None, None
    parsed.sort(key=lambda p: p[0])
    last_dt, last_exp = parsed[-1]
    return _format_utc(parsed[0][0]), _format_utc(last_dt + timedelta(seconds=last_exp))


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


def _parse_site_deg(val) -> float | None:
    """Parse a SITELAT/SITELONG header value into decimal degrees.

    ASIAir writes these as decimal-degree floats, but tolerate sign-aware
    sexagesimal strings ("38 33 47", "-8:52:53") too. Returns None for
    anything unparseable — this runs per-frame, so failures are silent.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    parts = [p for p in re.split(r"[ :]+", s) if p]
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    deg = abs(nums[0])
    minutes = nums[1]
    seconds = nums[2] if len(nums) == 3 else 0.0
    result = deg + minutes / 60.0 + seconds / 3600.0
    return -result if parts[0].startswith("-") else result


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

    The trailing component is only trusted when it actually names a filter
    (`names.KNOWN_FILTERS`). Folder names carry things that are not filters —
    `.../20250802_FRA400_NoFilter_RGB_Stars` is a broadband star layer shot to
    be composited onto narrowband data, and taking its last component invented
    `filter='Stars'` (M2). When the last component isn't a known filter, the
    remaining components are searched for one — which recovers the `NoFilter`
    that folder does state — and failing that this returns None, so the
    session lands in the U2 review queue as UnknownFilter rather than
    inventing a value. Same class of bug U2 cleaned up when mosaic panel
    names ended up in the filter column.
    Since M1 a mosaic session nests one level deeper
    (`Lights/<Filter>/P1-1/`), so a trailing panel dir is stripped first and
    the filter is read from the directory above it — otherwise the panel dir
    is mistaken for the filter dir and every panel reads as UnknownFilter.
    """
    if panel_from_dirname(lights_path.name):
        lights_path = lights_path.parent

    if lights_path.parent.name == "Lights":
        parts = [lights_path.name]
    elif lights_path.name == "Lights":
        parts = lights_path.parent.name.split("_")
        parts = parts if len(parts) >= 4 else []
    else:
        parts = lights_path.name.split("_")
        parts = parts if len(parts) >= 4 else []

    if not parts:
        return None
    # Last component first (the canonical position), then the rest.
    for raw in [parts[-1], *parts[:-1]]:
        candidate = normalize_filter(raw)
        if candidate in KNOWN_FILTERS:
            return candidate
    return None


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
    # Not routed through parse.fits_files() (R5, BACKLOG.md): this just needs
    # a per-directory boolean off os.walk's own `filenames` list, not a file
    # collection — calling fits_files() here would re-stat/re-iterdir() each
    # directory os.walk already gave us for free. It also doesn't exclude
    # "_thn" thumbnails, but that's harmless: a thumbnail-only directory would
    # still end up with an empty metadata_list in scan_all_command (which does
    # exclude thumbnails) and get skipped there — same end result either way.
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
        panel                    TEXT,
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
        updated_at               TEXT,
        site_lat                 REAL,
        site_lon                 REAL,
        start_utc                TEXT,
        end_utc                  TEXT
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
            -- S1: named observing sites for session->site proximity resolution and
            -- SQM-based depth weighting. bortle/sqm are user-entered (nullable).
            -- Exactly one row may be the home reference (partial unique index below).
            CREATE TABLE IF NOT EXISTS sites (
                id          INTEGER PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                lat         REAL NOT NULL,
                lon         REAL NOT NULL,
                radius_m    REAL NOT NULL DEFAULT 1000,
                bortle      INTEGER,
                sqm         REAL,
                is_home     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT,
                updated_at  TEXT
            );
            -- F4: per-session guiding quality, derived by intersecting PHD2
            -- guide-log segments with the session's start_utc/end_utc span
            -- (`darkroom catalog scan-guiding`). A side table, not columns on
            -- sessions: "no guiding data" is simply row-absent, and re-scans
            -- can INSERT OR REPLACE a whole row without touching the session.
            -- `coverage` = guided seconds / session wall span — the guard
            -- against a partial log looking authoritative.
            CREATE TABLE IF NOT EXISTS session_guiding (
                session_id         TEXT PRIMARY KEY,
                rms_ra_arcsec      REAL,
                rms_dec_arcsec     REAL,
                rms_total_arcsec   REAL,
                peak_arcsec        REAL,
                p95_arcsec         REAL,
                guide_frames       INTEGER,
                excluded_frames    INTEGER,
                dropped_frames     INTEGER,
                star_lost_events   INTEGER,
                dither_count       INTEGER,
                guided_sec         INTEGER,
                coverage           REAL,
                pixel_scale_arcsec REAL,
                guide_camera       TEXT,
                guide_exposure_ms  INTEGER,
                source_logs        TEXT,   -- JSON array of log basenames
                computed_at        TEXT
            );
            -- F8: divergences found by a Mac-side `catalog rescan-archive`
            -- pass (the webapi host has no archive mount, so it can never
            -- generate these itself) between what's on disk and what the
            -- catalog says. One row per divergence; `changes` is the
            -- {field: {current, proposed}} diff JSON. status starts
            -- 'pending' and moves to 'applied'/'dismissed' — rows are never
            -- deleted, so resolved rows are the audit trail.
            -- replace_rescan_proposals only ever touches the pending set.
            CREATE TABLE IF NOT EXISTS rescan_proposals (
                id           INTEGER PRIMARY KEY,
                session_id   TEXT NOT NULL,
                kind         TEXT NOT NULL,
                tier         TEXT NOT NULL,
                target       TEXT,
                obs_date     TEXT,
                lights_path  TEXT,
                changes      TEXT NOT NULL,
                detected_at  TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                resolved_at  TEXT
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

        # S1: observing-site coordinates from FITS SITELAT/SITELONG headers.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "site_lat" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN site_lat REAL")
        if "site_lon" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN site_lon REAL")

        # F4: session wall-clock span in UTC, for intersecting guide-log
        # segments with a night. CREATE TABLE IF NOT EXISTS above is a no-op on
        # an existing table, so an already-live DB only gets these here.
        if "start_utc" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN start_utc TEXT")
        if "end_utc" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN end_utc TEXT")

        # M1: mosaic panel label ("1-1"), NULL for an ordinary single-pointing
        # session. Part of session identity — see names.make_session_id.
        if "panel" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN panel TEXT")

        # Indexes are (re)created here, after the rebuild above (which drops
        # them along with the old table) — safe to run every time.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_target ON sessions(target)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_obs_date ON sessions(obs_date)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_processed_state ON sessions(processed_state)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sites_home ON sites(is_home) WHERE is_home = 1"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rescan_proposals_status "
            "ON rescan_proposals(status)"
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


def session_id_for(session: dict) -> str:
    """make_session_id from a session dict's identity fields (panel included).

    The one place a scanned session dict becomes a session_id — scan_all and
    rescan each spelled the call out by hand, and scan_all's copy had missed
    `panel` when M1 added it.
    """
    return make_session_id(
        session["target"], session["obs_date"], session["ota"],
        session["camera"], session["filter"], panel=session.get("panel"),
    )


# Every column upsert_session writes, in INSERT order. Drives the column list,
# the :named placeholders and the "missing key -> NULL" defaulting below, so a
# new column is added in exactly one place (same pattern as _GUIDING_COLUMNS).
_SESSION_COLUMNS = (
    "session_id", "target", "obs_date", "ota", "camera", "filter", "panel",
    "gain", "temperature_c", "exposure_sec", "focal_length",
    "frame_count", "total_integration_sec", "ra_deg", "dec_deg",
    "lights_path", "processed_status", "notes", "created_at", "updated_at",
    "site_lat", "site_lon", "start_utc", "end_utc",
)


def upsert_session(db_path: Path, session: dict) -> None:
    """Insert or update a session in the database.

    Uses SQLite's upsert (INSERT ... ON CONFLICT) syntax. On conflict by
    session_id, updates all fields EXCEPT processed_status/processed_state,
    which are preserved to protect manually-set values during re-scans.

    `notes` is treated the same way, but one step softer: a non-empty incoming
    note still wins, while an empty one leaves what is already there. Ingest
    never has a note to contribute (it always sends ""), so without this a
    re-ingest of an already-catalogued session silently destroyed whatever was
    written about that night — which is easy to trigger, since a session only
    has to *look* new for commit to upsert it. Matches the convention
    `set_processed_state` already follows (None leaves notes untouched).

    Args:
        db_path: Path to SQLite database file
        session: Dictionary with keys matching the sessions table schema
    """
    # Shared with darkroom.rescan, which has to diff a fresh scan against the
    # catalog and so must compare against what would be STORED, not the raw
    # header values (names.normalize_session_fields explains why).
    session = normalize_session_fields(session)
    now = _now()
    session.setdefault("created_at", now)
    session["updated_at"] = now
    session.setdefault("notes", "")
    # Every other column a caller leaves out is NULL: the legacy free-text
    # processed_status, the optional site/span fields, the M1 panel (NULL for
    # the ordinary single-pointing session), and whatever a rescan 'create'
    # could not read off disk. The NOT NULL columns (target, obs_date) still
    # fail at the INSERT, as they should.
    for col in _SESSION_COLUMNS:
        session.setdefault(col, None)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO sessions ({", ".join(_SESSION_COLUMNS)})
            VALUES ({", ".join(":" + c for c in _SESSION_COLUMNS)})
            ON CONFLICT(session_id) DO UPDATE SET
                target                = excluded.target,
                obs_date              = excluded.obs_date,
                ota                   = excluded.ota,
                camera                = excluded.camera,
                filter                = excluded.filter,
                panel                 = excluded.panel,
                gain                  = excluded.gain,
                temperature_c         = excluded.temperature_c,
                exposure_sec          = excluded.exposure_sec,
                focal_length          = excluded.focal_length,
                frame_count           = excluded.frame_count,
                total_integration_sec = excluded.total_integration_sec,
                ra_deg                = excluded.ra_deg,
                dec_deg               = excluded.dec_deg,
                lights_path           = excluded.lights_path,
                notes                 = COALESCE(NULLIF(excluded.notes, ''), sessions.notes),
                updated_at            = excluded.updated_at,
                site_lat              = COALESCE(excluded.site_lat, sessions.site_lat),
                site_lon              = COALESCE(excluded.site_lon, sessions.site_lon),
                start_utc             = COALESCE(excluded.start_utc, sessions.start_utc),
                end_utc               = COALESCE(excluded.end_utc, sessions.end_utc)
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
    now = _now()
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
                -- F9: `ota` has to be re-derivable. It is inferred from
                -- FOCALLEN (plus, since F9, the capture date), and a wrong
                -- inference used to be permanent — a rescan re-derived the
                -- right optic and then silently discarded it here, because
                -- set_id carries camera/exposure/gain/temp/date but not the
                -- optic. That is how 6 Canon-zoom flat sets sat labelled
                -- FRA400. `camera` needs no such line: it is part of set_id,
                -- so a change there is a new row rather than a conflict.
                ota          = excluded.ota,
                frame_count  = excluded.frame_count,
                capture_date = excluded.capture_date,
                folder_path  = excluded.folder_path,
                is_master    = excluded.is_master,
                updated_at   = excluded.updated_at
            """,
            cal_set,
        )


_GUIDING_COLUMNS = (
    "session_id",
    "rms_ra_arcsec", "rms_dec_arcsec", "rms_total_arcsec",
    "peak_arcsec", "p95_arcsec",
    "guide_frames", "excluded_frames", "dropped_frames",
    "star_lost_events", "dither_count",
    "guided_sec", "coverage",
    "pixel_scale_arcsec", "guide_camera", "guide_exposure_ms",
    "source_logs", "computed_at",
)


def upsert_session_guiding(db_path: Path, guiding: dict) -> None:
    """Insert or replace one session's guiding stats (F4).

    INSERT OR REPLACE, not an ON CONFLICT update: the row is wholly derived
    from the guide logs, so a re-scan should replace it outright — a field
    that no longer has a value must go back to NULL rather than keep a stale
    one. Unknown keys are ignored and missing ones become NULL, so callers
    (`darkroom.guidescan.apply`, the webapi) can pass whatever they have.

    Args:
        db_path: Path to SQLite database file
        guiding: Dictionary with keys matching the session_guiding schema;
            `source_logs` may be a list, which is stored as a JSON array.
    """
    row = {col: guiding.get(col) for col in _GUIDING_COLUMNS}
    if isinstance(row["source_logs"], (list, tuple)):
        row["source_logs"] = json.dumps(list(row["source_logs"]))
    row["computed_at"] = row["computed_at"] or datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    placeholders = ", ".join(f":{col}" for col in _GUIDING_COLUMNS)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO session_guiding ({', '.join(_GUIDING_COLUMNS)}) "
            f"VALUES ({placeholders})",
            row,
        )


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
    now = _now()
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
                    "site_lat": _parse_site_deg(header.get("SITELAT")),
                    "site_lon": _parse_site_deg(header.get("SITELONG")),
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
            # Pick the chronologically-first frame as representative —
            # `frames` is in directory-walk order (filename sort), not
            # capture order (B14).
            first = min(
                frames,
                key=lambda f: parse_date_obs(f.get("date_obs", "")) or datetime.max,
            )

            # Filter: filename-first, header fallback — scoped to this night's frames
            filter_ = None
            for f in frames:
                filter_ = parse_filter(f["filename_stem"])
                if filter_ is not None:
                    break
            if filter_ is None:
                filter_ = first.get("filter_header") or _filter_from_path(lights_path) or None

            focallen = first.get("focallen")
            start_utc, end_utc = compute_session_span(
                (f.get("date_obs", ""), f.get("exposure")) for f in frames
            )

            # M1: the panel can arrive from two places, and they disagree in
            # the normal case. The OBJECT header is whatever was typed at
            # acquisition ("M 8_1-1"), so it carries the panel on a freshly
            # shot mosaic that has not been archived yet; the "P1-1" directory
            # is what `session_dest_rel` wrote and is authoritative once the
            # frames are in the archive. Prefer the folder, fall back to the
            # header, and split the panel off the target either way so the
            # base object name is what gets stored.
            base_target, panel = parse_panel(first["object"] or _target_from_path(lights_path))
            panel = panel_from_dirname(lights_path.name) or panel

            sessions.append({
                "target": _normalize_target(base_target),
                "obs_date": night,
                "ota": parse_ota(focallen, obs_date=night),
                "camera": first["camera"],
                "filter": filter_,
                "panel": panel,
                "gain": first["gain"],
                "temperature_c": first["temperature"],
                "exposure_sec": first["exposure"],
                "focal_length": float(focallen) if focallen is not None else None,
                "frame_count": len(frames),
                "total_integration_sec": int(sum(f["exposure"] for f in frames)),
                "ra_deg": first.get("ra_deg"),
                "dec_deg": first.get("dec_deg"),
                "site_lat": first.get("site_lat"),
                "site_lon": first.get("site_lon"),
                "start_utc": start_utc,
                "end_utc": end_utc,
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

# Dark-vs-FlatDark threshold + reclassification lives in darkroom.parse
# (FLAT_DARK_THRESHOLD_SEC / reclassify_flat_dark) — the single source of
# truth shared with scanner.py and triage/suggest.py.


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
                obs_date = capture_date_of(meta.get("date_obs", ""))

                # ASIAir mixes flat darks and science darks in the same Dark/ folder.
                # Reclassify by exposure: anything under the threshold is a flat dark.
                frame_type = reclassify_flat_dark(frame_type, exposure)

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
                        "ota": parse_ota(meta.get("focallen"), obs_date=obs_date),
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


def _resolve_writer(args, backend, upsert):
    """(upsert callable, label) for a scan command's catalog writes.

    `upsert` is this module's upsert_session or upsert_calibration_set. With
    no backend (the legacy `python -m darkroom.cataloger` path) it is bound to
    `args.db` directly; otherwise the backend's method of the same name is
    used.
    """
    if backend is None:
        db_path = Path(args.db)
        init_db(db_path)
        return (lambda row: upsert(db_path, row)), str(db_path)
    from darkroom.catalog_client import LocalBackend

    label = str(backend.db_path) if isinstance(backend, LocalBackend) else backend.base_url
    return getattr(backend, upsert.__name__), label


def scan_calibration_command(args, *, backend=None):
    calibration_root = Path(args.calibration_path).resolve()
    archive_root = calibration_root.parent

    if not calibration_root.exists():
        print(f"Error: Calibration folder not found: {calibration_root}", file=sys.stderr)
        sys.exit(1)

    _upsert, db_label = _resolve_writer(args, backend, upsert_calibration_set)

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
        _upsert(cal_set)
        print(f"  {cal_set['set_id']}  ({cal_set['frame_count']} frames)")

    print(f"\nDone: {len(cal_sets)} calibration sets cataloged")
    print(f"Catalog: {db_label}")




def scan_all_command(args, *, backend=None):
    """Handle scan-all command (recursive scan of all targets/dates).

    Walks the directory tree from root to find any folder containing FITS files,
    extracts metadata from each group, builds a session record with collision-resistant
    session_id, and writes to SQLite database (or the webapi when a backend is given).

    Handles three coexisting NAS folder structures:
    1. Canonical: Target/Date_Equipment_Filter/Lights/
    2. Partial: Target/Date_Equipment_Filter/  (Lights optional)
    3. Old: Target/Lights - Label/
    """
    root = Path(args.root_path).resolve()
    archive_root = root.parent

    if not root.exists():
        print(f"Error: Root folder not found: {root}", file=sys.stderr)
        sys.exit(1)

    _upsert, db_label = _resolve_writer(args, backend, upsert_session)

    lights_folders = find_lights_folders(root)

    if not lights_folders:
        print("No FITS files found.")
        sys.exit(0)

    print(f"Found {len(lights_folders)} folder(s) containing FITS files")

    added = 0
    skipped = 0
    for lights_path in sorted(lights_folders):
        # Non-recursive: lights_path is already a leaf dir (find_lights_folders
        # only returns directories that directly contain FITS files). Routing
        # through fits_files() also now excludes ASIAir "_thn" thumbnail .fit
        # files from frame_count/total_integration_sec — previously these were
        # hand-rolled without that exclusion (see R5 in BACKLOG.md).
        frame_paths = fits_files(lights_path)
        metadata_list = [FITSHeaderExtractor.extract_metadata(f) for f in frame_paths]
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
            session_id = session_id_for(session)
            session["session_id"] = session_id
            session["lights_path"] = str(lights_path.relative_to(archive_root))
            _upsert(session)
            print(f"  {session_id}  ({session['frame_count']} frames, {session['total_integration_sec']}s)")
            added += 1

    print(f"\nDone: {added} sessions cataloged, {skipped} skipped")
    print(f"Catalog: {db_label}")


def migrate_archive_command(args) -> None:
    """Move sessions from old filter-in-folder layout to new Lights/<filter>/ layout.

    Old: 01_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>_<Filter>/Lights/*.fit
    New: 01_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>/Lights/<Filter>/*.fit
    """
    from darkroom.names import session_dest_rel

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

        # Deliberately NOT routed through parse.fits_files(): this moves every
        # .fit/.fits file out of old_abs (including "_thn" thumbnails) so that
        # old_abs.rmdir() below can succeed. fits_files() excludes thumbnails,
        # which would leave them behind, breaking the clean-sweep removal and
        # spuriously firing the "Could not remove" warning on every migrated
        # session. See R5 in BACKLOG.md — kept as hand-rolled on purpose.
        moved_paths = sorted(
            f for f in old_abs.iterdir()
            if f.is_file() and f.suffix.lower() in (".fit", ".fits")
        )

        if dry_run:
            print(f"  MOVE  {old_abs}")
            print(f"     -> {new_abs}  ({len(moved_paths)} file(s))")
            print(f"        UPDATE lights_path WHERE session_id='{row['session_id']}'")
        else:
            new_abs.mkdir(parents=True, exist_ok=True)
            for f in moved_paths:
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


if __name__ == "__main__":
    main()
