"""darkroom.finish — Copy WBPP stacks back to the NAS archive and clean up working dirs."""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import shutil
from datetime import datetime
from pathlib import Path

from darkroom.cataloger import set_processed_state
from darkroom.config import resolve_catalog, resolve_path


def _target_slug(target: str) -> str:
    return target.replace(" ", "")


# ── core helpers ──────────────────────────────────────────────────────────────

def _find_processing_date(
    master_dir: Path, processed_dir: Path, override: str | None
) -> str:
    """Return YYYY-MM-DD for the _Processed/<date>/ folder name.

    If override is given (--date), use it verbatim. Otherwise return the
    latest mtime across files in master/ and processed/ — captures the
    most recent processing activity, whether WBPP-only or with hand finishing.
    """
    if override:
        return override
    times: list[float] = []
    for d in (master_dir, processed_dir):
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    times.append(f.stat().st_mtime)
    if not times:
        sys.exit(f"No files in {master_dir} or {processed_dir} — cannot derive date")
    return datetime.fromtimestamp(max(times)).date().isoformat()


def _build_dest(output: Path, target: str, date_str: str) -> Path:
    """Return <output>/01_Deep Sky Objects/<target>/_Processed/<date_str>."""
    return output / "01_Deep Sky Objects" / target / "_Processed" / date_str


def _collect_light_dirs(wbpp_target: Path) -> set[Path]:
    """Return the resolved archive directories the Lights symlinks point into.

    Each SESSION_N/Lights/** symlink resolves to a light frame inside the
    archive; its parent directory is the session's stored ``lights_path``. We
    compare those directories — not a fixed number of ``.parent`` hops — so the
    match is agnostic to how many components sit between the archive root and the
    frames (e.g. the ``Lights/<filter>/`` split added when sessions were split by
    filter).
    """
    dirs: set[Path] = set()
    for session_dir in wbpp_target.glob("SESSION_*"):
        if not session_dir.is_dir():
            continue
        for symlink in (session_dir / "Lights").rglob("*"):
            if not symlink.is_symlink():
                continue
            try:
                resolved = symlink.resolve(strict=True)
            except FileNotFoundError:
                continue
            dirs.add(resolved.parent)
    return dirs


def _resolve_session_ids(
    wbpp_target: Path, catalog: Path, archive_root: Path
) -> list[str]:
    """Look up catalog session_ids for the lights symlinked under wbpp_target.

    Matches each Lights symlink's resolved archive directory against the
    catalog's stored ``lights_path`` (resolved under ``archive_root``).
    """
    light_dirs = _collect_light_dirs(wbpp_target)
    if not light_dirs:
        return []
    ids: list[str] = []
    with sqlite3.connect(catalog) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT session_id, lights_path FROM sessions WHERE lights_path IS NOT NULL"
        ).fetchall()
    for row in rows:
        if (archive_root / row["lights_path"]).resolve() in light_dirs:
            ids.append(row["session_id"])
    return sorted(set(ids))


def _mark_sessions_processed(
    wbpp_target: Path, catalog: Path, archive_root: Path, status: str, date_str: str
) -> None:
    """Mark every session resolved from wbpp_target as processed.

    Sets the structured processed_state='processed', with processed_path=status
    (the archive-relative _Processed/<date>/ path) and processed_date=date_str.
    """
    session_ids = _resolve_session_ids(wbpp_target, catalog, archive_root)
    if not session_ids:
        print("\nWarning: no catalog sessions matched symlinks — nothing to mark.")
        return
    print(f"\nMarking {len(session_ids)} session(s) as processed:")
    for sid in session_ids:
        ok = set_processed_state(
            catalog, sid, state="processed", processed_path=status, processed_date=date_str
        )
        mark = "✓" if ok else "✗ (not found)"
        print(f"  {mark} {sid}")


def _copy_flat(src_dir: Path, dest_dir: Path, *, dry_run: bool) -> int:
    """Copy all files from src_dir into dest_dir (flat, no subdirs). Returns count copied."""
    files = sorted(f for f in src_dir.iterdir() if f.is_file())
    if not files:
        return 0
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in files:
        dest = dest_dir / f.name
        if dest.exists() and not dest.is_file():
            sys.exit(f"Collision: {dest} exists but is not a file — aborting")
        if dry_run:
            if dest.exists():
                print(f"  [dry-run] skip (exists): {f.name}")
            else:
                print(f"  [dry-run] {f} → {dest}")
                count += 1
        else:
            if dest.exists():
                print(f"  skip (exists): {f.name}")
            else:
                shutil.copy2(f, dest)
                print(f"  {f.name} → {dest}")
                count += 1
    return count


