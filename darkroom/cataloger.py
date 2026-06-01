#!/usr/bin/env python3
"""
FITS Astrophotography Session Catalog Tool

Scans FITS files and catalogs sessions into SQLite for browsing via Datasette.
Two commands for ingestion:
  scan-all         — recursively catalog all light sessions
  scan-calibration — catalog calibration frames (darks, flats, bias)
Use: datasette serve astro_catalog.db
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time


def _parse_coords(ra, dec) -> tuple[float | None, float | None]:
    """Return (ra_deg, dec_deg) from FITS header values, or (None, None).

    ASIAir typically writes RA/DEC as float degrees. Older or different rigs
    may write sexagesimal strings ("09 55 33", "+69 03 55"). Handles both.
    """
    if ra is None or dec is None:
        return None, None
    try:
        return float(ra), float(dec)
    except (TypeError, ValueError):
        try:
            c = SkyCoord(ra=str(ra), dec=str(dec), unit=(u.hourangle, u.deg))
            return c.ra.deg, c.dec.deg
        except Exception:
            return None, None


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
# Filter and OTA parsing (filename-first, FITS header fallback)
# ============================================================================

_TEMP_RE = re.compile(r"^-?\d+\.?\d*C$")


def parse_filter(stem: str) -> str | None:
    """Return filter from filename stem, or None if not present.

    ASIAir cameras don't write FILTER to FITS headers. The filter name
    appears in the filename stem at parts[-2], unless that position holds
    a temperature reading (e.g. "-20C").

    Args:
        stem: Filename stem (without extension), e.g.
              "Light_M 81_180.0s_Bin1_0C_20260219_L-Pro_0186"

    Returns:
        Filter name (normalized: "LExtreme" → "L-Extreme"), or None if
        no filter found or parts[-2] matches temperature regex.
    """
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    candidate = parts[-2]
    if _TEMP_RE.match(candidate):
        return None
    return "L-Extreme" if candidate == "LExtreme" else candidate


def parse_ota(focallen) -> str:
    """Infer OTA name from FOCALLEN header value or input.

    Args:
        focallen: FITS header FOCALLEN value (int, str, or None).
                  Common values: 180 (FMA180), 400 (FRA400).

    Returns:
        OTA abbreviation: "FMA180", "FRA400", or "Unknown".
    """
    try:
        fl = int(focallen)
    except (TypeError, ValueError):
        return "Unknown"
    # Tolerance windows — ASIAir reports measured focal length, not nominal
    # (e.g. FRA400 reports 402).
    if 170 <= fl <= 190:
        return "FMA180"
    if 390 <= fl <= 410:
        return "FRA400"
    return "Unknown"


def make_session_id(target: str, obs_date: str, ota: str, camera: str, filter_: str | None) -> str:
    """Build collision-resistant session primary key.

    Removes spaces from target and camera, strips dashes from date, and uses
    "UnknownFilter" when filter detection failed (signals needs-review, distinct
    from a session deliberately shot bare).

    Args:
        target: Target name (e.g. "M 81", "NGC 7380")
        obs_date: Observation date in YYYY-MM-DD format
        ota: OTA abbreviation (e.g. "FRA400", "FMA180")
        camera: Camera model (e.g. "ASI585MC", "Canon6D")
        filter_: Filter name (e.g. "L-Pro", "L-Extreme"), or None/empty string

    Returns:
        Session ID: {TargetSlug}_{YYYYMMDD}_{OTA}_{Camera}_{Filter}
        (e.g. "M81_20260219_FRA400_ASI585MC_L-Pro")
    """
    slug = re.sub(r"\s+", "", target)
    camera_slug = re.sub(r"\s+", "", camera)
    date = obs_date.replace("-", "")
    # "UnknownFilter" means parse failed AND no FITS FILTER header — needs manual review.
    # A session legitimately shot bare would need to be flagged explicitly (future work).
    f = filter_ or "UnknownFilter"
    return f"{slug}_{date}_{ota}_{camera_slug}_{f}"


_CALIB_FOLDER_NAMES = frozenset({"flats", "darks", "bias", "flatdarks", "flat darks"})


_DSLR_RE = re.compile(r"canon|nikon|sony|pentax|fuji", re.IGNORECASE)


def _format_gain(camera: str, gain: int) -> str:
    """Return 'ISO1600' for DSLRs or '200g' for astro cameras."""
    if _DSLR_RE.search(camera):
        return "ISOAuto" if gain == 0 else f"ISO{gain}"
    return f"{gain}g"


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

    Looks for the component immediately after '04_Deep Sky Objects'. Falls back
    to the grandparent of the lights folder (Target/Date/Lights → Target).
    """
    parts = lights_path.parts
    for i, part in enumerate(parts):
        if part == "04_Deep Sky Objects" and i + 1 < len(parts):
            return parts[i + 1]
    if len(parts) >= 3:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""


