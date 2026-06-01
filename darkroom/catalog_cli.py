"""darkroom.catalog_cli — argparse wiring for `darkroom catalog ...` subcommands."""
from __future__ import annotations

import argparse
import sys
from itertools import groupby

from darkroom.catalog import query_all_sessions, query_sessions
from darkroom.cataloger import (
    mark_processed_command,
    migrate_archive_command,
    scan_all_command,
    scan_calibration_command,
)
from darkroom.config import resolve_catalog


def _resolve_db(args: argparse.Namespace) -> None:
    """Resolve args.db via CLI/env/toml/default; mutate args.db to the resolved string."""
    args.db = str(resolve_catalog(args.db))


def _list_run(args: argparse.Namespace) -> None:
    _resolve_db(args)
    rows = (
        query_sessions(args.db, target=args.target)
        if args.target
        else query_all_sessions(args.db)
    )
    if not rows:
        print("No sessions found.")
        return
    for tgt, group in groupby(rows, key=lambda r: r["target"]):
        print(f"\n{tgt}")
        for row in group:
            hrs = (row["total_integration_sec"] or 0) / 3600
            status = row.get("processed_status") or ""
            tag = f"  [{status}]" if status else ""
            print(
                f"  {row['obs_date']}  {row['session_id']}"
                f"  {row['frame_count']} frames  {hrs:.1f}h{tag}"
            )


def _scan_lights_run(args: argparse.Namespace) -> None:
    _resolve_db(args)
    scan_all_command(args)


def _scan_calibration_run(args: argparse.Namespace) -> None:
    _resolve_db(args)
    scan_calibration_command(args)


def _mark_run(args: argparse.Namespace) -> None:
    _resolve_db(args)
    mark_processed_command(args)


def _migrate_run(args: argparse.Namespace) -> None:
    _resolve_db(args)
    migrate_archive_command(args)


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "catalog",
        help="Browse and update the astro catalog",
    )
    p.add_argument(
        "--db",
        metavar="PATH",
        help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)",
    )
    sub = p.add_subparsers(dest="catcmd", required=True)

    sl = sub.add_parser("scan-lights", help="Recursively catalog all light sessions")
    sl.add_argument("root_path", help="Root folder to scan (e.g. '04_Deep Sky Objects')")
    sl.set_defaults(func=_scan_lights_run)

    sc = sub.add_parser("scan-calibration", help="Catalog calibration frames")
    sc.add_argument("calibration_path", help="Root folder to scan (e.g. '00_Calibration')")
    sc.set_defaults(func=_scan_calibration_run)

    m = sub.add_parser("mark", help="Update processed_status for one session")
    m.add_argument("session_id", help="Session ID")
    m.add_argument("status", help="Status string (date, path, or note)")
    m.set_defaults(func=_mark_run)

    ls = sub.add_parser("list", help="List sessions from the catalog")
    ls.add_argument("--target", metavar="NAME", help="Filter by target")
    ls.set_defaults(func=_list_run)

    mig = sub.add_parser(
        "migrate-archive",
        help="Migrate archive from old filter-in-folder layout to Lights/<filter>/ layout",
    )
    mig.add_argument("--archive", required=True, metavar="PATH", help="Archive root directory")
    mig.add_argument("--dry-run", action="store_true", help="Print moves without executing")
    mig.set_defaults(func=_migrate_run)
