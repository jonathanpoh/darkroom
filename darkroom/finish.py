"""darkroom.finish — Copy WBPP stacks back to the NAS archive and clean up working dirs."""
from __future__ import annotations

import argparse
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

from darkroom.catalog_client import CatalogBackend, resolve_backend
from darkroom.config import require_archive, resolve_path
from darkroom.names import (
    panel_sort_key,
    parse_wbpp_panel_dir,
    processed_panel_dir,
    target_slug,
)


# ── core helpers ──────────────────────────────────────────────────────────────

def _find_processing_date(dirs: list[Path], override: str | None) -> str:
    """Return YYYY-MM-DD for the _Processed/<date>/ folder name.

    If override is given (--date), use it verbatim. Otherwise return the
    latest mtime across files in every directory in *dirs* — captures the
    most recent processing activity, whether WBPP-only or with hand finishing.

    Takes a list (not a fixed master/processed pair) so a mosaic can fold in
    every panel's master/+processed/ plus the target-level processed/ and
    derive one shared date — the archive holds one dated _Processed/<date>/
    per mosaic, not one per panel (BACKLOG.md M3).
    """
    if override:
        return override
    times: list[float] = []
    for d in dirs:
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    times.append(f.stat().st_mtime)
    if not times:
        joined = ", ".join(str(d) for d in dirs)
        sys.exit(f"No files in {joined} — cannot derive date")
    return datetime.fromtimestamp(max(times)).date().isoformat()


def _build_dest(output: Path, target: str, date_str: str) -> Path:
    """Return <output>/01_Deep Sky Objects/<target>/_Processed/<date_str>."""
    return output / "01_Deep Sky Objects" / target / "_Processed" / date_str


def _collect_light_dirs(wbpp_target: Path) -> set[Path]:
    """Return the resolved archive directories the Lights symlinks point into.

    Each SESSION_N/Lights/** symlink resolves to a light frame inside the
    archive; its parent directory is the session's stored ``lights_path``. We
    compare those directories — not a fixed number of ``.parent`` hops — so the
    match is agnostic to how many components sit between the archive root and the
    frames (e.g. the ``Lights/<filter>/`` split added when sessions were split by
    filter).
    """
    dirs: set[Path] = set()
    for session_dir in wbpp_target.glob("SESSION_*"):
        if not session_dir.is_dir():
            continue
        for symlink in (session_dir / "Lights").rglob("*"):
            if not symlink.is_symlink():
                continue
            try:
                resolved = symlink.resolve(strict=True)
            except FileNotFoundError:
                continue
            dirs.add(resolved.parent)
    return dirs


def _resolve_session_ids(
    wbpp_target: Path, backend: CatalogBackend, archive_root: Path
) -> list[str]:
    """Look up catalog session_ids for the lights symlinked under wbpp_target.

    Matches each Lights symlink's resolved archive directory against the
    catalog's stored ``lights_path`` (resolved under ``archive_root``).
    Fetches every session via ``backend.query_sessions()`` and filters
    client-side for a non-null ``lights_path`` — the catalog is ~200 rows,
    and there's no server-side "lights_path is not null" filter.

    ``wbpp_target`` is whichever directory holds the SESSION_* dirs to
    resolve — the WBPP target root for a non-mosaic target, or one panel dir
    for a mosaic (each panel has its own SESSION_* numbering).
    """
    light_dirs = _collect_light_dirs(wbpp_target)
    if not light_dirs:
        return []
    rows = [r for r in backend.query_sessions() if r.get("lights_path") is not None]
    ids: list[str] = []
    for row in rows:
        if (archive_root / row["lights_path"]).resolve() in light_dirs:
            ids.append(row["session_id"])
    return sorted(set(ids))


def _mark_sessions_processed(
    wbpp_targets: list[Path],
    archive_root: Path,
    status: str,
    date_str: str,
    backend: CatalogBackend,
    *,
    state: str = "processed",
) -> None:
    """Mark every session resolved from wbpp_targets as *state*.

    Takes a list (not one dir) so a mosaic merge can resolve sessions across
    every panel dir at once — the merged mosaic marks all panels' sessions
    together, since none of them is individually finished (BACKLOG.md M3). A
    non-mosaic or single-panel caller just passes a one-element list.

    Sets the structured processed_state=*state*, with processed_path=status
    (the archive-relative _Processed/<date>/ path) and processed_date=date_str.
    Both the read (_resolve_session_ids) and the write go through ``backend``.
    """
    session_ids = sorted({
        sid
        for wbpp_target in wbpp_targets
        for sid in _resolve_session_ids(wbpp_target, backend, archive_root)
    })
    if not session_ids:
        print("\nWarning: no catalog sessions matched symlinks — nothing to mark.")
        return
    print(f"\nMarking {len(session_ids)} session(s) as {state}:")
    for sid in session_ids:
        ok = backend.set_processed_state(
            sid, state=state, processed_path=status, processed_date=date_str
        )
        mark = "✓" if ok else "✗ (not found)"
        print(f"  {mark} {sid}")


