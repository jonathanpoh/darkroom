#!/usr/bin/env python3
"""wbpp_prep.py — Prepare WBPP symlink sessions from archived catalog data."""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from itertools import groupby
from pathlib import Path

from darkroom.catalog import query_all_sessions, query_sessions


# ── config resolution ─────────────────────────────────────────────────────────

def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _find_toml() -> dict:
    for candidate in [Path("darkroom.toml"), Path.home() / ".config" / "darkroom" / "darkroom.toml"]:
        cfg = _load_toml(candidate)
        if cfg:
            return cfg
    return {}


def resolve_path(flag_val: str | None, env_var: str, toml_key: str) -> Path | None:
    """Resolve a path: CLI flag → env var → toml."""
    if flag_val:
        return Path(flag_val)
    env = os.environ.get(env_var)
    if env:
        return Path(env)
    cfg = _find_toml()
    if toml_key in cfg:
        return Path(cfg[toml_key])
    return None


# ── --list ────────────────────────────────────────────────────────────────────

def cmd_list(catalog: Path, target: str | None) -> None:
    if target:
        rows = query_sessions(catalog, target=target)
    else:
        rows = query_all_sessions(catalog)

    if not rows:
        print("No sessions found.")
        return

    for tgt, group in groupby(rows, key=lambda r: r["target"]):
        print(f"\n{tgt}")
        for row in group:
            total_h = (row["total_integration_sec"] or 0) / 3600
            print(
                f"  {row['obs_date']}  {row['session_id']}"
                f"  {row['frame_count']} frames  {total_h:.1f}h"
            )


# ── argument parsing ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Prepare WBPP symlink sessions from the astro catalog."
    )
    p.add_argument("--list", action="store_true", help="List sessions from catalog")
    p.add_argument("--target", metavar="NAME", help='Target name (e.g. "M 81")')
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Restrict --target to one night")
    p.add_argument("--session", metavar="ID", help="Select single session by catalog ID")
    p.add_argument("--overwrite", action="store_true",
                   help="Clear and regenerate target WBPP dir before creating symlinks")
    p.add_argument("--output", metavar="PATH",
                   help="Archive root (lights and cal paths resolve here)")
    p.add_argument("--catalog", metavar="PATH", help="Path to astro_catalog.db")
    p.add_argument("--wbpp", metavar="PATH", default="./WBPP",
                   help="Root for WBPP output dirs (default: ./WBPP)")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    catalog = resolve_path(args.catalog, "DARKROOM_CATALOG", "catalog_path")
    if catalog is None:
        sys.exit("Error: --catalog / DARKROOM_CATALOG / darkroom.toml catalog_path required")

    if args.list:
        cmd_list(catalog, args.target)
        return

    if not args.target and not args.session:
        parser.print_help()
        sys.exit(1)

    output = resolve_path(args.output, "DARKROOM_OUTPUT", "output_path")
    if output is None:
        sys.exit("Error: --output / DARKROOM_OUTPUT / darkroom.toml output_path required")

    wbpp_root = Path(args.wbpp)

    # Implemented in Task 4
    sys.exit("Prep mode not yet implemented")


if __name__ == "__main__":
    main()