def _filter_from_path(lights_path: Path) -> str | None:
    """Extract filter from the session folder name.

    Session folders follow YYYY-MM-DD_{OTA}_{Camera}_{Filter}. Returns the
    last underscore-delimited component if there are at least 4 parts (date +
    OTA + camera + filter). Returns None if the folder doesn't match.
    """
    folder = lights_path.parent.name
    parts = folder.split("_")
    if len(parts) >= 4:
        return parts[-1]
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
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        # In-place modification prevents os.walk from descending into @eaDir
        dirnames[:] = [d for d in dirnames if d != "@eaDir"]
        if Path(dirpath).name.lower() in _CALIB_FOLDER_NAMES:
            continue
        if any(f.lower().endswith((".fit", ".fits")) for f in filenames):
            result.append(Path(dirpath))
    return result




# ============================================================================
# SQLite Catalog Functions
# ============================================================================


def init_db(db_path: Path) -> None:
    """Initialize SQLite database with sessions and calibration_sets tables.

    Creates the database if it doesn't exist, and creates tables with
    idempotent IF NOT EXISTS clauses.

    Args:
        db_path: Path to SQLite database file
    """
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id              TEXT PRIMARY KEY,
                target                  TEXT NOT NULL,
                obs_date                TEXT NOT NULL,
                ota                     TEXT,
                camera                  TEXT,
                filter                  TEXT,
                gain                    INTEGER,
                temperature_c           REAL,
                exposure_sec            REAL,
                frame_count             INTEGER,
                total_integration_sec   INTEGER,
                total_integration_hours REAL GENERATED ALWAYS AS (total_integration_sec / 3600.0) VIRTUAL,
                ra_deg                  REAL,
                dec_deg                 REAL,
                lights_path             TEXT,
                processed_status        TEXT,
                notes                   TEXT
            );
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
                folder_path   TEXT
            );
        """)


def _normalize_camera(name):
    """Strip whitespace from a camera name. Idempotent; safe on None."""
    return None if name is None else re.sub(r"\s+", "", name)


def _round_exposure(x):
    """Round an exposure value to 4 decimals. Safe on None."""
    return None if x is None else round(float(x), 4)


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
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, target, obs_date, ota, camera, filter,
                gain, temperature_c, exposure_sec, frame_count,
                total_integration_sec, ra_deg, dec_deg,
                lights_path, processed_status, notes
            ) VALUES (
                :session_id, :target, :obs_date, :ota, :camera, :filter,
                :gain, :temperature_c, :exposure_sec, :frame_count,
                :total_integration_sec, :ra_deg, :dec_deg,
                :lights_path, :processed_status, :notes
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
                frame_count           = excluded.frame_count,
                total_integration_sec = excluded.total_integration_sec,
                ra_deg                = excluded.ra_deg,
                dec_deg               = excluded.dec_deg,
                lights_path           = excluded.lights_path,
                notes                 = excluded.notes
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
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO calibration_sets (
                set_id, frame_type, camera, ota, filter,
                gain, exposure_sec, temperature_c, frame_count,
                capture_date, folder_path
            ) VALUES (
                :set_id, :frame_type, :camera, :ota, :filter,
                :gain, :exposure_sec, :temperature_c, :frame_count,
                :capture_date, :folder_path
            )
            ON CONFLICT(set_id) DO UPDATE SET
                frame_count  = excluded.frame_count,
                capture_date = excluded.capture_date,
                folder_path  = excluded.folder_path
            """,
            cal_set,
        )


