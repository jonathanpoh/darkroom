"""darkroom.catalog_cli — argparse wiring for `darkroom catalog ...` subcommands."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from itertools import groupby
from pathlib import Path

from darkroom import guidelog
from darkroom.catalog import format_session_lines, list_sessions
from darkroom.catalog_client import CatalogBackend, resolve_backend
from darkroom.cataloger import (
    _parse_site_deg,
    mark_processed_command,
    migrate_archive_command,
    scan_all_command,
    scan_calibration_command,
)
from darkroom.config import require_archive, resolve_catalog, resolve_path
from darkroom.parse import fits_files
from darkroom.sites import resolve_site, session_site


def _resolve_db(args: argparse.Namespace) -> None:
    """Resolve args.db via CLI/env/toml/default; mutate args.db to the resolved string."""
    args.db = str(resolve_catalog(args.catalog))


def _backend(args: argparse.Namespace) -> CatalogBackend:
    """resolve_backend from the shared --catalog/--catalog-url/--api-token flags.

    The URL/token flags are optional on the namespace so subcommands (and
    tests) that only declare --catalog still resolve through env/toml.
    """
    return resolve_backend(
        args.catalog,
        url_flag=getattr(args, "catalog_url", None),
        token_flag=getattr(args, "api_token", None),
    )


def _exit_catalog_write_error(e: sqlite3.OperationalError) -> None:
    """The one hint every --apply path gives when the local catalog schema is stale."""
    sys.exit(
        f"Error writing to catalog: {e}\n"
        "Hint: run any `darkroom catalog` command against this catalog once "
        "(e.g. `catalog list`) to ensure it's migrated to the current schema, "
        "then retry --apply."
    )


def _list_run(args: argparse.Namespace) -> None:
    rows = list_sessions(_backend(args), args.target)
    if not rows:
        print("No sessions found.")
        return
    print("\n".join(format_session_lines(rows, with_state=True)))


def _scan_lights_run(args: argparse.Namespace) -> None:
    scan_all_command(args, backend=_backend(args))


def _scan_calibration_run(args: argparse.Namespace) -> None:
    scan_calibration_command(args, backend=_backend(args))


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

    backend = _backend(args)
    archive = require_archive(args.archive)

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
        _exit_catalog_write_error(e)

    for t in changed:
        tag = f"  [{t.evidence_date}]" if t.evidence_date else ""
        print(f"  {t.session_id}  {t.current_state} -> {t.proposed_state}{tag}")
    print(f"\nApplied {applied} change(s), {len(transitions) - applied} unchanged")


def _print_rescan_summary(proposals: list[dict]) -> None:
    """Group proposals by target; show each proposal's tier and changed fields."""

    def sort_key(p: dict) -> tuple[str, str, str]:
        return (p.get("target") or "", p.get("obs_date") or "", p["session_id"])

    for tgt, group in groupby(
        sorted(proposals, key=sort_key), key=lambda p: p.get("target") or "(no target)"
    ):
        print(f"\n{tgt}")
        for p in group:
            print(f"  {p.get('obs_date') or '?'}  {p['session_id']}  [{p['kind']}/{p['tier']}]")
            for field, delta in sorted(p["changes"].items()):
                print(f"      {field}: {delta['current']!r} -> {delta['proposed']!r}")


def _confirm_empty_disk(catalog_session_count: int, args: argparse.Namespace) -> bool:
    """Warn + require confirmation before treating '0 sessions on disk' as real.

    The archive root existing but the walk finding nothing (unmounted-but-
    present mountpoint, wrong --archive subdirectory, permissions) must never
    be waved through into a proposal to delete every catalog session. --yes
    skips the prompt for non-interactive use; matching `ingest review`'s
    posture (CLAUDE.md), no TTY and no --yes refuses rather than proceeding.
    """
    print(
        f"WARNING: 0 sessions found on disk, but {catalog_session_count} session(s) "
        f"are in the catalog — proceeding would push {catalog_session_count} "
        "'delete' proposal(s) to the review queue.\n"
        "This almost always means the archive isn't actually mounted/reachable "
        "at --archive, not that it was genuinely wiped.",
        file=sys.stderr,
    )
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Error: refusing without --yes (no TTY to confirm).",
            file=sys.stderr,
        )
        return False
    answer = input("Type 'yes' to proceed anyway, or press Enter to abort: ").strip()
    return answer == "yes"


