# darkroom/triage/cli.py
from __future__ import annotations

import argparse
from pathlib import Path


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("triage", help="Interactive archive triage web UI")
    sub2 = p.add_subparsers(dest="triage_cmd", required=True)

    # scan
    scan_p = sub2.add_parser("scan", help="Scan archive and populate triage.db")
    scan_p.add_argument("--archive", required=True, type=Path,
                        help="Path to staging archive root")
    scan_p.add_argument("--db", type=Path,
                        help="Path to triage.db (default: <archive>/triage.db)")
    scan_p.set_defaults(func=_cmd_scan)

    # serve
    serve_p = sub2.add_parser("serve", help="Start triage web UI")
    serve_p.add_argument("--archive", required=True, type=Path)
    serve_p.add_argument("--db", type=Path)
    serve_p.add_argument("--port", type=int, default=8002)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.set_defaults(func=_cmd_serve)

    p.set_defaults(func=lambda args: p.print_help())


def _resolve_db(args) -> Path:
    if args.db:
        return args.db
    return Path(args.archive) / "triage.db"


def _cmd_scan(args) -> None:
    from darkroom.triage.db import open_db, upsert_item
    from darkroom.triage.scanner import scan_archive

    archive = Path(args.archive)
    db_path = _resolve_db(args)
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

    archive = Path(args.archive)
    db_path = _resolve_db(args)

    app = create_app(db_path=db_path, archive_root=archive)
    print(f"Starting triage server at http://{args.host}:{args.port}")
    print(f"  archive: {archive}")
    print(f"  db:      {db_path}")
    uvicorn.run(app, host=args.host, port=args.port)
