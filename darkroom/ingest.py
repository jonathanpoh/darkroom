"""darkroom.ingest — Copy a completed ASIAir session into canonical archive structure."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from darkroom.cataloger import make_session_id
from darkroom.catalog_client import resolve_backend
from darkroom.config import resolve_catalog, resolve_catalog_url, resolve_path
from darkroom.names import KNOWN_FILTERS, _normalize_camera, session_dest_rel
from darkroom.scanner import CalibrationGroup, Session, ScanResult, scan_source


def _require_path(cli_val, env_var, toml_key, label) -> Path:
    p = resolve_path(cli_val, env_var, toml_key)
    if p is None:
        sys.exit(
            f"Error: --{label} / {env_var} / darkroom.toml {toml_key} required"
        )
    return p


# ---------------------------------------------------------------------------
# Destination path helpers
# ---------------------------------------------------------------------------

def camera_slug(camera: str) -> str:
    """Canonical camera name for folder names (delegates to _normalize_camera)."""
    return _normalize_camera(camera)


def cal_dest_rel(
    frame_type: str, camera: str, ota: str, filter_: str | None, capture_date: str
) -> Path:
    """Return relative destination path for a calibration group's folder."""
    slug = camera_slug(camera)
    if frame_type == "Flat":
        f = filter_ or "NoFilter"
        return Path("00_Calibration") / "Flats" / f"{ota}_{slug}_{f}" / capture_date
    if frame_type == "Dark":
        return Path("00_Calibration") / "Darks" / slug
    if frame_type == "FlatDark":
        return Path("00_Calibration") / "FlatDarks" / slug
    if frame_type == "Bias":
        return Path("00_Calibration") / "Bias" / slug / "Raw"
    raise ValueError(f"Unknown frame type: {frame_type}")


# ---------------------------------------------------------------------------
# Filter prompt
# ---------------------------------------------------------------------------

# KNOWN_FILTERS comes from darkroom.names — the single source of truth shared
# with the webapi cleanup queue (U2) and the `ingest review` prompts (U3). It is
# imported at the top of this module and re-exported here for the callers that
# have always read it off `darkroom.ingest`.


def resolve_filter(
    detected: str | None,
    interactive: bool,
    context: str = "",
) -> tuple[str, bool]:
    """Return (filter_str, needs_review).

    If filter is already detected, returns it directly. If missing and interactive,
    prompts the user. If missing and non-interactive, returns ('NoFilter', True).
    """
    if detected is not None:
        return detected, False

    if not interactive:
        return "NoFilter", True

    if context:
        print(f"\nNo filter detected for: {context}")
    else:
        print("\nNo filter detected.")

    for i, f in enumerate(KNOWN_FILTERS, 1):
        print(f"  {i}) {f}")
    print(f"  {len(KNOWN_FILTERS) + 1}) Enter manually")
    print("  [Enter] NoFilter")

    while True:
        try:
            raw = input("> ").strip()
            if not raw:
                return "NoFilter", False
            n = int(raw)
            if 1 <= n <= len(KNOWN_FILTERS):
                return KNOWN_FILTERS[n - 1], False
            if n == len(KNOWN_FILTERS) + 1:
                manual = input("Filter name: ").strip()
                return (manual or "NoFilter"), False
        except ValueError:
            print("Please enter a number.")
        except EOFError:
            return "NoFilter", False


def infer_flat_filter(
    group: CalibrationGroup,
    sessions: list[Session],
) -> list[str]:
    """Infer flat filter from matching Light sessions by camera, OTA, and date.

    Flats are typically taken the morning after imaging, so we match when the
    flat capture_date equals the session obs_date or obs_date + 1.

    Returns a sorted list of candidate filter names (empty = no match).
    """
    if not group.capture_date:
        return []
    flat_date = Date.fromisoformat(group.capture_date)
    candidates: set[str] = set()
    for sess in sessions:
        if sess.filter is None:
            continue
        if sess.camera != group.camera or sess.ota != group.ota:
            continue
        sess_date = Date.fromisoformat(sess.obs_date)
        delta = (flat_date - sess_date).days
        if 0 <= delta <= 1:
            candidates.add(sess.filter)
    return sorted(candidates)