def _rescan_archive_run(args: argparse.Namespace) -> None:
    """Diff the archive against the catalog and queue divergences for review (F8).

    Strictly read-only pass: rescans <archive>/01_Deep Sky Objects/ (the same
    walk `scan-lights` uses) and diffs it against the catalog, classifying
    each session_id found on either side as matching (no-op), diverging
    (update), on-disk-only (create), or catalog-only (delete). Dry run by
    default — prints a grouped summary, writes nothing.

    --apply does NOT edit any session row. It calls
    darkroom.rescan.apply -> backend.replace_rescan_proposals, which PUSHES
    these findings to the rescan_proposals review queue (superseding the
    previous pending set — applied/dismissed rows are left alone as the audit
    trail). A human (or the queue's pre-approved 'safe' tier) still has to
    apply each one from there; nothing about this command touches `sessions`.

    Refuses outright if the archive's DSO root doesn't exist at all (an
    unmounted NAS must never read as "archive is empty"), and warns +
    requires confirmation (--yes, or a 'yes' at the prompt) if the root
    exists but the walk finds 0 sessions while the catalog has some — that
    shape is what a wrong/partially-mounted --archive looks like, and taking
    it at face value would generate a delete proposal for every session in
    the catalog. An empty catalog with a full disk (ordinary first run)
    never triggers either guard.
    """
    from darkroom import rescan

    backend = _backend(args)
    archive = require_archive(args.archive)

    try:
        proposals = rescan.scan(
            archive, backend, pointing_tolerance_deg=args.pointing_tolerance
        )
    except rescan.ArchiveRootMissing as e:
        sys.exit(
            f"Error: {e}\n"
            "Hint: check --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path "
            "— is the NAS actually mounted?"
        )
    except rescan.EmptyDiskDivergence as e:
        if not _confirm_empty_disk(e.catalog_session_count, args):
            sys.exit("Aborted.")
        proposals = rescan.scan(
            archive, backend, pointing_tolerance_deg=args.pointing_tolerance,
            allow_empty_disk=True,
        )

    if not args.apply:
        _print_rescan_summary(proposals)
        counts = Counter(p["kind"] for p in proposals)
        tiers = Counter(p["tier"] for p in proposals)
        parts = [f"{n} {kind}" for kind, n in sorted(counts.items())]
        summary = ", ".join(parts) if parts else "no divergences found"
        print(
            f"\n{summary} ({tiers.get('safe', 0)} safe, {tiers.get('review', 0)} review); "
            "run with --apply to push these to the review queue "
            "(this does NOT write to sessions)"
        )
        return

    written = rescan.apply(backend, proposals)
    print(f"Pushed {written} proposal(s) to the review queue ({len(proposals)} found)")


def _scan_guiding_run(args: argparse.Namespace) -> None:
    """Match PHD2 guide-log segments to sessions by time and store the stats.

    Matching is purely temporal — each session's stored start_utc/end_utc span
    is intersected with the segments parsed out of every log. Log target names
    are never consulted (see darkroom.guidescan).

    Dry run (default) is pure-read: it parses logs and reads sessions, and
    writes nothing. --apply writes via darkroom.guidescan.apply, through the
    catalog backend (local file or webapi, per catalog_url — W9).

    Both halves of what didn't match are reported and never guessed at: a
    whole date range failing to match means the ASIAir clock/timezone was not
    what the parser assumes, which is the user's call, not the scanner's.

    `--settle-exclude` tunes how much post-dither settling is discarded. It is
    left at guidelog.DEFAULT_SETTLE_EXCLUDE_SEC by default because the stored
    numbers are only comparable across sessions at one setting.
    """
    from darkroom import guidescan, logs

    backend = _backend(args)

    if args.logs:
        logs_dir = Path(args.logs).expanduser()
    else:
        archive = resolve_path(None, "DARKROOM_ARCHIVE", "archive_path")
        if archive is None:
            sys.exit(
                "Error: --logs, or --archive / DARKROOM_ARCHIVE / darkroom.toml "
                "archive_path (whose 00_Logs/ASIAir subdirectory is used), required"
            )
        logs_dir = archive / logs.ARCHIVE_SUBDIR
    if not logs_dir.is_dir():
        sys.exit(f"Error: guide log directory not found: {logs_dir}")

    result = guidescan.scan(
        logs_dir, backend, settle_exclude_sec=args.settle_exclude
    )

    for line in _guiding_report(result):
        print(line)

    if not args.apply:
        for tgt, group in groupby(
            sorted(result.matches, key=lambda m: (m.target, m.obs_date)),
            key=lambda m: m.target,
        ):
            print(f"\n{tgt}")
            for m in group:
                cov = "" if m.coverage is None else f"  cov {m.coverage * 100:.0f}%"
                print(
                    f"  {m.obs_date}  {m.session_id}  {m.rms_total_arcsec:.2f}\" "
                    f"(RA {m.rms_ra_arcsec:.2f} Dec {m.rms_dec_arcsec:.2f})"
                    f"{cov}  {m.guide_frames} frames"
                )
        print(
            f"\n{len(result.matches)} session(s) would get guiding stats; "
            "run with --apply to write"
        )
        return

    try:
        applied = guidescan.apply(backend, result)
    except sqlite3.OperationalError as e:
        _exit_catalog_write_error(e)

    print(f"\nApplied guiding stats to {applied} session(s)")


