"""darkroom.ingest — Copy a completed ASIAir session into canonical archive structure."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from darkroom.cataloger import (
    _normalize_camera,
    init_db,
    make_session_id,
    upsert_calibration_set,
    upsert_session,
)
from darkroom.config import resolve_catalog, resolve_path
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


def session_dest_rel(
    target: str, obs_date: str, ota: str, camera: str, filter_: str | None
) -> Path:
    """Return relative destination path for a session's Lights/<filter>/ folder."""
    f = filter_ or "NoFilter"
    folder = f"{obs_date}_{ota}_{camera_slug(camera)}"
    return Path("04_Deep Sky Objects") / target / folder / "Lights" / f


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

KNOWN_FILTERS = ["L-Pro", "L-Extreme", "L-Synergy", "L-Enhance", "L-Ultimate", "AstronomikL2", "BaaderNeodymium", "OmegonHelievo"]


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


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def existing_catalog_sessions(catalog_path: Path) -> dict[str, int]:
    """Return {session_id: frame_count} for all sessions in the catalog."""
    if not catalog_path.exists():
        return {}
    with sqlite3.connect(catalog_path) as conn:
        rows = conn.execute("SELECT session_id, frame_count FROM sessions").fetchall()
    return {r[0]: r[1] for r in rows}


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
    )
    dest_rel = session_dest_rel(
        session.target, session.obs_date, session.ota, session.camera,
        None if needs_review else filter_,
    )
    dest_abs = output / dest_rel

    existing = catalog_sessions.get(session_id)
    if existing is None:
        status = "new"
        file_entries = [
            {"src": str(f), "dst": str(dest_rel / f.name), "copy": True}
            for f in sorted(session.files)
        ]
    elif existing == len(session.files):
        status = "existing"
        file_entries = []
    else:
        status = "topup"
        existing_names = (
            {p.name for p in dest_abs.iterdir() if p.is_file()}
            if dest_abs.exists()
            else set()
        )
        file_entries = [
            {"src": str(f), "dst": str(dest_rel / f.name), "copy": True}
            for f in sorted(session.files)
            if f.name not in existing_names
        ]

    return {
        "session_id": session_id,
        "target": session.target,
        "obs_date": session.obs_date,
        "ota": session.ota,
        "camera": session.camera,
        "filter": None if needs_review else filter_,
        "gain": session.gain,
        "temperature_c": session.temperature_c,
        "exposure_sec": session.exposure_sec,
        "frame_count": len(session.files),
        "ra_deg": session.ra_deg,
        "dec_deg": session.dec_deg,
        "needs_review": needs_review,
        "status": status,
        "lights_rel_path": str(dest_rel),
        "files": file_entries,
    }


def build_cal_entry(
    group: CalibrationGroup,
    output: Path,
    interactive: bool,
) -> dict:
    """Build one calibration[] manifest entry for the given CalibrationGroup."""
    # Filter resolution only matters for Flat frames (FlatDarks are short darks, filter irrelevant)
    if group.frame_type in ("Flat",):
        filter_, needs_review = resolve_filter(
            group.filter,
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
    catalog_sessions = existing_catalog_sessions(catalog)

    session_entries = [
        build_session_entry(s, output, catalog_sessions, interactive)
        for s in scan.sessions
    ]
    cal_entries = [
        build_cal_entry(g, output, interactive)
        for g in scan.calibration
    ]

    return {
        "meta": {
            "asiair": str(source),
            "archive": str(output),
            "catalog": str(catalog),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
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
            print(f"  {needs_review} item(s) need filter review — run: darkroom ingest review {dest}")
    else:
        print(yaml_str)


# ---------------------------------------------------------------------------
# Stubs for later tasks
# ---------------------------------------------------------------------------

def cmd_review(args: argparse.Namespace) -> None:
    """Interactively resolve needs_review items in a saved manifest file."""
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Error: manifest file not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = yaml.safe_load(manifest_path.read_text())
    changed = False

    for entry in manifest.get("sessions", []) + manifest.get("calibration", []):
        if not entry.get("needs_review"):
            continue

        is_session = "lights_rel_path" in entry
        context = (
            f"{entry['target']} on {entry['obs_date']}"
            if is_session
            else f"{entry['frame_type']} on {entry['capture_date']}"
        )
        filter_, _ = resolve_filter(None, interactive=True, context=context)
        entry["filter"] = filter_
        entry["needs_review"] = False

        if is_session:
            # Recalculate session_id, lights_rel_path, and all file dst paths
            new_session_id = make_session_id(
                entry["target"], entry["obs_date"],
                entry["ota"], entry["camera"], filter_,
            )
            new_dest_rel = session_dest_rel(
                entry["target"], entry["obs_date"],
                entry["ota"], entry["camera"], filter_,
            )
            entry["session_id"] = new_session_id
            entry["lights_rel_path"] = str(new_dest_rel)
            for f in entry.get("files", []):
                f["dst"] = str(new_dest_rel / Path(f["dst"]).name)
        else:
            # Recalculate set_id, folder_rel_path, and all file dst paths
            new_set_id = make_cal_set_id(
                entry["frame_type"], entry["camera"], entry["gain"],
                entry["exposure_sec"], entry["temperature_c"], entry["capture_date"],
            )
            new_dest_rel = cal_dest_rel(
                entry["frame_type"], entry["camera"], entry["ota"],
                filter_, entry["capture_date"],
            )
            entry["set_id"] = new_set_id
            entry["folder_rel_path"] = str(new_dest_rel)
            for f in entry.get("files", []):
                f["dst"] = str(new_dest_rel / Path(f["dst"]).name)

        changed = True

    if changed:
        manifest_path.write_text(
            yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )
        print(f"Updated: {manifest_path}")
    else:
        print("No items needed review.")


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

    init_db(catalog)
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
        upsert_session(catalog, {
            "session_id": entry["session_id"],
            "target": entry["target"],
            "obs_date": entry["obs_date"],
            "ota": entry["ota"],
            "camera": entry["camera"],
            "filter": entry.get("filter"),
            "gain": entry["gain"],
            "temperature_c": entry["temperature_c"],
            "exposure_sec": entry["exposure_sec"],
            "frame_count": entry["frame_count"],
            "total_integration_sec": int(entry["frame_count"] * entry["exposure_sec"]),
            "ra_deg": entry.get("ra_deg"),
            "dec_deg": entry.get("dec_deg"),
            "lights_path": entry["lights_rel_path"],
            "processed_status": "",
            "notes": "",
        })
        catalog_entries += 1

    for entry in manifest.get("calibration", []):
        upsert_calibration_set(catalog, {
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
        help="Interactively resolve needs_review items in a manifest",
        description="Interactively resolve needs_review (missing-filter) items in a saved manifest.",
    )
    review.add_argument("manifest", metavar="FILE",
                        help="Manifest file to review in place")
    review.set_defaults(func=cmd_review)

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