def mark_processed(db_path: Path, session_id: str, status: str) -> bool:
    """Update processed_status for a session. Returns True if session was found.

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


def mark_processed_command(args):
    """Handle mark-processed command."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    found = mark_processed(db_path, args.session_id, args.status)
    if not found:
        print(f"Error: Session not found: {args.session_id}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated: {args.session_id} → processed_status = {args.status!r}")


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
    """Update processed_status for all sessions matching target (case-insensitive).

    Args:
        db_path: Path to SQLite database file.
        target: Target name to match (e.g. "M 81"). Case-insensitive.
        status: New processed_status value (e.g. "2026-05-15").

    Returns:
        Number of rows updated.
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET processed_status = ? WHERE target = ? COLLATE NOCASE",
            (status, target),
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

    if args.date:
        date_str = args.date
    else:
        processed_root = (
            Path(args.archive) / "04_Deep Sky Objects" / args.target / "_Processed"
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
        count = mark_processed_by_target(db_path, args.target, date_str)
        if count == 0:
            print(
                f"Warning: no sessions found for target {args.target!r}",
                file=sys.stderr,
            )
        else:
            print(
                f"Updated {count} session(s) for target {args.target!r}"
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
                filter_ = first.get("filter_header") or _filter_from_path(lights_path) or ""

            sessions.append({
                "target": first["object"] or _target_from_path(lights_path),
                "obs_date": night,
                "ota": parse_ota(first.get("focallen")),
                "camera": first["camera"],
                "filter": filter_,
                "gain": first["gain"],
                "temperature_c": first["temperature"],
                "exposure_sec": first["exposure"],
                "frame_count": len(frames),
                "total_integration_sec": int(sum(f["exposure"] for f in frames)),
                "ra_deg": first.get("ra_deg"),
                "dec_deg": first.get("dec_deg"),
                "lights_path": str(lights_path),
                "processed_status": "",
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


class CalibrationCataloger:
    @staticmethod
    def scan(calibration_root: Path) -> list[dict]:
        """Recursively find and group calibration FITS files."""
        groups: dict[tuple, dict] = {}

        for dirpath, dirnames, filenames in os.walk(calibration_root):
            dirnames[:] = [d for d in dirnames if d != "@eaDir"]
            for fname in filenames:
                if not fname.lower().endswith((".fit", ".fits")):
                    continue
                fits_path = Path(dirpath) / fname
                meta = FITSHeaderExtractor.extract_metadata(fits_path)
                if not meta:
                    continue

                frame_type = _infer_frame_type(fits_path, meta.get("imagetyp"))
                camera = _normalize_camera(meta["camera"])
                gain = meta["gain"]
                exposure = _round_exposure(meta["exposure"])
                temp = round(meta["temperature"])
                folder = str(fits_path.parent)
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
            camera_slug = re.sub(r"\s+", "", group["camera"])
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
            })

        return cal_sets


def scan_calibration_command(args):
    calibration_root = Path(args.calibration_path)
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
    root = Path(args.root_path)
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
            upsert_session(db_path, session)
            print(f"  {session_id}  ({session['frame_count']} frames, {session['total_integration_sec']}s)")
            added += 1

    print(f"\nDone: {added} sessions cataloged, {skipped} skipped")
    print(f"Database: {db_path}")
    print(f"Browse: datasette serve {db_path}")


def main():
    parser = argparse.ArgumentParser(
        description="FITS astrophotography session cataloger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initial scan of all sessions
  %(prog)s scan-all "/Volumes/Astrophotography/04_Deep Sky Objects"

  # Scan calibration frames
  %(prog)s scan-calibration /Volumes/Astrophotography/00_Calibration

  # Mark a session as processed
  %(prog)s mark-processed M81_20260219_FRA400_ASI585MC_L-Pro "2026-03-01 /path/to/output"

  # Mark all sessions for a target as processed (date auto-detected from archive)
  %(prog)s finish --target "M 81" --archive /Volumes/Astrophotography

  # Mark specific sessions only
  %(prog)s finish --target "M 81" --archive /Volumes/Astrophotography \\
      --session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro

  # Fallback when _Processed/ is already cleaned up
  %(prog)s finish --target "M 81" --date 2026-05-15

  # Browse the catalog (run separately)
  datasette serve astro_catalog.db
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
    p_all.add_argument("root_path", help="Root folder to scan (e.g. '04_Deep Sky Objects')")

    # scan-calibration
    p_cal = subparsers.add_parser("scan-calibration", help="Catalog calibration frames")
    p_cal.add_argument("calibration_path", help="Path to calibration folder (e.g. 00_Calibration)")

    # mark-processed
    p_mark = subparsers.add_parser("mark-processed", help="Update processed_status for a session")
    p_mark.add_argument("session_id", help="Session ID (e.g. M81_20260219_FRA400_ASI585MC_L-Pro)")
    p_mark.add_argument("status", help="Status string (date, path, or note)")

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
            "<archive>/04_Deep Sky Objects/<target>/_Processed/ to detect the date "
            "(targets outside 04_Deep Sky Objects/ should use --date instead)"
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