def _prompt_flat_filter_candidates(
    candidates: list[str],
    group: CalibrationGroup,
    interactive: bool,
) -> tuple[str, bool]:
    """Prompt to choose among inferred filter candidates for a Flat group."""
    context = f"Flat on {group.capture_date} ({group.ota}/{group.camera})"
    if not interactive:
        if len(candidates) == 1:
            return candidates[0], False
        return "NoFilter", True

    print(f"\n{context}: multiple filters found in matching Light sessions:")
    for i, f in enumerate(candidates, 1):
        print(f"  {i}) {f}")
    print("  [Enter] NoFilter")

    while True:
        try:
            raw = input("> ").strip()
            if not raw:
                return "NoFilter", False
            n = int(raw)
            if 1 <= n <= len(candidates):
                return candidates[n - 1], False
        except ValueError:
            print("Please enter a number.")
        except EOFError:
            return "NoFilter", True


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def existing_catalog_sessions(catalog_path: Path) -> dict[str, int]:
    """Return {session_id: frame_count} for all sessions in the catalog.

    A missing file, or one that exists without the schema yet, both mean "no
    sessions known" — not an error. The file can exist un-migrated if anything
    touched the path before the first commit (sqlite creates an empty database
    on connect), and this runs on the unattended CCC postflight path, where an
    OperationalError traceback would abort an otherwise fine ingest. The commit
    itself creates the schema via LocalBackend.
    """
    if not catalog_path.exists():
        return {}
    try:
        with sqlite3.connect(catalog_path) as conn:
            rows = conn.execute(
                "SELECT session_id, frame_count FROM sessions"
            ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {r[0]: r[1] for r in rows}


def catalog_label(catalog: Path) -> str:
    """Which catalog this run is using, for the provenance line.

    A configured `catalog_url` wins for every verb, so the local path is only
    ever in play when no server is set. Saying which one out loud is what makes
    an accidental local run obvious: without it, a postflight that failed to
    pick up `DARKROOM_CATALOG_URL` (a different HOME finds no darkroom.toml)
    archives the frames and registers them in a stale local file, silently,
    while the server never learns the session exists.
    """
    url = resolve_catalog_url()
    return f"{url} (server)" if url else f"{catalog} (local file)"


def report_catalog(catalog: Path) -> None:
    """Print the provenance line.

    Deliberately stderr: `ingest scan` with no --manifest writes the YAML to
    stdout, which has to stay machine-readable. A CCC postflight redirects both
    streams to its log, so it lands there either way.
    """
    print(f"Catalog: {catalog_label(catalog)}", file=sys.stderr)


def catalog_frame_counts(rows: list[dict]) -> dict[str, int]:
    """{session_id: frame_count} from catalog rows, backend-agnostic."""
    return {
        r["session_id"]: r.get("frame_count") or 0
        for r in rows
        if r.get("session_id")
    }


def resolve_catalog_sessions(catalog: Path) -> tuple[dict[str, int], bool]:
    """Return ({session_id: frame_count}, verified) for the dedupe check.

    The new/existing/topup verdict has to be computed against whichever catalog
    `commit` will actually write to. When a `catalog_url` is configured that is
    the server, so this goes through `resolve_backend` — reading the local
    SQLite file instead would report every already-archived session as "new".

    With no server configured it reads the file directly rather than via
    LocalBackend, because LocalBackend ensures the schema on construction and a
    scan must stay read-only on the catalog (same rule `procscan` follows).

    `verified=False` means the server could not be reached: the manifest is
    still written, since a scan is read-only and useful offline, but every
    status in it is a guess and `meta.status_verified` says so.
    """
    if not resolve_catalog_url():
        return existing_catalog_sessions(catalog), True
    try:
        backend = resolve_backend(str(catalog))
        return catalog_frame_counts(backend.query_sessions()), True
    except Exception as exc:  # noqa: BLE001 — any backend failure degrades alike
        print(
            f"Warning: catalog server unreachable ({exc}).\n"
            "         Every session will be reported 'new' and the manifest is "
            "flagged status_verified: false.\n"
            "         Re-run the scan, or check the statuses before committing.",
            file=sys.stderr,
        )
        return {}, False


def make_cal_set_id(
    frame_type: str,
    camera: str,
    gain: int,
    exposure_sec: float,
    temperature_c: float,
    capture_date: str,
) -> str:
    """Build a calibration set primary key."""
    slug = camera_slug(camera)
    temp_str = f"{int(temperature_c)}C"
    return f"{frame_type}_{slug}_{exposure_sec:.3g}s_{gain}g_{temp_str}_{capture_date}"


# ---------------------------------------------------------------------------
# Manifest entry builders
# ---------------------------------------------------------------------------

def plan_session_files(
    srcs: list[Path],
    dest_rel: Path,
    dest_abs: Path,
    session_id: str,
    catalog_sessions: dict[str, int],
) -> tuple[str, list[dict]]:
    """Return (status, files[]) for a session's frames against the catalog.

    Status is "new" (session_id unknown to the catalog), "existing" (known and
    the frame count already matches) or "topup" (known but short some frames).

    Every source frame is listed either way, carrying a per-file `copy` flag —
    `cmd_commit` copies only the flagged ones, and skips "existing" entries
    wholesale. Listing them all (rather than only the ones due to be copied) is
    what lets `ingest review` re-derive this plan after an identity edit changes
    the session_id: the file list survives the round-trip through the manifest.
    """
    existing = catalog_sessions.get(session_id)
    if existing is None:
        status = "new"
        copy_flags = {f.name: True for f in srcs}
    elif existing == len(srcs):
        status = "existing"
        copy_flags = {f.name: False for f in srcs}
    else:
        status = "topup"
        on_disk = (
            {p.name for p in dest_abs.iterdir() if p.is_file()}
            if dest_abs.exists()
            else set()
        )
        copy_flags = {f.name: f.name not in on_disk for f in srcs}

    file_entries = [
        {"src": str(f), "dst": str(dest_rel / f.name), "copy": copy_flags[f.name]}
        for f in sorted(srcs)
    ]
    return status, file_entries


def build_session_entry(
    session: Session,
    output: Path,
    catalog_sessions: dict[str, int],
    interactive: bool,
) -> dict:
    """Build one sessions[] manifest entry for the given Session."""
    filter_, needs_review = resolve_filter(
        session.filter,
        interactive=interactive,
        context=f"{session.target} on {session.obs_date}",
    )

    # Pass None for filter when unknown so make_session_id uses "UnknownFilter"
    session_id = make_session_id(
        session.target,
        session.obs_date,
        session.ota,
        session.camera,
        None if needs_review else filter_,
        panel=session.panel,
    )
    dest_rel = session_dest_rel(
        session.target, session.obs_date, session.ota, session.camera,
        None if needs_review else filter_,
        panel=session.panel,
    )
    dest_abs = output / dest_rel

    status, file_entries = plan_session_files(
        session.files, dest_rel, dest_abs, session_id, catalog_sessions,
    )

    return {
        "session_id": session_id,
        "target": session.target,
        "obs_date": session.obs_date,
        "ota": session.ota,
        "camera": session.camera,
        "filter": None if needs_review else filter_,
        "panel": session.panel,
        "gain": session.gain,
        "temperature_c": session.temperature_c,
        "exposure_sec": session.exposure_sec,
        "focal_length": session.focal_length,
        "frame_count": len(session.files),
        "ra_deg": session.ra_deg,
        "dec_deg": session.dec_deg,
        "site_lat": session.site_lat,
        "site_lon": session.site_lon,
        "start_utc": session.start_utc,
        "end_utc": session.end_utc,
        "needs_review": needs_review,
        "status": status,
        "lights_rel_path": str(dest_rel),
        "files": file_entries,
    }


def build_cal_entry(
    group: CalibrationGroup,
    output: Path,
    interactive: bool,
    sessions: list[Session] | None = None,
) -> dict:
    """Build one calibration[] manifest entry for the given CalibrationGroup."""
    # Filter resolution only matters for Flat frames (FlatDarks are short darks, filter irrelevant)
    if group.frame_type in ("Flat",):
        if group.filter is not None:
            filter_, needs_review = group.filter, False
        else:
            candidates = infer_flat_filter(group, sessions or [])
            if len(candidates) == 1:
                filter_ = candidates[0]
                needs_review = False
                print(f"  Flat {group.capture_date}: inferred filter '{filter_}' from matching Light session", file=sys.stderr)
            elif len(candidates) > 1:
                filter_, needs_review = _prompt_flat_filter_candidates(
                    candidates, group, interactive,
                )
            else:
                filter_, needs_review = resolve_filter(
                    None,
                    interactive=interactive,
                    context=f"{group.frame_type} on {group.capture_date}",
                )
    else:
        filter_ = group.filter
        needs_review = False

    set_id = make_cal_set_id(
        group.frame_type, group.camera, group.gain,
        group.exposure_sec, group.temperature_c, group.capture_date,
    )
    dest_rel = cal_dest_rel(
        group.frame_type, group.camera, group.ota, filter_, group.capture_date
    )
    dest_abs = output / dest_rel

    file_entries = []
    for f in sorted(group.files):
        dest_file = dest_abs / f.name
        file_entries.append({
            "src": str(f),
            "dst": str(dest_rel / f.name),
            "copy": not dest_file.exists(),
        })

    return {
        "set_id": set_id,
        "frame_type": group.frame_type,
        "camera": group.camera,
        "ota": group.ota,
        "filter": None if needs_review else filter_,
        "gain": group.gain,
        "exposure_sec": group.exposure_sec,
        "temperature_c": group.temperature_c,
        "capture_date": group.capture_date,
        "frame_count": len(group.files),
        "needs_review": needs_review,
        "folder_rel_path": str(dest_rel),
        "files": file_entries,
    }


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------

def build_manifest(
    scan: ScanResult,
    source: Path,
    output: Path,
    catalog: Path,
    interactive: bool,
) -> dict:
    """Build the full manifest dict from a ScanResult."""
    catalog_sessions, status_verified = resolve_catalog_sessions(catalog)

    session_entries = [
        build_session_entry(s, output, catalog_sessions, interactive)
        for s in scan.sessions
    ]
    cal_entries = [
        build_cal_entry(g, output, interactive, sessions=scan.sessions)
        for g in scan.calibration
    ]

    return {
        "meta": {
            "asiair": str(source),
            "archive": str(output),
            # Stays the local path: `cmd_commit` feeds it to resolve_backend as
            # the offline fallback, so it must remain a usable filesystem path
            # even when the server is what actually gets written.
            "catalog": str(catalog),
            "catalog_url": resolve_catalog_url(),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            # False = the catalog was unreachable, so every `status` below is a
            # guess ("new") rather than a verdict. See resolve_catalog_sessions.
            "status_verified": status_verified,
        },
        "sessions": session_entries,
        "calibration": cal_entries,
    }


def _manifest_dest(manifest_arg: str) -> tuple[Path, str | None]:
    """Resolve the --manifest output path, defaulting a missing extension to .yaml.

    The manifest is always YAML, so a bare name gets `.yaml` appended and a
    misleading `.json` name returns a warning (the content is not JSON).
    Returns (dest, warning_or_None).
    """
    dest = Path(manifest_arg)
    if dest.suffix == "":
        return dest.with_suffix(".yaml"), None
    if dest.suffix.lower() == ".json":
        return dest, (
            f"Warning: {dest.name} will contain YAML, not JSON "
            "— consider a .yaml/.yml name"
        )
    return dest, None


def cmd_scan(args: argparse.Namespace, *, write_file: bool) -> None:
    """Handle --dry-run and --manifest modes."""
    source = Path(args.asiair)
    output = _require_path(args.archive, "DARKROOM_ARCHIVE", "archive_path", "archive")
    catalog = resolve_catalog(args.catalog)
    interactive = sys.stdin.isatty()

    if not source.exists():
        print(f"Error: source path does not exist: {source}", file=sys.stderr)
        sys.exit(1)

    report_catalog(catalog)
    scan = scan_source(source)
    manifest = build_manifest(scan, source, output, catalog, interactive)

    yaml_str = yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True)

    if write_file:
        dest, warning = _manifest_dest(args.manifest)
        if warning:
            print(warning, file=sys.stderr)
        dest.write_text(yaml_str)
        needs_review = sum(
            1 for e in manifest["sessions"] + manifest["calibration"]
            if e.get("needs_review")
        )
        print(f"Manifest written to {dest}")
        if needs_review:
            print(f"  {needs_review} item(s) need a filter — run: darkroom ingest review {dest}")
        else:
            print(f"  Confirm the parsed values with: darkroom ingest review {dest}")
    else:
        print(yaml_str)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def _run_review(args: argparse.Namespace) -> None:
    """`ingest review` — dispatch to darkroom.ingest_review.

    Imported lazily so `darkroom.ingest` (which the no-TTY commit path lives in)
    doesn't drag in the interactive module, and so ingest_review is free to
    import the manifest builders from here without a circular import.
    """
    from darkroom.ingest_review import cmd_review

    cmd_review(args)


