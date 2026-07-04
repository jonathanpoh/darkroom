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
    args.db = str(resolve_catalog(args.catalog))


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
            state = row.get("processed_state") or "unprocessed"
            if state == "unprocessed":
                tag = ""
            else:
                detail = row.get("processed_date") or row.get("processed_path") or ""
                tag = f"  [{state}{': ' + detail if detail else ''}]"
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
    sub = p.add_subparsers(dest="catcmd", required=True)

    # Shared --catalog flag, added to every subcommand so its position is
    # consistent with the rest of the CLI (after the subcommand, not before).
    catalog_flag = argparse.ArgumentParser(add_help=False)
    catalog_flag.add_argument(
        "--catalog",
        metavar="PATH",
        help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)",
    )

    sl = sub.add_parser("scan-lights", parents=[catalog_flag],
                        help="Recursively catalog all light sessions")
    sl.add_argument("root_path", help="Root folder to scan (e.g. '01_Deep Sky Objects')")
    sl.set_defaults(func=_scan_lights_run)

    sc = sub.add_parser("scan-calibration", parents=[catalog_flag],
                        help="Catalog calibration frames")
    sc.add_argument("calibration_path", help="Root folder to scan (e.g. '00_Calibration')")
    sc.set_defaults(func=_scan_calibration_run)

    m = sub.add_parser(
        "mark", parents=[catalog_flag],
        help="Set structured processed_state for one session",
        description="Set a session's structured processed_state. `darkroom finish` "
                    "auto-sets state='processed' with the _Processed/<date>/ path and "
                    "date it wrote. Set it by hand to mark a session unprocessed, "
                    "processed, or skipped, optionally attaching a date, an output "
                    "path, or a note.",
    )
    m.add_argument("session_id", help="Session ID (see `catalog list`)")
    m.add_argument("state", choices=["unprocessed", "processed", "skipped"],
                   help="New processed_state")
    m.add_argument("--date", metavar="YYYY-MM-DD", help="processed_date")
    m.add_argument("--path", metavar="PATH", help="processed_path (archive-relative _Processed path)")
    m.add_argument("--notes", metavar="TEXT",
                   help="Notes (only overwrites existing notes when passed)")
    m.set_defaults(func=_mark_run)

    ls = sub.add_parser("list", parents=[catalog_flag],
                        help="List sessions from the catalog")
    ls.add_argument("--target", metavar="NAME", help="Filter by target")
    ls.set_defaults(func=_list_run)

    mig = sub.add_parser(
        "migrate-archive", parents=[catalog_flag],
        help="Migrate archive from old filter-in-folder layout to Lights/<filter>/ layout",
    )
    mig.add_argument("--archive", required=True, metavar="PATH", help="Archive root directory")
    mig.add_argument("--dry-run", action="store_true", help="Print moves without executing")
    mig.set_defaults(func=_migrate_run)