def _preview_mark(wbpp_targets: list[Path], backend: CatalogBackend, archive_root: Path, *, state: str) -> None:
    """--dry-run counterpart to _mark_sessions_processed: read-only, prints what would happen."""
    session_ids = sorted({
        sid
        for wbpp_target in wbpp_targets
        for sid in _resolve_session_ids(wbpp_target, backend, archive_root)
    })
    if session_ids:
        print(f"\n[dry-run] would mark {len(session_ids)} session(s) as {state}:")
        for sid in session_ids:
            print(f"  {sid}")
    else:
        print(
            "\n[dry-run] WARNING: no catalog sessions matched the SESSION_N "
            "symlinks — a real run would copy files but mark nothing. "
            "Check --archive points at the archive the symlinks resolve into."
        )


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


# ── mosaic helpers ────────────────────────────────────────────────────────────

def _panel_dirs(wbpp_target: Path) -> dict[str, Path]:
    """Return {panel_label: dir} for every PANEL_* dir directly under wbpp_target.

    Empty for a non-mosaic target — the presence of any PANEL_* dir is exactly
    how a mosaic is detected (BACKLOG.md M3): no grouping keyword can keep WBPP
    from merging panels at final integration, so each panel got its own tree.
    """
    panels: dict[str, Path] = {}
    for p in wbpp_target.iterdir():
        if not p.is_dir():
            continue
        label = parse_wbpp_panel_dir(p.name)
        if label is not None:
            panels[label] = p
    return panels


def _require_output(wbpp_output: Path, master_dir: Path, where: str) -> None:
    """Shared existence checks for one WBPP Output/ dir — target-level or one panel's."""
    if not wbpp_output.exists():
        sys.exit(f"Output/ not found in {where} — did you set the WBPP output dir correctly?")
    if not master_dir.exists():
        sys.exit(f"master/ not found in {wbpp_output}")


# ── cleanup helpers ──────────────────────────────────────────────────────────

def _list_session_dirs(wbpp_target_dir: Path) -> list[Path]:
    """Return existing SESSION_N dirs inside wbpp_target_dir."""
    return sorted(
        p for p in wbpp_target_dir.iterdir()
        if p.is_dir() and re.fullmatch(r"SESSION_\d+", p.name)
    )


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

def _finish_simple(
    wbpp_target: Path, *, output: Path, target: str, backend: CatalogBackend,
    date_str: str, dry_run: bool,
) -> None:
    """Finish a non-mosaic target — today's exact single-tree behavior, unchanged."""
    wbpp_output = wbpp_target / "Output"
    master_dir = wbpp_output / "master"
    processed_dir = wbpp_output / "processed"
    _require_output(wbpp_output, master_dir, str(wbpp_target))

    dest = _build_dest(output, target, date_str)
    print(f"Destination: {dest}")

    if not any(f.is_file() for f in master_dir.iterdir()):
        sys.exit(f"Error: no files in {master_dir} — aborting (nothing to archive)")
    print("\nCopying master/")
    # _copy_flat returns only the count of NEW copies — zero is fine on a
    # re-run (everything already archived), so don't treat it as an error.
    _copy_flat(master_dir, dest / "master", dry_run=dry_run)

    processed_files = (
        [f for f in processed_dir.iterdir() if f.is_file()] if processed_dir.exists() else []
    )
    if not processed_files:
        print("\nWarning: processed/ is empty — skipping")
    else:
        print("\nCopying processed/")
        _copy_flat(processed_dir, dest / "processed", dry_run=dry_run)

    # Log folders ride along when present: WBPP's logs/ records exactly which
    # frames went into the stack (the F2 attribution source), and asiair_logs/
    # (hand-collected) holds the Autorun/PHD2 guide logs (the F4 source).
    for log_name in ("logs", "asiair_logs"):
        log_dir = wbpp_output / log_name
        if log_dir.exists() and any(f.is_file() for f in log_dir.iterdir()):
            print(f"\nCopying {log_name}/")
            _copy_flat(log_dir, dest / log_name, dry_run=dry_run)

    status = str(dest.relative_to(output))
    if dry_run:
        # Resolution is read-only, and it's the step most sensitive to a wrong
        # --archive root — surface it in the dry run instead of skipping it.
        _preview_mark([wbpp_target], backend, output, state="processed")
    else:
        _mark_sessions_processed([wbpp_target], output, status, date_str, backend, state="processed")

    _confirm_and_delete(
        [wbpp_output] if wbpp_output.exists() else [],
        "WBPP Output/ directory to delete (intermediates + master + processed)",
        dry_run=dry_run,
    )
    _confirm_and_delete(
        _list_session_dirs(wbpp_target),
        "SESSION_N directories to delete",
        dry_run=dry_run,
    )


