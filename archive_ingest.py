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