def _guiding_report(result) -> list[str]:
    """The unmatched-both-ways report every scan-guiding run prints.

    Deliberately identical in dry-run and --apply mode: the mismatches are the
    diagnostic, not a preview of pending writes.
    """
    lines = [
        f"{result.log_count} log(s), {result.segment_count} guiding segment(s); "
        f"{len(result.matches)} session(s) matched"
    ]
    if result.undated_sessions:
        lines.append(
            f"  {len(result.undated_sessions)} session(s) have no start_utc — "
            "run `darkroom catalog backfill-times` first"
        )
    if result.unmatched_sessions:
        lines.append(
            f"  {len(result.unmatched_sessions)} dated session(s) matched no guide data:"
        )
        for row in result.unmatched_sessions:
            lines.append(f"    {row['obs_date']}  {row['session_id']}")
    if result.unmatched_logs:
        lines.append(f"  {len(result.unmatched_logs)} log(s) matched no session:")
        lines.extend(f"    {name}" for name in result.unmatched_logs)
    return lines


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

    backend = _backend(args)
    archive = require_archive(args.archive)
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
    backend = _backend(args)
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
    backend = _backend(args)
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


# `sites set` flag attribute -> site column; --home is a store_true, handled apart.
_SITE_SET_FIELDS = {
    "new_name": "name", "lat": "lat", "lon": "lon",
    "radius_m": "radius_m", "bortle": "bortle", "sqm": "sqm",
}


def _sites_set_run(args: argparse.Namespace) -> None:
    backend = _backend(args)
    fields = {
        col: getattr(args, attr)
        for attr, col in _SITE_SET_FIELDS.items()
        if getattr(args, attr) is not None
    }
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


# ── backfill-* : fill NULL session columns from archive FITS headers ─────────
#
# `backfill-sites` and `backfill-times` share everything but the per-session
# extraction: candidate selection (lights_path present, target column NULL —
# so re-running is a no-op once applied), the per-frame header read with its
# error tally, the grouped dry-run listing, the --apply write loop through the
# catalog backend (local file or webapi, per catalog_url — W9), and the
# summary line. _backfill_run is that scaffold; each command supplies an
# extractor returning either the values to write or a skip reason.

# Skip reasons every backfill tallies. `no_headers` is the "nothing usable in
# the frames" bucket; `missing` is the folder itself. A command may add its
# own (backfill-times: `wrong_night`) via _Backfill.extra_skips.
_NO_HEADERS = "no_headers"
_MISSING = "missing"
_UNREADABLE = "unreadable"  # every frame failed to open; counted under read_errors only


