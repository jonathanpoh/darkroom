#!/usr/bin/env python3
"""archive_ingest.py — Copy a completed ASIAir session into canonical archive structure."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fits_cataloger import (
    init_db,
    make_session_id,
    upsert_calibration_set,
    upsert_session,
)

from darkroom.scanner import CalibrationGroup, Session, ScanResult, scan_source


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load darkroom.toml from project dir or ~/.config/darkroom/."""
    for p in [
        Path("darkroom.toml"),
        Path.home() / ".config" / "darkroom" / "darkroom.toml",
    ]:
        if p.exists():
            with open(p, "rb") as f:
                return tomllib.load(f)
    return {}


def resolve_path(
    cli_val: str | None,
    env_key: str,
    config: dict,
    config_key: str,
    label: str,
) -> Path:
    """Resolve a path from CLI → env var → config, exit with error if missing."""
    val = cli_val or os.environ.get(env_key) or config.get("darkroom", {}).get(config_key)
    if not val:
        print(
            f"Error: {label} path required. Use --{label}, {env_key} env var, "
            f"or set {config_key} in darkroom.toml",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(val)


# ---------------------------------------------------------------------------
# Destination path helpers
# ---------------------------------------------------------------------------

def camera_slug(camera: str) -> str:
    """Strip spaces from camera name for use in folder names."""
    return re.sub(r"\s+", "", camera)


def session_dest_rel(
    target: str, obs_date: str, ota: str, camera: str, filter_: str | None
) -> Path:
    """Return relative destination path for a session's Lights/ folder."""
    f = filter_ or "NoFilter"
    folder = f"{obs_date}_{ota}_{camera_slug(camera)}_{f}"
    return Path("04_Deep Sky Objects") / target / folder / "Lights"


def cal_dest_rel(
    frame_type: str, camera: str, ota: str, filter_: str | None, capture_date: str
) -> Path:
    """Return relative destination path for a calibration group's folder."""
    slug = camera_slug(camera)
    if frame_type == "Flat":
        f = filter_ or "NoFilter"
        return Path("00_Calibration") / "Flats" / f"{ota}_{slug}_{f}" / capture_date
    if frame_type == "Dark":
        return Path("00_Calibration") / "Darks" / slug
    if frame_type == "FlatDark":
        return Path("00_Calibration") / "FlatDarks" / slug
    if frame_type == "Bias":
        return Path("00_Calibration") / "Bias" / slug / "Raw"
    raise ValueError(f"Unknown frame type: {frame_type}")


# ---------------------------------------------------------------------------
# Filter prompt
# ---------------------------------------------------------------------------

KNOWN_FILTERS = ["L-Pro", "L-Extreme", "AstronomikL2", "BaaderNeodymium", "OmegonHelievo"]


def resolve_filter(
    detected: str | None,
    interactive: bool,
    context: str = "",
) -> tuple[str, bool]:
    """Return (filter_str, needs_review).

    If filter is already detected, returns it directly. If missing and interactive,
    prompts the user. If missing and non-interactive, returns ('NoFilter', True).
    """
    if detected is not None:
        return detected, False

    if not interactive:
        return "NoFilter", True

    if context:
        print(f"\nNo filter detected for: {context}")
    else:
        print("\nNo filter detected.")

    for i, f in enumerate(KNOWN_FILTERS, 1):
        print(f"  {i}) {f}")
    print(f"  {len(KNOWN_FILTERS) + 1}) Enter manually")
    print("  [Enter] NoFilter")

    while True:
        try:
            raw = input("> ").strip()
            if not raw:
                return "NoFilter", False
            n = int(raw)
            if 1 <= n <= len(KNOWN_FILTERS):
                return KNOWN_FILTERS[n - 1], False
            if n == len(KNOWN_FILTERS) + 1:
                manual = input("Filter name: ").strip()
                return (manual or "NoFilter"), False
        except ValueError:
            print("Please enter a number.")
        except EOFError:
            return "NoFilter", False


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def existing_catalog_sessions(catalog_path: Path) -> dict[str, int]:
    """Return {session_id: frame_count} for all sessions in the catalog."""
    if not catalog_path.exists():
        return {}
    with sqlite3.connect(catalog_path) as conn:
        rows = conn.execute("SELECT session_id, frame_count FROM sessions").fetchall()
    return {r[0]: r[1] for r in rows}


def make_cal_set_id(
    frame_type: str,
    camera: str,
    gain: int,
    exposure_sec: float,
    temperature_c: float,
    capture_date: str,
) -> str:
    """Build a calibration set primary key."""
    slug = camera_slug(camera)
    temp_str = f"{int(temperature_c)}C"
    return f"{frame_type}_{slug}_{exposure_sec:.3g}s_{gain}g_{temp_str}_{capture_date}"


# ---------------------------------------------------------------------------
# Manifest entry builders
# ---------------------------------------------------------------------------

def build_session_entry(
    session: Session,
    output: Path,
    catalog_sessions: dict[str, int],
    interactive: bool,
) -> dict:
    """Build one sessions[] manifest entry for the given Session."""
    filter_, needs_review = resolve_filter(
        session.filter,
        interactive=interactive,
        context=f"{session.target} on {session.obs_date}",
    )

    # Pass None for filter when unknown so make_session_id uses "UnknownFilter"
    session_id = make_session_id(
        session.target,
        session.obs_date,
        session.ota,
        session.camera,
        None if needs_review else filter_,
    )
    dest_rel = session_dest_rel(
        session.target, session.obs_date, session.ota, session.camera,
        None if needs_review else filter_,
    )
    dest_abs = output / dest_rel

    existing = catalog_sessions.get(session_id)
    if existing is None:
        status = "new"
        file_entries = [
            {"src": str(f), "dst": str(dest_rel / f.name), "copy": True}
            for f in sorted(session.files)
        ]
    elif existing == len(session.files):
        status = "existing"
        file_entries = []
    else:
        status = "topup"
        existing_names = (
            {p.name for p in dest_abs.iterdir() if p.is_file()}
            if dest_abs.exists()
            else set()
        )
        file_entries = [
            {"src": str(f), "dst": str(dest_rel / f.name), "copy": True}
            for f in sorted(session.files)
            if f.name not in existing_names
        ]

    return {
        "session_id": session_id,
        "target": session.target,
        "obs_date": session.obs_date,
        "ota": session.ota,
        "camera": session.camera,
        "filter": None if needs_review else filter_,
        "gain": session.gain,
        "temperature_c": session.temperature_c,
        "exposure_sec": session.exposure_sec,
        "frame_count": len(session.files),
        "ra_deg": session.ra_deg,
        "dec_deg": session.dec_deg,
        "needs_review": needs_review,
        "status": status,
        "lights_rel_path": str(dest_rel),
        "files": file_entries,
    }


def build_cal_entry(
    group: CalibrationGroup,
    output: Path,
    interactive: bool,
) -> dict:
    """Build one calibration[] manifest entry for the given CalibrationGroup."""
    # Filter resolution only matters for Flat/FlatDark
    if group.frame_type in ("Flat", "FlatDark"):
        filter_, needs_review = resolve_filter(
            group.filter,
            interactive=interactive,
            context=f"{group.frame_type} on {group.capture_date}",
        )
    else:
        filter_ = group.filter
        needs_review = False

    set_id = make_cal_set_id(
        group.frame_type, group.camera, group.gain,
        group.exposure_sec, group.temperature_c, group.capture_date,
    )
    dest_rel = cal_dest_rel(
        group.frame_type, group.camera, group.ota, filter_, group.capture_date
    )
    dest_abs = output / dest_rel

    file_entries = []
    for f in sorted(group.files):
        dest_file = dest_abs / f.name
        file_entries.append({
            "src": str(f),
            "dst": str(dest_rel / f.name),
            "copy": not dest_file.exists(),
        })

    return {
        "set_id": set_id,
        "frame_type": group.frame_type,
        "camera": group.camera,
        "ota": group.ota,
        "filter": None if needs_review else filter_,
        "gain": group.gain,
        "exposure_sec": group.exposure_sec,
        "temperature_c": group.temperature_c,
        "capture_date": group.capture_date,
        "frame_count": len(group.files),
        "needs_review": needs_review,
        "folder_rel_path": str(dest_rel),
        "files": file_entries,
    }


# ---------------------------------------------------------------------------
# Placeholders for later tasks
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    config = load_config()
    print("archive_ingest: not fully implemented yet")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive ASIAir session to canonical folder structure."
    )
    parser.add_argument("--source", required=False, metavar="PATH")
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--catalog", metavar="PATH")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--manifest", metavar="FILE")
    mode.add_argument("--review", metavar="FILE")
    mode.add_argument("--commit", nargs="?", const=True, metavar="FILE")
    return parser


if __name__ == "__main__":
    main()