def cmd_commit(args: argparse.Namespace) -> None:
    """Execute a manifest: copy files and register in catalog."""
    if args.manifest is None:
        # No manifest file given — scan and commit in one step
        if not args.asiair:
            print("Error: commit without a manifest file requires --asiair", file=sys.stderr)
            sys.exit(1)
        source = Path(args.asiair)
        output = _require_path(args.archive, "DARKROOM_ARCHIVE", "archive_path", "archive")
        catalog = resolve_catalog(args.catalog)
        interactive = sys.stdin.isatty()
        # Ahead of the scan, so the provenance line precedes anything
        # build_manifest reports about reaching that catalog.
        report_catalog(catalog)
        scan = scan_source(source)
        manifest = build_manifest(scan, source, output, catalog, interactive)
    else:
        manifest_path = Path(args.manifest)
        if not manifest_path.exists():
            print(f"Error: manifest file not found: {manifest_path}", file=sys.stderr)
            sys.exit(1)
        manifest = yaml.safe_load(manifest_path.read_text())
        output = Path(manifest["meta"]["archive"])
        catalog = Path(manifest["meta"]["catalog"])
        # Before any copying or upserting: say where this is about to land.
        report_catalog(catalog)

    # A manifest scanned while the catalog was unreachable carries guessed
    # statuses. Worth saying out loud — the scan-time warning may only have
    # reached a postflight log nobody read — but not worth refusing over: an
    # over-eager "new" re-copies nothing (dst-exists check) and the upsert
    # preserves processed_state and notes.
    if manifest.get("meta", {}).get("status_verified") is False:
        print(
            "Warning: this manifest was scanned while the catalog was "
            "unreachable — 'new'/'existing'/'topup' statuses are unverified.",
            file=sys.stderr,
        )

    # Hard-refuse if any needs_review items remain
    flagged = [
        e.get("session_id") or e.get("set_id")
        for e in manifest.get("sessions", []) + manifest.get("calibration", [])
        if e.get("needs_review")
    ]
    if flagged:
        print("Error: manifest has unresolved needs_review items:", file=sys.stderr)
        for item in flagged:
            print(f"  - {item}", file=sys.stderr)
        print("Run: darkroom ingest review <manifest>", file=sys.stderr)
        sys.exit(1)

    # resolve_backend(str(catalog)) intentionally lets a configured
    # DARKROOM_CATALOG_URL override the manifest/flag-resolved local path
    # (W9: the Mac ingest box goes remote); the local path above is only the
    # offline fallback. LocalBackend ensures the schema itself — no separate
    # init_db call needed.
    backend = resolve_backend(str(catalog))
    files_copied = 0
    files_skipped = 0

    all_entries = manifest.get("sessions", []) + manifest.get("calibration", [])
    total_to_copy = sum(
        1 for e in all_entries
        if e.get("status") != "existing"
        for f in e.get("files", [])
        if f.get("copy")
    )

    # Copy files
    for entry in all_entries:
        if entry.get("status") == "existing":
            continue
        for f in entry.get("files", []):
            if not f.get("copy"):
                files_skipped += 1
                continue
            src = Path(f["src"])
            dst = output / f["dst"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                files_skipped += 1
                continue
            shutil.copy2(src, dst)
            files_copied += 1
            print(f"\rCopying: {files_copied}/{total_to_copy}", end="", flush=True)

    if total_to_copy:
        print()

    # Upsert catalog entries
    catalog_entries = 0
    for entry in manifest.get("sessions", []):
        if entry.get("status") == "existing":
            continue
        backend.upsert_session({
            "session_id": entry["session_id"],
            "target": entry["target"],
            "obs_date": entry["obs_date"],
            "ota": entry["ota"],
            "camera": entry["camera"],
            "filter": entry.get("filter"),
            # .get, not [] — manifests written before M1 have no panel key.
            "panel": entry.get("panel"),
            "gain": entry["gain"],
            "temperature_c": entry["temperature_c"],
            "exposure_sec": entry["exposure_sec"],
            "focal_length": entry.get("focal_length"),
            "frame_count": entry["frame_count"],
            "total_integration_sec": int(entry["frame_count"] * entry["exposure_sec"]),
            "ra_deg": entry.get("ra_deg"),
            "dec_deg": entry.get("dec_deg"),
            "site_lat": entry.get("site_lat"),
            "site_lon": entry.get("site_lon"),
            "start_utc": entry.get("start_utc"),
            "end_utc": entry.get("end_utc"),
            "lights_path": entry["lights_rel_path"],
            "notes": "",
        })
        catalog_entries += 1

    for entry in manifest.get("calibration", []):
        backend.upsert_calibration_set({
            "set_id": entry["set_id"],
            "frame_type": entry["frame_type"],
            "camera": entry["camera"],
            "ota": entry["ota"],
            "filter": entry.get("filter"),
            "gain": entry["gain"],
            "exposure_sec": entry["exposure_sec"],
            "temperature_c": entry["temperature_c"],
            "frame_count": entry["frame_count"],
            "capture_date": entry["capture_date"],
            "folder_path": entry["folder_rel_path"],
        })
        catalog_entries += 1

    print(f"Done: {files_copied} files copied, {files_skipped} skipped, {catalog_entries} catalog entries written")


def _run_scan(args: argparse.Namespace) -> None:
    """`ingest scan` — scan the ASIAir source and emit a manifest.

    No --manifest prints to stdout (dry run); --manifest FILE writes it.
    """
    cmd_scan(args, write_file=args.manifest is not None)


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "ingest",
        help="Archive a completed ASIAir session into the NAS",
        description="Copy ASIAir source files into the canonical archive structure and register sessions in the catalog.",
    )
    verbs = p.add_subparsers(dest="ingest_cmd", required=True)

    # ── scan ──────────────────────────────────────────────────────────────
    scan = verbs.add_parser(
        "scan",
        help="Scan the ASIAir source and emit a manifest",
        description="Scan the ASIAir source; print the manifest to stdout, or write it to a file with --manifest.",
    )
    scan.add_argument("--asiair", required=True, metavar="PATH",
                      help="ASIAir root or Autorun/Plan folder")
    scan.add_argument("--manifest", metavar="FILE",
                      help="Write the manifest to FILE for review (default: print to stdout)")
    scan.add_argument("--archive", metavar="PATH",
                      help="Archive root (env: DARKROOM_ARCHIVE)")
    scan.add_argument("--catalog", metavar="PATH",
                      help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)")
    scan.set_defaults(func=_run_scan)

    # ── review ────────────────────────────────────────────────────────────
    review = verbs.add_parser(
        "review",
        help="Interactively confirm/correct a manifest before committing",
        description="Walk a scanned manifest and confirm or correct the values parsed "
                    "from ASIAir filenames — target, filter, OTA and camera — writing "
                    "the corrections back in place. Needs an interactive terminal.",
    )
    review.add_argument("manifest", metavar="FILE",
                        help="Manifest file to review in place")
    review.add_argument("--flagged-only", action="store_true",
                        help="Only visit entries marked needs_review (missing filter)")
    review.add_argument("--catalog", metavar="PATH",
                        help="astro_catalog.db to source pick-list suggestions from "
                             "(env: DARKROOM_CATALOG)")
    review.set_defaults(func=_run_review)

    # ── commit ────────────────────────────────────────────────────────────
    commit = verbs.add_parser(
        "commit",
        help="Execute a manifest (copy files + register in catalog)",
        description="Execute a manifest: copy files and register sessions. "
                    "With no FILE, scans --asiair and commits in one step.",
    )
    commit.add_argument("manifest", nargs="?", metavar="FILE",
                        help="Manifest to execute (omit to scan + commit directly)")
    commit.add_argument("--asiair", metavar="PATH",
                        help="ASIAir root or Autorun/Plan folder (required when no manifest FILE is given)")
    commit.add_argument("--archive", metavar="PATH",
                        help="Archive root (env: DARKROOM_ARCHIVE)")
    commit.add_argument("--catalog", metavar="PATH",
                        help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)")
    commit.set_defaults(func=cmd_commit)