def _finish_panel(
    panel_dir: Path, label: str, *, output: Path, target: str, backend: CatalogBackend,
    date_str: str, dry_run: bool,
) -> None:
    """Finish one mosaic panel: its Output/ -> _Processed/<date>/<label>/, sessions in_progress.

    Not `processed` — a stacked-but-unmerged panel is exactly what in_progress
    means ("stacked and/or editing, no final export yet"). The panel's sessions
    only flip to processed once the mosaic-level merge lands, and they flip
    together with every other panel's (see _finish_mosaic_merge).
    """
    print(f"\n── Panel {label} ──")
    wbpp_output = panel_dir / "Output"
    master_dir = wbpp_output / "master"
    processed_dir = wbpp_output / "processed"
    _require_output(wbpp_output, master_dir, str(panel_dir))

    dest = _build_dest(output, target, date_str) / processed_panel_dir(label)
    print(f"Destination: {dest}")

    if not any(f.is_file() for f in master_dir.iterdir()):
        sys.exit(f"Error: no files in {master_dir} — aborting (nothing to archive for panel {label})")
    print("\nCopying master/")
    _copy_flat(master_dir, dest / "master", dry_run=dry_run)

    processed_files = (
        [f for f in processed_dir.iterdir() if f.is_file()] if processed_dir.exists() else []
    )
    if not processed_files:
        print("\nWarning: processed/ is empty — skipping")
    else:
        print("\nCopying processed/")
        _copy_flat(processed_dir, dest / "processed", dry_run=dry_run)

    for log_name in ("logs", "asiair_logs"):
        log_dir = wbpp_output / log_name
        if log_dir.exists() and any(f.is_file() for f in log_dir.iterdir()):
            print(f"\nCopying {log_name}/")
            _copy_flat(log_dir, dest / log_name, dry_run=dry_run)

    status = str(dest.relative_to(output))
    if dry_run:
        _preview_mark([panel_dir], backend, output, state="in_progress")
    else:
        _mark_sessions_processed([panel_dir], output, status, date_str, backend, state="in_progress")

    _confirm_and_delete(
        [wbpp_output] if wbpp_output.exists() else [],
        f"Panel {label} WBPP Output/ directory to delete (intermediates + master + processed)",
        dry_run=dry_run,
    )
    _confirm_and_delete(
        _list_session_dirs(panel_dir),
        f"Panel {label} SESSION_N directories to delete",
        dry_run=dry_run,
    )


def _finish_mosaic_merge(
    wbpp_target: Path, panel_dirs: list[Path], *, output: Path, target: str,
    backend: CatalogBackend, date_str: str, dry_run: bool,
) -> None:
    """Finish the merged mosaic: target-level Output/processed/ -> _Processed/<date>/ top level.

    The merge happens by hand in PixInsight — WBPP never merges non-overlapping
    panels, which is the whole reason each panel got its own tree — so there is
    no master/ here, only whatever the merge was saved as under processed/.
    Every panel's sessions flip to `processed` together here, because the
    merged image is not any single panel's output (BACKLOG.md M3).
    """
    wbpp_output = wbpp_target / "Output"
    processed_dir = wbpp_output / "processed"
    processed_files = (
        [f for f in processed_dir.iterdir() if f.is_file()] if processed_dir.exists() else []
    )
    if not processed_files:
        print(
            "\nMerged mosaic not found — target-level Output/processed/ is empty. "
            "This is the normal state between stacking the panels and merging them "
            "by hand in PixInsight, not an error; sessions stay in_progress until "
            "you save the merge there and re-run finish."
        )
        return

    print("\n── Merged mosaic ──")
    dest = _build_dest(output, target, date_str)
    print(f"Destination: {dest}")
    print("\nCopying processed/")
    _copy_flat(processed_dir, dest / "processed", dry_run=dry_run)

    for log_name in ("logs", "asiair_logs"):
        log_dir = wbpp_output / log_name
        if log_dir.exists() and any(f.is_file() for f in log_dir.iterdir()):
            print(f"\nCopying {log_name}/")
            _copy_flat(log_dir, dest / log_name, dry_run=dry_run)

    status = str(dest.relative_to(output))
    if dry_run:
        _preview_mark(panel_dirs, backend, output, state="processed")
    else:
        _mark_sessions_processed(panel_dirs, output, status, date_str, backend, state="processed")

    _confirm_and_delete(
        [wbpp_output] if wbpp_output.exists() else [],
        "Target-level WBPP Output/ directory to delete (merged mosaic)",
        dry_run=dry_run,
    )


