#!/usr/bin/env python3
"""Report FITS files with missing or placeholder OBJECT headers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from astropy.io import fits


def check_object_value(value: str | None) -> str | None:
    """Return 'MISSING' or 'FOV' if the value is problematic, else None."""
    if value is None or str(value).strip() == "":
        return "MISSING"
    if str(value).strip().upper() == "FOV":
        return "FOV"
    return None


_FITS_SUFFIXES = {".fit", ".fits"}


def collect_fits_files(root: Path) -> list[Path]:
    """Return sorted FITS files under root, excluding thumbnails."""
    results: list[Path] = []
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in name:
                results.append(p)
    return sorted(results)


def scan_file(path: Path) -> tuple[Path, str] | None:
    """Open a FITS file and return (path, reason) if OBJECT is problematic, else None."""
    try:
        with fits.open(path) as hdul:
            object_val = hdul[0].header.get("OBJECT")
    except Exception as exc:
        print(f"WARNING: could not read {path}: {exc}", file=sys.stderr)
        return None
    reason = check_object_value(object_val)
    if reason is not None:
        return (path, reason)
    return None


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Report FITS files with missing or placeholder OBJECT headers."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="File(s) or directory(ies) to scan (directories scanned recursively).",
    )
    args = parser.parse_args()

    all_files: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if p.is_dir():
            all_files.extend(collect_fits_files(p))
        elif p.is_file():
            all_files.append(p)
        else:
            print(f"WARNING: {p} not found", file=sys.stderr)

    missing_count = 0
    fov_count = 0
    for path in all_files:
        result = scan_file(path)
        if result is not None:
            _, reason = result
            print(f"{path}  [{reason}]")
            if reason == "MISSING":
                missing_count += 1
            else:
                fov_count += 1

    total_flagged = missing_count + fov_count
    print(
        f"\nScanned {len(all_files)} files — {total_flagged} flagged"
        + (f" (missing: {missing_count}, FOV: {fov_count})" if total_flagged else "")
    )


if __name__ == "__main__":
    main()
