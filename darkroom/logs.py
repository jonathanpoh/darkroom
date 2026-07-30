"""darkroom logs — archive ASIAir log files into the NAS archive (F4 step 4).

The ASIAir writes its logs to the SD card, which is rotated and eventually
cleared; the copy CCC leaves on the Mac is equally temporary. This copies the
Autorun and PHD2 guide logs into `<archive>/00_Logs/ASIAir/` so they survive
and can be re-parsed later (`darkroom catalog scan-guiding` reads them there).

Two rules carried over from the rest of the pipeline:

- **The source is never touched.** No deletes, no renames, no rewrites — SD-card
  originals are sacred (see CLAUDE.md). `shutil.copy2` in one direction only.
- **Dry run by default.** Same posture as `catalog scan-processed` /
  `catalog backfill-sites`: printing is free, writing needs `--apply`.

`*_CHN.txt` files are skipped: they are Chinese-language translations of the
identical log content, and account for roughly half the corpus (403 files,
97 MB) for no extra information.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from darkroom.config import resolve_path

#: Log filename prefixes we archive. Autorun logs are archived but not parsed
#: (see the F4 plan); PHD2 guide logs are what `scan-guiding` consumes.
LOG_PREFIXES = ("Autorun_Log_", "PHD2_GuideLog_")

#: Destination, relative to the archive root. Flat — no date subfolders; the
#: ASIAir already puts the timestamp in every filename.
ARCHIVE_SUBDIR = Path("00_Logs") / "ASIAir"


def is_chn(name: str) -> bool:
    """True for the Chinese-translation duplicates (`..._CHN.txt`)."""
    return name.endswith("_CHN.txt")


def is_log_name(name: str) -> bool:
    """True for an ASIAir log filename we care about (translations included)."""
    return name.endswith(".txt") and name.startswith(LOG_PREFIXES)


@dataclass
class ImportPlan:
    """What an import would do. Pure data — building it writes nothing."""

    copy: list[Path] = field(default_factory=list)
    """Sources to copy: new at the destination, or present at a different size."""
    duplicates: list[Path] = field(default_factory=list)
    """Already at the destination with the same size."""
    chn: list[Path] = field(default_factory=list)
    """`_CHN.txt` translations, skipped."""


def plan_import(source: Path, dest: Path) -> ImportPlan:
    """Decide what would be copied from `source` into `dest`, touching neither."""
    plan = ImportPlan()
    for path in sorted(source.iterdir()):
        if not path.is_file() or not is_log_name(path.name):
            continue
        if is_chn(path.name):
            plan.chn.append(path)
            continue
        existing = dest / path.name
        # Size, not mtime: copy2 preserves mtime, so a matching size on a
        # matching name means we already archived this log.
        if existing.is_file() and existing.stat().st_size == path.stat().st_size:
            plan.duplicates.append(path)
        else:
            plan.copy.append(path)
    return plan


def import_logs(plan: ImportPlan, dest: Path) -> int:
    """Copy every file in `plan.copy` into `dest`. Returns the number copied."""
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in plan.copy:
        shutil.copy2(path, dest / path.name)
        copied += 1
    return copied


def _resolve_source(flag_val) -> Path | None:
    """`--source` → DARKROOM_ASIAIR_LOGS → toml, else `<asiair root>/log`."""
    logs = resolve_path(flag_val, "DARKROOM_ASIAIR_LOGS", "asiair_logs_path")
    if logs is not None:
        return logs
    root = resolve_path(None, "DARKROOM_ASIAIR", "asiair_path")
    return root / "log" if root is not None else None


def _import_run(args: argparse.Namespace) -> None:
    source = _resolve_source(args.source)
    if source is None:
        sys.exit(
            "Error: --source / DARKROOM_ASIAIR_LOGS / darkroom.toml asiair_logs_path "
            "required (or asiair_path, whose 'log' subdirectory is used)"
        )
    if not source.is_dir():
        sys.exit(f"Error: log source is not a directory: {source}")

    archive = resolve_path(args.archive, "DARKROOM_ARCHIVE", "archive_path")
    if archive is None:
        sys.exit("Error: --archive / DARKROOM_ARCHIVE / darkroom.toml archive_path required")

    dest = archive / ARCHIVE_SUBDIR
    plan = plan_import(source, dest)

    if not args.apply:
        for path in plan.copy:
            existing = dest / path.name
            note = " (size differs — would overwrite)" if existing.is_file() else ""
            print(f"  {path.name}{note}")
        print(
            f"\n{len(plan.copy)} would be copied to {dest}, "
            f"{len(plan.duplicates)} already archived, "
            f"{len(plan.chn)} _CHN translations skipped; run with --apply to write"
        )
        return

    copied = import_logs(plan, dest)
    print(
        f"{copied} copied to {dest}, "
        f"{len(plan.duplicates)} already archived, "
        f"{len(plan.chn)} _CHN translations skipped"
    )


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "logs",
        help="Archive ASIAir log files (Autorun, PHD2 guide) to the NAS",
    )
    sub = p.add_subparsers(dest="logs_cmd", required=True)

    imp = sub.add_parser(
        "import",
        help="Copy ASIAir logs from the SD-card copy into the archive",
        description="Copy Autorun_Log_*.txt and PHD2_GuideLog_*.txt from the "
                    "ASIAir log directory into <archive>/00_Logs/ASIAir/. "
                    "Skips *_CHN.txt (Chinese translations of the same content) "
                    "and names already archived at the same size. The source is "
                    "only ever read — nothing there is moved, changed or deleted. "
                    "Dry run by default (prints what would be copied, writes "
                    "nothing); pass --apply to copy.",
    )
    imp.add_argument("--source", metavar="PATH",
                     help="ASIAir log directory (env: DARKROOM_ASIAIR_LOGS)")
    imp.add_argument("--archive", metavar="PATH",
                     help="Archive root (env: DARKROOM_ARCHIVE)")
    imp.add_argument("--apply", action="store_true",
                     help="Copy the files (default: dry run, read-only)")
    imp.set_defaults(func=_import_run)

    p.set_defaults(func=lambda args: p.print_help())
