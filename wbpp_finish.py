#!/usr/bin/env python3
"""wbpp_finish.py — Copy WBPP stacks back to the NAS archive and clean up working dirs."""
from __future__ import annotations

import argparse
import os
import re
import sys
import shutil
import tomllib
from datetime import datetime
from pathlib import Path


# ── config resolution (mirrors wbpp_prep.py) ──────────────────────────────────

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


def _target_slug(target: str) -> str:
    return target.replace(" ", "")


# ── core helpers ──────────────────────────────────────────────────────────────

def _find_master_date(master_dir: Path) -> str:
    """Return YYYY-MM-DD creation date of the first masterLight_*.xisf in master_dir."""
    candidates = sorted(master_dir.glob("masterLight_*.xisf"))
    if not candidates:
        sys.exit(f"No masterLight_*.xisf found in {master_dir}")
    stat = candidates[0].stat()
    ts = getattr(stat, "st_birthtime", stat.st_mtime)
    return datetime.fromtimestamp(ts).date().isoformat()


def _build_dest(output: Path, target: str, date_str: str) -> Path:
    """Return <output>/04_Deep Sky Objects/<target>/_Processed/<date_str>."""
    return output / "04_Deep Sky Objects" / target / "_Processed" / date_str


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

_INTERMEDIATE_NAMES = {"calibrated", "debayered", "fastIntegration", "logs"}


def _list_intermediates(wbpp_target_dir: Path) -> list[Path]:
    """Return existing intermediate dirs (named dirs + SESSION_N dirs) inside wbpp_target_dir."""
    result = []
    for p in wbpp_target_dir.iterdir():
        if p.is_dir() and (p.name in _INTERMEDIATE_NAMES or re.fullmatch(r"SESSION_\d+", p.name)):
            result.append(p)
    return sorted(result, key=lambda p: p.name)


def _list_outputs(wbpp_target_dir: Path) -> list[Path]:
    """Return existing master/ and processed/ dirs inside wbpp_target_dir."""
    result = []
    for name in ("master", "processed"):
        p = wbpp_target_dir / name
        if p.is_dir():
            result.append(p)
    return result


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
    dry_run: bool,
) -> None:
    slug = _target_slug(target)
    wbpp_target = wbpp_root / slug
    master_dir = wbpp_target / "master"
    processed_dir = wbpp_target / "processed"

    if not wbpp_target.exists():
        sys.exit(f"WBPP target dir not found: {wbpp_target}")
    if not master_dir.exists():
        sys.exit(f"master/ not found in {wbpp_target}")
    if not processed_dir.exists():
        sys.exit(f"processed/ not found in {wbpp_target}")

    date_str = _find_master_date(master_dir)
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

    print(f'\nDone. Remember to mark "{target}" as processed in darkroom-catalog once that command is implemented.')

    _confirm_and_delete(
        _list_intermediates(wbpp_target),
        "Intermediate directories to delete",
        dry_run=dry_run,
    )
    _confirm_and_delete(
        _list_outputs(wbpp_target),
        "Working output directories to delete (master/ and processed/)",
        dry_run=dry_run,
    )


# ── argument parsing ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Copy WBPP stacks to the NAS archive and clean up working dirs."
    )
    p.add_argument("--target", metavar="NAME", required=True, help='Target name (e.g. "M 81")')
    p.add_argument("--output", metavar="PATH",
                   help="Archive root — same value as wbpp_prep.py --output")
    p.add_argument("--wbpp", metavar="PATH", default="./WBPP",
                   help="Root for WBPP target dirs (default: ./WBPP)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be copied/deleted without making changes")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    output = resolve_path(args.output, "DARKROOM_OUTPUT", "output_path")
    if output is None:
        sys.exit("Error: --output / DARKROOM_OUTPUT / darkroom.toml output_path required")

    cmd_finish(
        output=output,
        wbpp_root=Path(args.wbpp),
        target=args.target,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
