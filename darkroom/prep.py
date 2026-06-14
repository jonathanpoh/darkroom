"""darkroom.prep — Prepare WBPP symlink sessions from archived catalog data."""
from __future__ import annotations

import argparse
import sys
from datetime import date as Date
from itertools import groupby
from pathlib import Path

from darkroom.catalog import (
    find_darks,
    find_flat_darks,
    find_flats,
    query_all_sessions,
    query_sessions,
)
from darkroom.config import resolve_catalog, resolve_path
from darkroom.wbpp import (
    clear_sessions,
    discover_darks,
    discover_flat_darks,
    discover_flat_files,
    discover_lights,
    find_real_files,
    make_symlinks,
    next_session_num,
)


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


# ── prep helpers ─────────────────────────────────────────────────────────────

def _target_slug(target: str) -> str:
    return target.replace(" ", "")


def _overwrite_target_dir(target_dir: Path) -> None:
    """Safely clear SESSION_N dirs in target_dir before regeneration.

    If real (non-symlink) files are found, warns and requires 'yes' confirmation.
    Deletes only SESSION_N subdirs — target_dir itself is preserved.
    """
    real_files = find_real_files(target_dir)
    if real_files:
        print(f"WARNING: Real files found in {target_dir}/ (not symlinks):")
        for f in real_files:
            print(f"  {f}")
        if not sys.stdin.isatty():
            sys.exit("Aborted: real files found and no TTY to confirm deletion.")
        answer = input(
            "\nThese will be permanently deleted. Type 'yes' to continue, or press Enter to abort: "
        ).strip()
        if answer != "yes":
            sys.exit("Aborted.")
    clear_sessions(target_dir)


def _resolve_flat(
    cal_rows: list[dict], filter_name: str, obs_date: str, window_days: int
) -> dict | None:
    """Prompt user to resolve flat set ambiguity. Returns chosen row or None.

    In non-interactive mode (no TTY): auto-selects closest match; skips pause on 0 matches.
    """
    interactive = sys.stdin.isatty()
    if len(cal_rows) == 0:
        print(f"  No flats found for {filter_name} within ±{window_days} day(s) of {obs_date}.")
        if interactive:
            input("  [Enter] Proceed without flats")
        return None
    if len(cal_rows) == 1:
        return cal_rows[0]
    # 2+ matches — prompt or auto-select closest
    print(f"  Multiple flat sets found for {filter_name} near {obs_date}:")
    for i, row in enumerate(cal_rows, 1):
        tag = " ← closest" if i == 1 else ""
        print(f"    {i}) {row['capture_date']} ({row['frame_count']} frames){tag}")
    if not interactive:
        print("  Non-interactive: auto-selecting closest.")
        return cal_rows[0]
    raw = input("  [1]> ").strip()
    idx = int(raw) - 1 if raw.isdigit() else 0
    if 0 <= idx < len(cal_rows):
        return cal_rows[idx]
    return cal_rows[0]


def _build_night(
    sessions: list[dict],
    *,
    output: Path,
    catalog: Path,
    session_dir: Path,
    flat_window: int,
) -> None:
    """Build one SESSION_N directory from one or more sessions on the same night."""
    session_dir.mkdir(parents=True, exist_ok=True)

    # Lights — split by filter
    for sess in sessions:
        lights_src = output / sess["lights_path"]
        filter_name = sess["filter"] or "NoFilter"
        dest = session_dir / "Lights" / f"FILTER_{filter_name}"
        files = discover_lights(lights_src)
        count = make_symlinks(files, dest)
        print(f"  Lights/FILTER_{filter_name}/    {count} symlinks")

    # Darks — camera/gain/exposure from first session (all sessions same night share params)
    s0 = sessions[0]
    dark_rows = find_darks(
        catalog, camera=s0["camera"], gain=s0["gain"], exposure_sec=s0["exposure_sec"]
    )
    dark_count = 0
    for row in dark_rows:
        files = discover_darks(output / row["folder_path"], exposure_sec=s0["exposure_sec"])
        dark_count += make_symlinks(files, session_dir / "Darks")
    if dark_count == 0:
        print("  Darks/                    0 symlinks  [no darks found]")
    else:
        print(f"  Darks/                    {dark_count} symlinks")

    # Flats + FlatDarks — per filter
    for sess in sessions:
        filter_name = sess["filter"] or "NoFilter"
        obs_date = sess["obs_date"]
        flat_rows = find_flats(
            catalog,
            camera=sess["camera"],
            ota=sess["ota"],
            filter_=sess["filter"],
            obs_date=obs_date,
            window_days=flat_window,
        )
        chosen_flat = _resolve_flat(flat_rows, filter_name, obs_date, flat_window)
        if chosen_flat:
            files = discover_flat_files(output / chosen_flat["folder_path"])
            flat_count = make_symlinks(files, session_dir / "Flats" / f"FILTER_{filter_name}")
            print(f"  Flats/FILTER_{filter_name}/      {flat_count} symlinks")

            flat_date = Date.fromisoformat(chosen_flat["capture_date"])
            fd_rows = find_flat_darks(
                catalog,
                camera=sess["camera"],
                flat_exposure_sec=chosen_flat["exposure_sec"],
                flat_capture_date=chosen_flat["capture_date"],
            )
            fd_count = 0
            for fd_row in fd_rows:
                files = discover_flat_darks(
                    output / fd_row["folder_path"], capture_date=flat_date
                )
                fd_count += make_symlinks(files, session_dir / "FlatDarks")
            if fd_count == 0:
                print("  FlatDarks/                0 symlinks  [none found — skipped]")
            else:
                print(f"  FlatDarks/                {fd_count} symlinks")
        else:
            print(f"  Flats/FILTER_{filter_name}/      0 symlinks  [no flats found — skipped]")


