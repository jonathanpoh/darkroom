"""darkroom.catalog_cli — argparse wiring for `darkroom catalog ...` subcommands."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from itertools import groupby

from darkroom.catalog import query_all_sessions
from darkroom.catalog_client import resolve_backend
from darkroom.cataloger import (
    _parse_site_deg,
    mark_processed_command,
    migrate_archive_command,
    scan_all_command,
    scan_calibration_command,
)
from darkroom.config import resolve_catalog, resolve_path
from darkroom.sites import resolve_site


def _resolve_db(args: argparse.Namespace) -> None:
    """Resolve args.db via CLI/env/toml/default; mutate args.db to the resolved string."""
    args.db = str(resolve_catalog(args.catalog))


def _list_run(args: argparse.Namespace) -> None:
    backend = resolve_backend(args.catalog)
    rows = (
        backend.query_sessions(target=args.target)
        if args.target
        else query_all_sessions(backend)
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


def _scan_processed_run(args: argparse.Namespace) -> None:
    """Scan the archive for processing output and reconcile processed_state.

    Dry run (default) is pure-read: it never calls init_db and never opens
    the catalog for writing, so it's safe to point at a live catalog just to
    preview. --apply writes via darkroom.procscan.apply, through the
    catalog backend (local file or webapi, per catalog_url — W9).
    """
    from darkroom import procscan

    backend = resolve_backend(args.catalog)
    archive = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if archive is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")

    transitions = procscan.scan(archive, backend)
    changed = [t for t in transitions if t.change]

    if not args.apply:
        for tgt, group in groupby(
            sorted(changed, key=lambda t: (t.target, t.obs_date)), key=lambda t: t.target
        ):
            print(f"\n{tgt}")
            for t in group:
                tag = f"  [{t.evidence} {t.evidence_date}]" if t.evidence_date else ""
                print(f"  {t.obs_date}  {t.session_id}  {t.current_state} -> {t.proposed_state}{tag}")
        counts = Counter(t.proposed_state for t in changed)
        parts = [f"{n} -> {state}" for state, n in sorted(counts.items())]
        parts.append(f"{len(transitions) - len(changed)} unchanged")
        print(f"\n{', '.join(parts)}; run with --apply to write")
        return

    try:
        applied = procscan.apply(backend, transitions)
    except sqlite3.OperationalError as e:
        sys.exit(
            f"Error writing to catalog: {e}\n"
            "Hint: run any `darkroom catalog` command against this catalog once "
            "(e.g. `catalog list`) to ensure it's migrated to the current schema, "
            "then retry --apply."
        )

    for t in changed:
        tag = f"  [{t.evidence_date}]" if t.evidence_date else ""
        print(f"  {t.session_id}  {t.current_state} -> {t.proposed_state}{tag}")
    print(f"\nApplied {applied} change(s), {len(transitions) - applied} unchanged")


def _apply_renames_run(args: argparse.Namespace) -> None:
    """Execute (or preview) pending archive folder renames (U2 Phase 1).

    Dry run (default) only classifies each pending_renames row against the
    archive filesystem — no moves, no acks. --apply performs the moves
    (darkroom.renames.apply_renames) and acks the ledger row for everything
    it resolved. Exits 1 if any item errored (unsafe path or a filesystem
    error mid-move), 0 otherwise — including when items are left pending as
    'missing' or 'conflict', which are reported but not treated as failure.
    """
    from darkroom import renames

    backend = resolve_backend(
        args.catalog, url_flag=args.catalog_url, token_flag=args.api_token
    )
    archive = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if archive is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")
    if not archive.is_dir():
        sys.exit(f"Error: archive path is not a directory: {archive}")

    results = renames.apply_renames(archive, backend, apply=args.apply)

    verbs = {
        renames.APPLIED: "applied" if args.apply else "would apply",
        renames.ALREADY_DONE: "already in place" + (" (acked)" if args.apply else ""),
        renames.CONFLICT: "conflict",
        renames.MISSING: "missing",
        renames.ERROR: "error",
    }
    for r in results:
        tag = f"  [{r.detail}]" if r.detail else ""
        print(f"  {r.session_id}  {r.old_path} -> {r.new_path}  [{verbs[r.outcome]}]{tag}")

    counts = Counter(r.outcome for r in results)
    order = (renames.APPLIED, renames.ALREADY_DONE, renames.CONFLICT, renames.MISSING, renames.ERROR)
    parts = [f"{counts.get(o, 0)} {o}" for o in order]
    suffix = "" if args.apply else "; run with --apply to write"
    print(f"\n{', '.join(parts)}{suffix}")

    if counts.get(renames.ERROR, 0):
        sys.exit(1)


def _sites_add_run(args: argparse.Namespace) -> None:
    backend = resolve_backend(
        args.catalog, url_flag=args.catalog_url, token_flag=args.api_token
    )
    site = {
        "name": args.name,
        "lat": args.lat,
        "lon": args.lon,
        "radius_m": args.radius_m,
        "bortle": args.bortle,
        "sqm": args.sqm,
        "is_home": args.home,
    }
    try:
        site_id = backend.add_site(site)
    except ValueError as e:
        sys.exit(str(e))
    print(f"added site {args.name!r} (id {site_id})")


def _fmt_opt(val, spec: str = "") -> str:
    """Format a nullable numeric field, blank when None."""
    return format(val, spec) if val is not None else ""


def _sites_list_run(args: argparse.Namespace) -> None:
    backend = resolve_backend(
        args.catalog, url_flag=args.catalog_url, token_flag=args.api_token
    )
    sites = backend.list_sites()
    if not sites:
        print("No sites configured. Run `darkroom catalog sites add` to add one.")
        return

    sessions = backend.query_sessions()
    matched: Counter = Counter()
    unmatched = []
    no_gps = 0
    for row in sessions:
        lat, lon = row.get("site_lat"), row.get("site_lon")
        if lat is None or lon is None:
            no_gps += 1
            continue
        site = resolve_site(lat, lon, sites)
        if site is None:
            unmatched.append(row)
        else:
            matched[site["name"]] += 1

    print(f"{'name':<24} {'lat':>10} {'lon':>10} {'radius_m':>9} {'bortle':>6} {'sqm':>6}  sessions")
    for site in sites:
        name = site["name"] + (" (home)" if site.get("is_home") else "")
        print(
            f"{name:<24} {site['lat']:>10.4f} {site['lon']:>10.4f} "
            f"{site['radius_m']:>9.0f} {_fmt_opt(site.get('bortle')):>6} "
            f"{_fmt_opt(site.get('sqm'), '.1f'):>6}  {matched.get(site['name'], 0)}"
        )

    total_matched = sum(matched.values())
    print(
        f"\n{total_matched} sessions matched, {len(unmatched)} unmatched "
        f"(GPS but no site in radius), {no_gps} without GPS"
    )
    if unmatched:
        print("\nUnmatched sessions (consider a wider --radius-m):")
        for row in unmatched:
            print(f"  {row['session_id']}: {row['site_lat']:.4f}, {row['site_lon']:.4f}")


def _sites_set_run(args: argparse.Namespace) -> None:
    backend = resolve_backend(
        args.catalog, url_flag=args.catalog_url, token_flag=args.api_token
    )
    fields: dict = {}
    if args.new_name is not None:
        fields["name"] = args.new_name
    if args.lat is not None:
        fields["lat"] = args.lat
    if args.lon is not None:
        fields["lon"] = args.lon
    if args.radius_m is not None:
        fields["radius_m"] = args.radius_m
    if args.bortle is not None:
        fields["bortle"] = args.bortle
    if args.sqm is not None:
        fields["sqm"] = args.sqm
    if args.home:
        fields["is_home"] = True

    if not fields:
        sys.exit(
            "Error: nothing to update — pass at least one of "
            "--name/--lat/--lon/--radius-m/--bortle/--sqm/--home"
        )

    try:
        updated = backend.update_site(args.name, fields)
    except ValueError as e:
        sys.exit(str(e))
    if not updated:
        sys.exit(f"Error: site {args.name!r} not found")

    if "name" in fields:
        print(f"updated site {args.name!r} (renamed to {fields['name']!r})")
    else:
        print(f"updated site {args.name!r}")


def _backfill_sites_run(args: argparse.Namespace) -> None:
    """Backfill site_lat/site_lon on sessions from archive FITS SITELAT/SITELONG.

    Dry run (default) is pure-read: it never writes to the catalog. --apply
    writes via update_session_fields, through the catalog backend (local
    file or webapi, per catalog_url — W9). Only sessions with a NULL
    site_lat are ever candidates, so re-running is a no-op once applied
    (idempotent by construction).
    """
    from astropy.io import fits

    backend = resolve_backend(
        args.catalog, url_flag=args.catalog_url, token_flag=args.api_token
    )
    archive = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if archive is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")

    rows = backend.query_sessions()
    candidates = [r for r in rows if r.get("lights_path") and r.get("site_lat") is None]

    found = []  # list[(row, lat, lon)]
    no_headers = 0
    missing = 0
    read_errors = 0

    for row in candidates:
        folder = archive / row["lights_path"]
        if not folder.is_dir():
            missing += 1
            continue

        frame = None
        for p in sorted(folder.rglob("*")):
            if p.suffix.lower() in (".fit", ".fits") and "thumbnail" not in p.name.lower():
                frame = p
                break
        if frame is None:
            no_headers += 1
            continue

        try:
            header = fits.getheader(frame)
        except Exception:
            read_errors += 1
            continue

        lat = _parse_site_deg(header.get("SITELAT"))
        lon = _parse_site_deg(header.get("SITELONG"))
        if lat is None or lon is None:
            no_headers += 1
            continue
        found.append((row, lat, lon))

    if not args.apply:
        sites = backend.list_sites()
        for tgt, group in groupby(
            sorted(found, key=lambda f: (f[0]["target"], f[0]["session_id"])),
            key=lambda f: f[0]["target"],
        ):
            print(f"\n{tgt}")
            for row, lat, lon in group:
                site = resolve_site(lat, lon, sites)
                site_name = site["name"] if site else "(no site in radius)"
                print(f"  {row['session_id']}: {lat:.4f}, {lon:.4f} -> {site_name}")
        parts = [
            f"{len(found)} would be set",
            f"{no_headers} no site headers",
            f"{missing} missing on disk",
        ]
        if read_errors:
            parts.append(f"{read_errors} read errors")
        print(f"\n{', '.join(parts)}; run with --apply to write")
        return

    try:
        written = 0
        for row, lat, lon in found:
            if backend.update_session_fields(row["session_id"], site_lat=lat, site_lon=lon):
                written += 1
    except sqlite3.OperationalError as e:
        sys.exit(
            f"Error writing to catalog: {e}\n"
            "Hint: run any `darkroom catalog` command against this catalog once "
            "(e.g. `catalog list`) to ensure it's migrated to the current schema, "
            "then retry --apply."
        )

    parts = [
        f"{written} set",
        f"{no_headers} no site headers",
        f"{missing} missing on disk",
    ]
    if read_errors:
        parts.append(f"{read_errors} read errors")
    print(", ".join(parts))


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
    m.add_argument("state", choices=["unprocessed", "in_progress", "processed", "skipped"],
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

    sp = sub.add_parser(
        "scan-processed", parents=[catalog_flag],
        help="Scan the archive for processing output and reconcile processed_state",
        description="Scan <archive>/01_Deep Sky Objects/<target>/ for stacked/edited "
                    "output (.xisf masters/intermediates, PixInsight project files, "
                    "final exports) and propose a processed_state upgrade "
                    "(unprocessed -> in_progress -> processed) for each session whose "
                    "evidence date is on or after its obs_date. Never downgrades and "
                    "never touches a skipped session. Dry run by default (prints "
                    "proposed changes, writes nothing); pass --apply to write them.",
    )
    sp.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    sp.add_argument("--apply", action="store_true",
                     help="Write proposed changes to the catalog (default: dry run, read-only)")
    sp.set_defaults(func=_scan_processed_run)

    ar = sub.add_parser(
        "apply-renames", parents=[catalog_flag],
        help="Execute pending archive folder renames owed by catalog identity edits",
        description="Read the pending_renames ledger (populated server-side when a "
                    "catalog identity edit changes a session's lights_path — the "
                    "webapi host has no NAS mount, so it can only record the folder "
                    "move it owes) and resolve each entry against the local/mounted "
                    "archive. Dry run by default (prints proposed actions, writes "
                    "nothing); pass --apply to move folders and ack the ledger.",
    )
    ar.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    ar.add_argument("--catalog-url", metavar="URL",
                     help="Catalog API base URL (env: DARKROOM_CATALOG_URL)")
    ar.add_argument("--api-token", metavar="TOKEN",
                     help="Catalog API bearer token (env: DARKROOM_API_TOKEN)")
    ar.add_argument("--apply", action="store_true",
                     help="Move folders and ack the ledger (default: dry run, read-only)")
    ar.set_defaults(func=_apply_renames_run)

    # --catalog-url/--api-token, shared by the sites group and backfill-sites
    # (copied from apply-renames' registration above).
    catalog_url_flag = argparse.ArgumentParser(add_help=False)
    catalog_url_flag.add_argument("--catalog-url", metavar="URL",
                                   help="Catalog API base URL (env: DARKROOM_CATALOG_URL)")
    catalog_url_flag.add_argument("--api-token", metavar="TOKEN",
                                   help="Catalog API bearer token (env: DARKROOM_API_TOKEN)")
    site_flags = [catalog_flag, catalog_url_flag]

    sites_p = sub.add_parser("sites", help="Manage observing sites")
    site_sub = sites_p.add_subparsers(dest="sitecmd", required=True)

    sa = site_sub.add_parser("add", parents=site_flags, help="Add a new site")
    sa.add_argument("name", help="Site name")
    sa.add_argument("lat", type=float, help="Latitude (decimal degrees)")
    sa.add_argument("lon", type=float, help="Longitude (decimal degrees)")
    sa.add_argument("--radius-m", type=float, default=1000.0, metavar="M",
                     help="Match radius in metres (default: 1000)")
    sa.add_argument("--bortle", type=int, metavar="N", help="Bortle scale (1-9)")
    sa.add_argument("--sqm", type=float, metavar="X", help="Sky quality (mag/arcsec^2)")
    sa.add_argument("--home", action="store_true",
                     help="Mark this the home site (clears any existing home)")
    sa.set_defaults(func=_sites_add_run)

    sls = site_sub.add_parser("list", parents=site_flags,
                               help="List configured sites and matched sessions")
    sls.set_defaults(func=_sites_list_run)

    ss = site_sub.add_parser("set", parents=site_flags, help="Update an existing site")
    ss.add_argument("name", help="Current site name")
    ss.add_argument("--name", dest="new_name", metavar="NEW", help="Rename the site")
    ss.add_argument("--lat", type=float, metavar="X", help="New latitude")
    ss.add_argument("--lon", type=float, metavar="Y", help="New longitude")
    ss.add_argument("--radius-m", type=float, metavar="M", help="New match radius in metres")
    ss.add_argument("--bortle", type=int, metavar="N", help="New Bortle scale (1-9)")
    ss.add_argument("--sqm", type=float, metavar="X", help="New sky quality (mag/arcsec^2)")
    ss.add_argument("--home", action="store_true",
                     help="Mark this the home site (clears any existing home)")
    ss.set_defaults(func=_sites_set_run)

    bf = sub.add_parser(
        "backfill-sites", parents=site_flags,
        help="Backfill site_lat/site_lon on sessions from archive FITS headers",
        description="Scan each session with a NULL site_lat for its first FITS frame's "
                    "SITELAT/SITELONG headers and propose setting site_lat/site_lon from "
                    "them. Dry run by default (prints proposed changes, writes nothing); "
                    "pass --apply to write them. Idempotent: only NULL site_lat sessions "
                    "are ever candidates.",
    )
    bf.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    bf.add_argument("--apply", action="store_true",
                     help="Write proposed changes to the catalog (default: dry run, read-only)")
    bf.set_defaults(func=_backfill_sites_run)
