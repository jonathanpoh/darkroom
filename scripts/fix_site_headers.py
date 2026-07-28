#!/usr/bin/env python3
"""Repair SITELAT/SITELONG in archived light frames.

The ASIAir takes its site coordinates from the phone running the app. When the
phone has no cellular fix it falls back to WiFi-BSSID geolocation, which
resolves to wherever the access point is registered in Apple's location
database rather than where you actually are. The result is a *confident wrong
answer*: a stable, plausible-looking terrestrial position that can persist for
hours and across sessions, because the ASIAir only re-reads location at autorun
start.

This script overwrites SITELAT/SITELONG for every FITS frame under a directory
with a known-correct position, recording the original value in a HISTORY card
so the edit stays auditable.

Dry run by default; pass --apply to write. Idempotent: frames already carrying
the target coordinates (within --tolerance) are skipped, so re-running is a
no-op.

    # inspect what's there, change nothing
    uv run python scripts/fix_site_headers.py "<dir>" --lat 38.395014 --lon -8.310778

    # write it
    uv run python scripts/fix_site_headers.py "<dir>" --lat 38.395014 --lon -8.310778 --apply
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from astropy.io import fits

# Header edits are cheap to describe and expensive to get wrong, so the
# original value goes into a HISTORY card on every frame we touch.
HISTORY_TAG = "darkroom fix_site_headers"


def _fits_frames(root: Path) -> list[Path]:
    """Every FITS frame under *root*, thumbnails excluded (matches parse.fits_files)."""
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in (".fit", ".fits") and "thumbnail" not in p.name.lower()
    )


def _close_enough(val, target: float, tol: float) -> bool:
    return val is not None and abs(float(val) - target) <= tol


def survey(frames: list[Path]) -> Counter:
    """Distinct (SITELAT, SITELONG) pairs across *frames*, most common first."""
    counts: Counter = Counter()
    for path in frames:
        header = fits.getheader(path)
        counts[(header.get("SITELAT"), header.get("SITELONG"))] += 1
    return counts


def patch(frames: list[Path], lat: float, lon: float, tol: float, apply: bool) -> tuple[int, int]:
    """Set SITELAT/SITELONG on each frame that needs it. Returns (changed, skipped)."""
    changed = skipped = 0
    stamp = date.today().isoformat()

    for path in frames:
        with fits.open(path, mode="update" if apply else "readonly") as hdul:
            header = hdul[0].header
            old_lat, old_lon = header.get("SITELAT"), header.get("SITELONG")

            if _close_enough(old_lat, lat, tol) and _close_enough(old_lon, lon, tol):
                skipped += 1
                continue

            changed += 1
            if not apply:
                continue

            header["SITELAT"] = lat
            header["SITELONG"] = lon
            header.add_history(
                f"{HISTORY_TAG} {stamp}: SITELAT {old_lat} -> {lat}, "
                f"SITELONG {old_lon} -> {lon}"
            )

    return changed, skipped


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Overwrite SITELAT/SITELONG in archived FITS light frames.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("directory", type=Path, help="Directory of frames (searched recursively)")
    ap.add_argument("--lat", type=float, required=True, help="Correct site latitude (deg)")
    ap.add_argument("--lon", type=float, required=True, help="Correct site longitude (deg)")
    ap.add_argument(
        "--tolerance",
        type=float,
        default=1e-4,
        help="Frames already within this many degrees are left alone (default: 1e-4, ~11 m)",
    )
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()

    if not args.directory.is_dir():
        sys.exit(f"Error: not a directory: {args.directory}")

    frames = _fits_frames(args.directory)
    if not frames:
        sys.exit(f"Error: no FITS frames under {args.directory}")

    print(f"{args.directory}")
    print(f"  {len(frames)} frames; target {args.lat}, {args.lon}\n")
    print("  current values:")
    for (old_lat, old_lon), n in survey(frames).most_common():
        print(f"    {n:4d}  SITELAT={old_lat}  SITELONG={old_lon}")

    changed, skipped = patch(frames, args.lat, args.lon, args.tolerance, args.apply)
    verb = "rewritten" if args.apply else "would be rewritten"
    print(f"\n  {changed} {verb}, {skipped} already correct")
    if changed and not args.apply:
        print("  re-run with --apply to write")


if __name__ == "__main__":
    main()