def cmd_prep(
    *,
    catalog: Path,
    output: Path,
    wbpp_root: Path,
    target: str | None,
    obs_date: str | None,
    session_id: str | None,
    overwrite: bool = False,
    flat_window: int = 3,
) -> None:
    if session_id:
        rows = query_sessions(catalog, session_id=session_id)
        if not rows:
            sys.exit(f"Session not found: {session_id}")
        target_name = rows[0]["target"]
    elif target:
        rows = query_sessions(catalog, target=target, obs_date=obs_date)
        if not rows:
            date_info = f" on {obs_date}" if obs_date else ""
            sys.exit(f"No sessions found for target '{target}'{date_info}")
        target_name = target
    else:
        sys.exit("Specify --target or --session")

    slug = _target_slug(target_name)
    target_dir = wbpp_root / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    if overwrite:
        _overwrite_target_dir(target_dir)

    # Create Output/ and Output/processed/ so PixInsight can select the dir immediately
    output_dir = target_dir / "Output"
    (output_dir / "processed").mkdir(parents=True, exist_ok=True)

    rows_sorted = sorted(rows, key=lambda r: r["obs_date"])
    for night_date, night_rows in groupby(rows_sorted, key=lambda r: r["obs_date"]):
        night_sessions = list(night_rows)
        n = next_session_num(target_dir)
        session_dir = target_dir / f"SESSION_{n}"
        session_dir.mkdir(parents=True, exist_ok=True)  # create before next iteration reads num
        filters = ", ".join(s["filter"] or "NoFilter" for s in night_sessions)
        total_lights = sum(s["frame_count"] for s in night_sessions)
        print(f"\nSESSION_{n}  ({target_name} · {night_date} · {filters} · {total_lights} lights)")
        _build_night(
            night_sessions,
            output=output,
            catalog=catalog,
            session_dir=session_dir,
            flat_window=flat_window,
        )
        print(f"\nIn PixInsight: WBPP → Add Directory → select {session_dir}/")

    print(f"\nSet WBPP output directory to: {output_dir}/")


# ── argument parsing ──────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    """Entry point invoked by darkroom.cli."""
    catalog = resolve_catalog(args.catalog)

    if args.list:
        cmd_list(catalog, args.target)
        return

    if not args.target and not args.session:
        sys.exit("Error: specify --target or --session (or --list to browse)")

    output = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if output is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")

    wbpp_root = resolve_path(args.wbpp, "DARKROOM_WBPP", "wbpp_path") or Path("./WBPP")

    cmd_prep(
        catalog=catalog,
        output=output,
        wbpp_root=wbpp_root,
        target=args.target,
        obs_date=args.date,
        session_id=args.session,
        overwrite=args.overwrite,
        flat_window=args.flat_window,
    )


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "wbpp",
        help="Prepare a WBPP symlink session from the catalog",
        description="Build SESSION_N symlink dirs under <wbpp>/<target>/ for PixInsight WBPP.",
    )
    p.add_argument("--list", action="store_true", help="List sessions from catalog")
    p.add_argument("--target", metavar="NAME", help='Target name (e.g. "M 81")')
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Restrict --target to one night")
    p.add_argument("--session", metavar="ID", help="Select single session by catalog ID")
    p.add_argument("--overwrite", action="store_true",
                   help="Clear and regenerate target WBPP dir before creating symlinks")
    p.add_argument("--flat-window", type=int, default=3, metavar="DAYS",
                   help="Match flats within ±DAYS of the session date (default: 3)")
    p.add_argument("--archive", metavar="PATH",
                   help="Archive root (env: DARKROOM_ARCHIVE)")
    p.add_argument("--catalog", metavar="PATH",
                   help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)")
    p.add_argument("--wbpp", metavar="PATH",
                   help="Root for WBPP output dirs (env: DARKROOM_WBPP, default: ./WBPP)")
    p.set_defaults(func=run)