def cmd_finish(
    *,
    output: Path,
    wbpp_root: Path,
    target: str,
    backend: CatalogBackend,
    date_override: str | None,
    dry_run: bool,
    panel: str | None = None,
) -> None:
    slug = target_slug(target)
    wbpp_target = wbpp_root / slug
    if not wbpp_target.exists():
        sys.exit(f"WBPP target dir not found: {wbpp_target}")

    panels = _panel_dirs(wbpp_target)

    if panel is not None:
        if not panels:
            sys.exit(f"--panel given but {wbpp_target} has no PANEL_* subdirectories (not a mosaic)")
        if panel not in panels:
            avail = ", ".join(sorted(panels, key=panel_sort_key))
            sys.exit(f"Panel {panel!r} not found under {wbpp_target} — available: {avail}")
        panel_dir = panels[panel]
        panel_output = panel_dir / "Output"
        date_str = _find_processing_date(
            [panel_output / "master", panel_output / "processed"], date_override
        )
        _finish_panel(panel_dir, panel, output=output, target=target, backend=backend,
                       date_str=date_str, dry_run=dry_run)
        return

    if not panels:
        wbpp_output = wbpp_target / "Output"
        date_str = _find_processing_date(
            [wbpp_output / "master", wbpp_output / "processed"], date_override
        )
        _finish_simple(wbpp_target, output=output, target=target, backend=backend,
                        date_str=date_str, dry_run=dry_run)
        return

    # Mosaic, full run: one WBPP run per panel (BACKLOG.md M3 — no grouping
    # keyword survives final integration), plus a target-level Output/ for the
    # merge. All of it shares one _Processed/<date>/ — the date the whole
    # mosaic files under, not one date per panel — so derive it from every
    # panel's master/+processed/ together with the target-level processed/.
    labels = sorted(panels, key=panel_sort_key)
    print(f"Mosaic detected: {len(labels)} panel(s) — {', '.join(labels)}")
    dirs = [wbpp_target / "Output" / "processed"]
    for label in labels:
        panel_output = panels[label] / "Output"
        dirs += [panel_output / "master", panel_output / "processed"]
    date_str = _find_processing_date(dirs, date_override)

    for label in labels:
        _finish_panel(panels[label], label, output=output, target=target, backend=backend,
                       date_str=date_str, dry_run=dry_run)

    _finish_mosaic_merge(
        wbpp_target, [panels[label] for label in labels], output=output, target=target,
        backend=backend, date_str=date_str, dry_run=dry_run,
    )


# ── argument parsing ──────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    """Entry point invoked by darkroom.cli."""
    output = require_archive(args.archive)

    wbpp_root = resolve_path(args.wbpp, "DARKROOM_WBPP", "wbpp_path") or Path("./WBPP")

    cmd_finish(
        output=output,
        wbpp_root=wbpp_root,
        target=args.target,
        backend=resolve_backend(args.catalog),
        date_override=args.date,
        dry_run=args.dry_run,
        panel=args.panel,
    )


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "finish",
        help="Copy WBPP stacks to the archive and mark sessions processed",
        description="Copy master/ and processed/ to <archive>/01_Deep Sky Objects/<target>/_Processed/<date>/, then mark each session as processed in the catalog.",
    )
    p.add_argument("--target", metavar="NAME", required=True, help='Target name (e.g. "M 81")')
    p.add_argument("--archive", metavar="PATH",
                   help="Archive root (env: DARKROOM_ARCHIVE)")
    p.add_argument("--catalog", metavar="PATH",
                   help="astro_catalog.db (env: DARKROOM_CATALOG, default: ~/.config/darkroom/astro_catalog.db)")
    p.add_argument("--wbpp", metavar="PATH",
                   help="Root for WBPP target dirs (env: DARKROOM_WBPP, default: ./WBPP)")
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="Name the _Processed/<date>/ output folder (default: derived "
                        "from WBPP output mtimes). Does NOT select a night — finish "
                        "always processes the whole WBPP target.")
    p.add_argument("--panel", metavar="N-M",
                   help='Finish only this mosaic panel (e.g. "1-1"), marking its sessions '
                        "in_progress. Without this flag, a mosaic target finishes every "
                        "panel and then, if the merged mosaic is present in the target-level "
                        "Output/processed/, marks every panel's sessions processed.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be copied/deleted without making changes")
    p.set_defaults(func=run)
