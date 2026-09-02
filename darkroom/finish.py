"""darkroom.finish — Copy WBPP stacks back to the archive and clean up working dirs."""
from __future__ import annotations

import argparse
import sys
import shutil
from datetime import datetime
from pathlib import Path

from darkroom.catalog_client import CatalogBackend, resolve_backend
from darkroom.config import require_archive, resolve_path
from darkroom.names import (
    DSO_DIRNAME,
    panel_sort_key,
    parse_wbpp_panel_dir,
    processed_panel_dir,
    target_slug,
)
from darkroom.wbpp import session_dirs


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
    """Return <output>/<DSO_DIRNAME>/<target>/_Processed/<date_str>."""
    return output / DSO_DIRNAME / target / "_Processed" / date_str


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


def _lights_index(backend: CatalogBackend, archive_root: Path) -> dict[Path, list[str]]:
    """{resolved lights dir: [session_id, ...]} for every catalog session with a lights_path.

    Built once per finish run and handed to every `_resolve_session_ids` call:
    a mosaic resolves sessions once per panel and once more for the merge, and
    the catalog does not change in between. Fetches every session via
    ``backend.query_sessions()`` and filters client-side — the catalog is ~250
    rows, and there's no server-side "lights_path is not null" filter. A list
    per dir, not one id: legacy layouts can have two session rows on one folder.
    """
    index: dict[Path, list[str]] = {}
    for row in backend.query_sessions():
        if row.get("lights_path") is not None:
            key = (archive_root / row["lights_path"]).resolve()
            index.setdefault(key, []).append(row["session_id"])
    return index


def _resolve_session_ids(wbpp_targets: list[Path], index: dict[Path, list[str]]) -> list[str]:
    """Catalog session_ids for the lights symlinked under every dir in wbpp_targets.

    Matches each Lights symlink's resolved archive directory against the
    catalog's stored ``lights_path`` (see `_lights_index`). Each entry of
    ``wbpp_targets`` is a directory holding SESSION_* dirs — the WBPP target
    root for a non-mosaic target, or one panel dir for a mosaic (each panel has
    its own SESSION_* numbering). Takes a list so a mosaic merge can resolve
    sessions across every panel dir at once: the merged mosaic marks all
    panels' sessions together, since none of them is individually finished
    (BACKLOG.md M3).
    """
    return sorted({
        sid
        for wbpp_target in wbpp_targets
        for light_dir in _collect_light_dirs(wbpp_target)
        for sid in index.get(light_dir, ())
    })


def _mark_sessions(
    session_ids: list[str], backend: CatalogBackend, status: str, date_str: str,
    *, state: str, dry_run: bool,
) -> None:
    """Mark every session in session_ids as *state*, or print what a real run would mark.

    Sets the structured processed_state=*state*, with processed_path=status
    (the archive-relative _Processed/<date>/ path) and processed_date=date_str.
    The dry run is read-only; resolution is the step most sensitive to a wrong
    --archive root, so its result is surfaced there instead of skipped.
    """
    if not session_ids:
        if dry_run:
            print(
                "\n[dry-run] WARNING: no catalog sessions matched the SESSION_N "
                "symlinks — a real run would copy files but mark nothing. "
                "Check --archive points at the archive the symlinks resolve into."
            )
        else:
            print("\nWarning: no catalog sessions matched symlinks — nothing to mark.")
        return
    if dry_run:
        print(f"\n[dry-run] would mark {len(session_ids)} session(s) as {state}:")
        for sid in session_ids:
            print(f"  {sid}")
        return
    print(f"\nMarking {len(session_ids)} session(s) as {state}:")
    for sid in session_ids:
        ok = backend.set_processed_state(
            sid, state=state, processed_path=status, processed_date=date_str
        )
        mark = "✓" if ok else "✗ (not found)"
        print(f"  {mark} {sid}")


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


def _has_files(d: Path) -> bool:
    return d.exists() and any(f.is_file() for f in d.iterdir())


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