# ── cleanup helpers ──────────────────────────────────────────────────────────

def _list_session_dirs(wbpp_target_dir: Path) -> list[Path]:
    """Return existing SESSION_N dirs inside wbpp_target_dir."""
    return sorted(
        p for p in wbpp_target_dir.iterdir()
        if p.is_dir() and re.fullmatch(r"SESSION_\d+", p.name)
    )


def _confirm_and_delete(dirs: list[Path], label: str, *, dry_run: bool) -> None:
    """List dirs, prompt for confirmation, delete if confirmed. No-op if dirs is empty."""
    if not dirs:
        return
    print(f"\n{label}:")
    for d in dirs:
        print(f"  {d}")
    if dry_run:
        print("  [dry-run] would delete above")
        return
    answer = input("Delete these directories? [yes/N] ").strip()
    if answer != "yes":
        print("  Skipped.")
        return
    for d in dirs:
        try:
            shutil.rmtree(d)
            print(f"  Deleted: {d.name}")
        except FileNotFoundError:
            print(f"  Already gone: {d.name}")


# ── main command ──────────────────────────────────────────────────────────────

def cmd_finish(
    *,
    output: Path,
    wbpp_root: Path,
    target: str,
    catalog: Path,
    date_override: str | None,
    dry_run: bool,
) -> None:
    slug = _target_slug(target)
    wbpp_target = wbpp_root / slug
    wbpp_output = wbpp_target / "Output"
    master_dir = wbpp_output / "master"
    processed_dir = wbpp_output / "processed"

    if not wbpp_target.exists():
        sys.exit(f"WBPP target dir not found: {wbpp_target}")
    if not wbpp_output.exists():
        sys.exit(f"Output/ not found in {wbpp_target} — did you set the WBPP output dir correctly?")
    if not master_dir.exists():
        sys.exit(f"master/ not found in {wbpp_output}")

    date_str = _find_processing_date(master_dir, processed_dir, date_override)
    dest = _build_dest(output, target, date_str)

    print(f"Destination: {dest}")

    print("\nCopying master/")
    master_count = _copy_flat(master_dir, dest / "master", dry_run=dry_run)
    if not dry_run and master_count == 0:
        sys.exit("Error: master/ contains no .xisf files — aborting (nothing copied)")

    processed_files = [f for f in processed_dir.iterdir() if f.is_file()]
    if not processed_files:
        print("\nWarning: processed/ is empty — skipping")
    else:
        print("\nCopying processed/")
        _copy_flat(processed_dir, dest / "processed", dry_run=dry_run)

    if not dry_run:
        status = str(dest.relative_to(output))
        _mark_sessions_processed(wbpp_target, catalog, output, status, date_str)

    _confirm_and_delete(
        [wbpp_output] if wbpp_output.exists() else [],
        "WBPP Output/ directory to delete (intermediates + master + processed)",
        dry_run=dry_run,
    )
    _confirm_and_delete(
        _list_session_dirs(wbpp_target),
        "SESSION_N directories to delete",
        dry_run=dry_run,
    )


# ── argument parsing ──────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    """Entry point invoked by darkroom.cli."""
    output = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if output is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")

    catalog = resolve_catalog(args.catalog)

    wbpp_root = resolve_path(args.wbpp, "DARKROOM_WBPP", "wbpp_path") or Path("./WBPP")

    cmd_finish(
        output=output,
        wbpp_root=wbpp_root,
        target=args.target,
        catalog=catalog,
        date_override=args.date,
        dry_run=args.dry_run,
    )


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "finish",
        help="Copy WBPP stacks to the archive and mark sessions processed",
        description="Copy master/ and processed/ to <archive>/01_Deep Sky Objects/<target>/_Processed/<date>/, then mark each session as processed in the catalog.",
    )
    p.add_argument("--target", metavar="NAME", required=True, help='Target name (e.g. "M 81")')
    p.add_argument("--archive", metavar="PATH",
                   help="Archive root (env: DARKROOM_ARCHIVE)")
    p.add_argument("--catalog", metavar="PATH",
                   help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)")
    p.add_argument("--wbpp", metavar="PATH",
                   help="Root for WBPP target dirs (env: DARKROOM_WBPP, default: ./WBPP)")
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="Name the _Processed/<date>/ output folder (default: derived "
                        "from WBPP output mtimes). Does NOT select a night — finish "
                        "always processes the whole WBPP target.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be copied/deleted without making changes")
    p.set_defaults(func=run)
