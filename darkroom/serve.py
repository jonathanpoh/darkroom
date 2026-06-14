"""darkroom.serve — Launch datasette on the darkroom catalog."""
from __future__ import annotations

import argparse
import os
import shutil
import sys

from darkroom.config import resolve_catalog


def run(args: argparse.Namespace) -> None:
    catalog = resolve_catalog(args.catalog)
    if not catalog.exists():
        sys.exit(f"Catalog not found: {catalog}\nRun `darkroom catalog scan-lights` first.")
    if shutil.which("datasette") is None:
        sys.exit("datasette not found on PATH — install it with `pip install datasette` (or `uv tool install datasette`).")
    os.execvp("datasette", ["datasette", "serve", str(catalog)])


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "serve",
        help="Browse the catalog with datasette",
        description="Launch datasette on the darkroom catalog.",
    )
    p.add_argument(
        "--catalog", metavar="PATH",
        help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)",
    )
    p.set_defaults(func=run)