class _Backfill:
    """One backfill command's specifics, consumed by _backfill_run."""

    def __init__(
        self,
        *,
        null_column: str,
        columns: tuple[str, ...],
        headers_label: str,
        extract,
        describe,
        extra_skips: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.null_column = null_column      # candidates are rows where this is NULL
        self.columns = columns              # update_session_fields keys, in `values` order
        self.headers_label = headers_label  # summary wording for the no_headers tally
        self.extract = extract              # (row, headers) -> values tuple | skip reason
        self.describe = describe            # (backend) -> ((row, values) -> dry-run text)
        self.extra_skips = extra_skips      # (reason, summary label) in print order


def _read_headers(frames: list[Path]) -> tuple[list, int]:
    """Open every frame's header; return (headers, unreadable count).

    Every frame, not just the first: the extractors need the whole set (a
    modal position, a min/max span), and read errors are skipped per-frame so
    one bad file doesn't cost the session its result.
    """
    from astropy.io import fits

    headers = []
    unreadable = 0
    for frame in frames:
        try:
            headers.append(fits.getheader(frame))
        except Exception:
            unreadable += 1
    return headers, unreadable


def _backfill_run(args: argparse.Namespace, spec: _Backfill) -> None:
    backend = _backend(args)
    archive = require_archive(args.archive)

    rows = backend.query_sessions()
    candidates = [r for r in rows if r.get("lights_path") and r.get(spec.null_column) is None]

    found: list[tuple[dict, tuple]] = []
    tally: Counter = Counter()
    for row in candidates:
        folder = archive / row["lights_path"]
        if not folder.is_dir():
            tally[_MISSING] += 1
            continue
        frames = fits_files(folder, recursive=True)
        if not frames:
            tally[_NO_HEADERS] += 1
            continue
        headers, unreadable = _read_headers(frames)
        if unreadable:
            tally["read_errors"] += 1
        result = spec.extract(row, headers)
        if isinstance(result, str):
            tally[result] += 1
            continue
        found.append((row, result))

    def summary(first: str) -> str:
        parts = [
            first,
            f"{tally[_NO_HEADERS]} {spec.headers_label}",
            f"{tally[_MISSING]} missing on disk",
        ]
        parts.extend(
            f"{tally[reason]} {label}" for reason, label in spec.extra_skips if tally[reason]
        )
        if tally["read_errors"]:
            parts.append(f"{tally['read_errors']} read errors")
        return ", ".join(parts)

    if not args.apply:
        describe = spec.describe(backend)
        for tgt, group in groupby(
            sorted(found, key=lambda f: (f[0]["target"], f[0]["session_id"])),
            key=lambda f: f[0]["target"],
        ):
            print(f"\n{tgt}")
            for row, values in group:
                print(f"  {row['session_id']}: {describe(row, values)}")
        print(f"\n{summary(f'{len(found)} would be set')}; run with --apply to write")
        return

    try:
        written = 0
        for row, values in found:
            if backend.update_session_fields(row["session_id"], **dict(zip(spec.columns, values))):
                written += 1
    except sqlite3.OperationalError as e:
        _exit_catalog_write_error(e)

    print(summary(f"{written} set"))


def _extract_site(row: dict, headers: list) -> tuple[float, float] | str:
    """Modal SITELAT/SITELONG across the session's frames (see sites.session_site).

    A stale or WiFi-geolocated fix on one frame must not decide the whole
    session, so every frame votes and any outlier more than a kilometre off
    is reported on stderr.
    """
    if not headers:
        return _UNREADABLE
    lat, lon = session_site(
        (
            (_parse_site_deg(h.get("SITELAT")), _parse_site_deg(h.get("SITELONG")))
            for h in headers
        ),
        row["session_id"],
    )
    if lat is None:
        return _NO_HEADERS
    return lat, lon


def _describe_site(backend: CatalogBackend):
    sites = backend.list_sites()

    def describe(row: dict, values: tuple) -> str:
        lat, lon = values
        site = resolve_site(lat, lon, sites)
        site_name = site["name"] if site else "(no site in radius)"
        return f"{lat:.4f}, {lon:.4f} -> {site_name}"

    return describe


def _backfill_sites_run(args: argparse.Namespace) -> None:
    """Backfill site_lat/site_lon on sessions from archive FITS SITELAT/SITELONG.

    Dry run (default) is pure-read. --apply writes via update_session_fields.
    Only sessions with a NULL site_lat are ever candidates (idempotent).
    """
    _backfill_run(args, _Backfill(
        null_column="site_lat",
        columns=("site_lat", "site_lon"),
        headers_label="no site headers",
        extract=_extract_site,
        describe=_describe_site,
    ))


_WRONG_NIGHT = "wrong_night"


def _extract_span(row: dict, headers: list) -> tuple[str, str] | str:
    """UTC wall-clock span of the frames on *this session's* imaging night.

    A folder is not a session. Legacy archive layouts (pre-F4) can have two
    session rows pointing at one lights_path, and taking the span over the
    whole folder then hands both of them the same multi-day window — observed
    as a 76-hour "session" that swallowed three nights' guide rows and
    reported an identical bogus RMS for both. Keep only frames whose imaging
    night (cataloger.compute_imaging_night, the noon-to-noon rule the scanner
    groups by) equals the session's obs_date; never fall back to the
    unfiltered folder span. start_utc is the earliest such DATE-OBS; end_utc
    is the latest one's DATE-OBS plus *that* frame's exposure, so the span
    covers the final sub-exposure (F4 intersects guide-log segments against it).
    """
    from darkroom.cataloger import compute_imaging_night, compute_session_span

    stamps = [
        (h.get("DATE-OBS", ""), h.get("EXPOSURE", h.get("EXPTIME", 0.0))) for h in headers
    ]
    nights = [compute_imaging_night(date_obs) for date_obs, _ in stamps]
    on_night = [s for s, night in zip(stamps, nights) if night == row["obs_date"]]
    if not on_night:
        # Nothing dated at all is a header problem; dated frames that all
        # belong to other nights is a layout problem. Both mean no span.
        return _WRONG_NIGHT if any(n is not None for n in nights) else _NO_HEADERS
    start_utc, end_utc = compute_session_span(on_night)
    if start_utc is None:
        return _NO_HEADERS
    return start_utc, end_utc


def _backfill_times_run(args: argparse.Namespace) -> None:
    """Backfill start_utc/end_utc on sessions from archive FITS DATE-OBS/EXPTIME.

    Dry run (default) is pure-read. --apply writes via update_session_fields.
    Only sessions with a NULL start_utc are ever candidates (idempotent).
    """
    _backfill_run(args, _Backfill(
        null_column="start_utc",
        columns=("start_utc", "end_utc"),
        headers_label="no date headers",
        extract=_extract_span,
        describe=lambda backend: (lambda row, values: f"{values[0]} -> {values[1]}"),
        extra_skips=((_WRONG_NIGHT, "skipped (no frames on the session night)"),),
    ))


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

    # Shared --catalog-url/--api-token flags (W9 backend abstraction), on
    # every subcommand that reads or writes through the catalog backend.
    catalog_url_flag = argparse.ArgumentParser(add_help=False)
    catalog_url_flag.add_argument("--catalog-url", metavar="URL",
                                   help="Catalog API base URL (env: DARKROOM_CATALOG_URL)")
    catalog_url_flag.add_argument("--api-token", metavar="TOKEN",
                                   help="Catalog API bearer token (env: DARKROOM_API_TOKEN)")

    sl = sub.add_parser("scan-lights", parents=[catalog_flag, catalog_url_flag],
                        help="Recursively catalog all light sessions")
    sl.add_argument("root_path", help="Root folder to scan (e.g. '01_Deep Sky Objects')")
    sl.set_defaults(func=_scan_lights_run)

    sc = sub.add_parser("scan-calibration", parents=[catalog_flag, catalog_url_flag],
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
        "scan-processed", parents=[catalog_flag, catalog_url_flag],
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

    rs = sub.add_parser(
        "rescan-archive", parents=[catalog_flag, catalog_url_flag],
        help="Diff the archive against the catalog and queue divergences for review",
        description="Strictly read-only pass over <archive>/01_Deep Sky Objects/ "
                    "(the same walk `scan-lights` uses), diffed against the catalog "
                    "(resolve_backend().query_sessions()). Classifies each session_id "
                    "found on either side as matching (no-op), diverging (update), "
                    "on-disk-only (create), or catalog-only (delete) — see BACKLOG.md "
                    "F8. Dry run by default (prints a grouped summary, writes "
                    "nothing). --apply does NOT edit any session row — it pushes the "
                    "findings to the rescan_proposals review queue (superseding the "
                    "previous pending set), for a human (or the queue's pre-approved "
                    "'safe' tier — a pure frame_count/total_integration_sec change) "
                    "to apply from there. Refuses outright if the archive's DSO root "
                    "doesn't exist; warns and requires --yes (or a 'yes' at the "
                    "prompt) if the root exists but 0 sessions are found on disk "
                    "while the catalog is not empty, since taking that at face "
                    "value would delete-propose every session in the catalog.",
    )
    rs.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    rs.add_argument("--pointing-tolerance", type=float, default=0.5, metavar="DEG",
                     help="RA/Dec divergence tolerance in degrees, wrapping RA at "
                          "360 (default: 0.5)")
    rs.add_argument("--apply", action="store_true",
                     help="Push proposals to the review queue (default: dry run, "
                          "read-only; this does NOT write to sessions)")
    rs.add_argument("--yes", action="store_true",
                     help="Skip the confirmation prompt when the disk scan finds "
                          "0 sessions but the catalog is not empty (required for "
                          "non-interactive use in that case)")
    rs.set_defaults(func=_rescan_archive_run)

    sg = sub.add_parser(
        "scan-guiding", parents=[catalog_flag, catalog_url_flag],
        help="Match PHD2 guide logs to sessions by time and store guiding stats",
        description="Parse every PHD2_GuideLog_*.txt in the log directory and "
                    "intersect its guiding segments with each session's stored UTC "
                    "wall-clock span (start_utc/end_utc — run `catalog backfill-times` "
                    "first if those are NULL), pooling the in-window rows into one "
                    "RMS/peak/p95 per session. Matching is by time only; log target "
                    "names are never used. Sessions matching no log, and logs matching "
                    "no session, are reported rather than guessed at. Dry run by "
                    "default (prints what it measured, writes nothing); pass --apply "
                    "to write. Re-running replaces existing rows.",
    )
    sg.add_argument("--logs", metavar="PATH",
                    help="Guide log directory (default: <archive>/00_Logs/ASIAir)")
    sg.add_argument("--settle-exclude", type=float, metavar="SECONDS",
                    default=guidelog.DEFAULT_SETTLE_EXCLUDE_SEC,
                    help="Seconds of guiding discarded after a segment start or a "
                         f"dither (default: {guidelog.DEFAULT_SETTLE_EXCLUDE_SEC:g}). "
                         "A good night barely moves (NGC 281 0.92\" -> 0.90\" going "
                         "from 15s to 120s); a bad one moves a lot (M 45 15.04\" -> "
                         "5.60\"), since on a poor night the dither recoveries ARE "
                         "much of the error. Leave it at the default unless you are "
                         "deliberately probing that sensitivity — the stored numbers "
                         "are only comparable across sessions at one setting.")
    sg.add_argument("--apply", action="store_true",
                    help="Write guiding stats to the catalog (default: dry run, read-only)")
    sg.set_defaults(func=_scan_guiding_run)

    ar = sub.add_parser(
        "apply-renames", parents=[catalog_flag, catalog_url_flag],
        help="Execute pending archive folder renames owed by catalog identity edits",
        description="Read the pending_renames ledger (populated server-side when a "
                    "catalog identity edit changes a session's lights_path — the "
                    "webapi host has no NAS mount, so it can only record the folder "
                    "move it owes) and resolve each entry against the local/mounted "
                    "archive. Dry run by default (prints proposed actions, writes "
                    "nothing); pass --apply to move folders and ack the ledger.",
    )
    ar.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    ar.add_argument("--apply", action="store_true",
                     help="Move folders and ack the ledger (default: dry run, read-only)")
    ar.set_defaults(func=_apply_renames_run)

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
        description="Scan every FITS frame of each session with a NULL site_lat and "
                    "propose setting site_lat/site_lon from the modal SITELAT/SITELONG "
                    "across those frames, warning when frames disagree by more than a "
                    "kilometre. Dry run by default (prints proposed changes, writes "
                    "nothing); pass --apply to write them. Idempotent: only NULL "
                    "site_lat sessions are ever candidates.",
    )
    bf.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    bf.add_argument("--apply", action="store_true",
                     help="Write proposed changes to the catalog (default: dry run, read-only)")
    bf.set_defaults(func=_backfill_sites_run)

    bt = sub.add_parser(
        "backfill-times", parents=site_flags,
        help="Backfill start_utc/end_utc on sessions from archive FITS headers",
        description="Read DATE-OBS/EXPTIME from every FITS frame of each session with "
                    "a NULL start_utc, keep only the frames whose imaging night is "
                    "that session's obs_date (one lights_path folder can hold several "
                    "nights), and propose setting the session's UTC wall-clock span: "
                    "start_utc = earliest DATE-OBS, end_utc = latest DATE-OBS "
                    "plus that frame's exposure. Dry run by default (prints proposed "
                    "changes, writes nothing); pass --apply to write them. Idempotent: "
                    "only NULL start_utc sessions are ever candidates.",
    )
    bt.add_argument("--archive", metavar="PATH", help="Archive root (env: DARKROOM_ARCHIVE)")
    bt.add_argument("--apply", action="store_true",
                     help="Write proposed changes to the catalog (default: dry run, read-only)")
    bt.set_defaults(func=_backfill_times_run)
