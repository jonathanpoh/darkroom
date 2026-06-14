# darkroom/triage/cli.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from darkroom.config import resolve_path


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("triage", help="Interactive archive triage web UI")
    sub2 = p.add_subparsers(dest="triage_cmd", required=True)

    # scan
    scan_p = sub2.add_parser("scan", help="Scan archive and populate triage.db")
    scan_p.add_argument("--archive", type=Path,
                        help="Archive root to scan (env: DARKROOM_ARCHIVE)")
    scan_p.add_argument("--db", type=Path,
                        help="triage.db — NOT the catalog (default: <archive>/triage.db)")
    scan_p.set_defaults(func=_cmd_scan)

    # serve
    serve_p = sub2.add_parser("serve", help="Start triage web UI")
    serve_p.add_argument("--archive", type=Path,
                         help="Archive root to serve (env: DARKROOM_ARCHIVE)")
    serve_p.add_argument("--db", type=Path,
                         help="triage.db — NOT the catalog (default: <archive>/triage.db)")
    serve_p.add_argument("--port", type=int, default=8002)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.set_defaults(func=_cmd_serve)

    p.set_defaults(func=lambda args: p.print_help())


def _resolve_archive(args) -> Path:
    archive = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if archive is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")
    return archive


def _resolve_db(args, archive: Path) -> Path:
    if args.db:
        return args.db
    return archive / "triage.db"


def _cmd_scan(args) -> None:
    from darkroom.triage.db import open_db, upsert_item
    from darkroom.triage.scanner import scan_archive

    archive = _resolve_archive(args)
    db_path = _resolve_db(args, archive)
    conn = open_db(db_path)

    candidates = scan_archive(archive)
    new_count = 0
    for c in candidates:
        prev = conn.execute(
            "SELECT status FROM triage_items WHERE source_path = ?",
            (c.source_path,),
        ).fetchone()
        if prev is None:
            upsert_item(
                conn,
                category=c.category,
                source_path=c.source_path,
                proposed_path=c.proposed_path,
                proposed_value=c.proposed_value,
                fits_metadata=c.fits_metadata if c.fits_metadata else None,
            )
            new_count += 1

    total = conn.execute("SELECT COUNT(*) FROM triage_items").fetchone()[0]
    print(f"Scan complete: {new_count} new items added, {total} total in {db_path}")


def _cmd_serve(args) -> None:
    import uvicorn
    from darkroom.triage.server import create_app

    archive = _resolve_archive(args)
    db_path = _resolve_db(args, archive)

    app = create_app(db_path=db_path, archive_root=archive)
    print(f"Starting triage server at http://{args.host}:{args.port}")
    print(f"  archive: {archive}")
    print(f"  db:      {db_path}")
    uvicorn.run(app, host=args.host, port=args.port)