def _finish_tree(
    wbpp_output: Path,
    dest: Path,
    *,
    session_ids: list[str],
    state: str,
    require_master: bool,
    session_dirs_root: Path | None,
    label: str,
    backend: CatalogBackend,
    archive_root: Path,
    date_str: str,
    dry_run: bool,
) -> None:
    """Archive one WBPP Output/ tree to *dest*, mark *session_ids* as *state*, offer cleanup.

    The one body behind every finish shape. A non-mosaic target, one mosaic
    panel and the hand-merged mosaic differ only in:

    - ``require_master``: a stacked tree must have a non-empty master/; the
      merge, saved by hand in PixInsight, has only processed/ (WBPP never
      merges non-overlapping panels — that is why each panel got its own tree).
    - ``session_dirs_root``: whose SESSION_N dirs to offer for deletion after
      Output/. None for the merge — each panel's were offered when it finished.
    - ``label``: prefix for the cleanup prompts ("", "Panel 1-1 ", "Target-level ").

    Log folders ride along when present: WBPP's logs/ records exactly which
    frames went into the stack (the F2 attribution source), and asiair_logs/
    (hand-collected) holds the Autorun/PHD2 guide logs (the F4 source).
    """
    if require_master:
        master_dir = wbpp_output / "master"
        _require_output(wbpp_output, master_dir, str(wbpp_output.parent))
        if not _has_files(master_dir):
            sys.exit(f"Error: no files in {master_dir} — aborting (nothing to archive)")
    print(f"Destination: {dest}")

    # _copy_flat returns only the count of NEW copies — zero is fine on a
    # re-run (everything already archived), so don't treat it as an error.
    for name in ("master", "processed", "logs", "asiair_logs"):
        src = wbpp_output / name
        if _has_files(src):
            print(f"\nCopying {name}/")
            _copy_flat(src, dest / name, dry_run=dry_run)
        elif name == "processed":
            print("\nWarning: processed/ is empty — skipping")

    status = str(dest.relative_to(archive_root))
    _mark_sessions(session_ids, backend, status, date_str, state=state, dry_run=dry_run)

    contents = "intermediates + master + processed" if require_master else "merged mosaic"
    _confirm_and_delete(
        [wbpp_output] if wbpp_output.exists() else [],
        f"{label}WBPP Output/ directory to delete ({contents})",
        dry_run=dry_run,
    )
    if session_dirs_root is not None:
        _confirm_and_delete(
            session_dirs(session_dirs_root), f"{label}SESSION_N directories to delete",
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
        labels = [panel]
    else:
        labels = sorted(panels, key=panel_sort_key)
        if labels:
            print(f"Mosaic detected: {len(labels)} panel(s) — {', '.join(labels)}")

    # Everything in one run shares one _Processed/<date>/ — for a mosaic, the
    # date the whole mosaic files under, not one per panel (BACKLOG.md M3) —
    # so derive it from every Output/ tree in play. A tree with no master/
    # (the target-level merge) just contributes its processed/.
    outputs = [panels[label] / "Output" for label in labels]
    if panel is None:
        outputs.append(wbpp_target / "Output")
    date_str = _find_processing_date(
        [o / sub for o in outputs for sub in ("master", "processed")], date_override
    )
    dest = _build_dest(output, target, date_str)
    index = _lights_index(backend, output)
    common = dict(backend=backend, archive_root=output, date_str=date_str, dry_run=dry_run)

    if not panels:
        _finish_tree(
            wbpp_target / "Output", dest,
            session_ids=_resolve_session_ids([wbpp_target], index), state="processed",
            require_master=True, session_dirs_root=wbpp_target, label="", **common,
        )
        return

    # Mosaic: one WBPP run per panel (no grouping keyword survives final
    # integration), each archived under its own _Processed/<date>/<panel>/ and
    # its sessions left in_progress — a stacked-but-unmerged panel is exactly
    # what in_progress means ("stacked and/or editing, no final export yet").
    for label in labels:
        print(f"\n── Panel {label} ──")
        _finish_tree(
            panels[label] / "Output", dest / processed_panel_dir(label),
            session_ids=_resolve_session_ids([panels[label]], index), state="in_progress",
            require_master=True, session_dirs_root=panels[label], label=f"Panel {label} ",
            **common,
        )
    if panel is not None:
        return

    # The merge happens by hand in PixInsight and lands in the target-level
    # Output/processed/. Every panel's sessions flip to `processed` together
    # here, because the merged image is not any single panel's output.
    merge_output = wbpp_target / "Output"
    if not _has_files(merge_output / "processed"):
        print(
            "\nMerged mosaic not found — target-level Output/processed/ is empty. "
            "This is the normal state between stacking the panels and merging them "
            "by hand in PixInsight, not an error; sessions stay in_progress until "
            "you save the merge there and re-run finish."
        )
        return
    print("\n── Merged mosaic ──")
    _finish_tree(
        merge_output, dest,
        session_ids=_resolve_session_ids([panels[label] for label in labels], index),
        state="processed", require_master=False, session_dirs_root=None,
        label="Target-level ", **common,
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
