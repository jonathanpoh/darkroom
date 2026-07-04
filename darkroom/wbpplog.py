"""darkroom.wbpplog — parse PixInsight WBPP run logs for exact session→edit
attribution (F2).

Strictly read-only: every function here only reads files (open/stat), never
writes/moves/deletes. Stdlib + darkroom.parse only — no astropy, no catalog/DB
access — so this module (and anything that imports only this module) stays
importable without pulling in the heavy FITS-header stack.

A WBPP "run" is a folder that directly contains a ``logs/`` subdirectory
holding one or more ``*.log`` files. Each log lists every input frame it
stacked/processed by absolute (old, now-stale) path; light frames are
reliably distinguished from calibration by a ``Light`` basename prefix
(``Dark_``/``Flat_`` are calibration). The embedded ``YYYYMMDD-HHMMSS`` in a
light frame's filename is its local capture time — light frames are always
shot at night, never near local noon, so the imaging night it belongs to is
unambiguous via the noon rule in ``_night_from_local_dt``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from darkroom.parse import parse_datetime

# Mirrors darkroom.procscan._EXPORT_EXTS. Duplicated rather than imported:
# procscan imports this module, so importing the other way would cycle.
_EXPORT_EXTS = frozenset({".tif", ".tiff", ".jpg", ".jpeg", ".png", ".psd", ".psb"})

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Characters trimmed from a whitespace-split token's ends so bracketed
# ("[000] /path/...") or comma/paren-adjacent paths still match cleanly.
_TOKEN_STRIP = "[](),;:"


def _night_from_local_dt(dt: datetime) -> str:
    """Map a local capture datetime to its imaging-night date (noon rule).

    Light frames are never captured near local noon, so a capture before
    noon belongs to the night that began the previous calendar day; a
    capture at/after noon belongs to the night beginning that same day.
    """
    night = dt.date() - timedelta(days=1) if dt.hour < 12 else dt.date()
    return night.isoformat()


def parse_log_nights(log_path: Path) -> set[str]:
    """Return the set of imaging nights (YYYY-MM-DD) a WBPP log references.

    Reads the log tolerant of encoding errors (``errors="replace"``) and
    scans for whitespace/quote-delimited tokens whose basename starts with
    'Light' (case-insensitive) and ends in .fit/.fits. The log's own path
    components are stale staging paths and are ignored entirely — only the
    basename's embedded capture timestamp is used. Tokens that don't parse
    to a datetime (``darkroom.parse.parse_datetime``) are skipped, not
    raised on.
    """
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return set()

    nights: set[str] = set()
    for raw in re.split(r'[\s"\']+', text):
        token = raw.strip(_TOKEN_STRIP)
        if not token:
            continue
        basename = token.replace("\\", "/").rsplit("/", 1)[-1]
        lower = basename.lower()
        if not lower.startswith("light"):
            continue
        if lower.endswith(".fits"):
            stem = basename[: -len(".fits")]
        elif lower.endswith(".fit"):
            stem = basename[: -len(".fit")]
        else:
            continue
        dt = parse_datetime(stem)
        if dt is None:
            continue
        nights.add(_night_from_local_dt(dt))
    return nights


@dataclass
class RunEvidence:
    """One WBPP run folder's log-derived processing evidence."""

    run_dir: Path
    edit_date: str | None
    nights: frozenset[str]
    has_export: bool


def _walk_no_lights(root: Path):
    """os.walk(root), pruning any 'Lights'/'lights' subtree (case-insensitive)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() != "lights"]
        if Path(dirpath).name.lower() == "lights":
            continue
        yield dirpath, dirnames, filenames


def _has_export(run_dir: Path) -> bool:
    """True if run_dir's subtree contains a final-export file (F1's rules)."""
    for dirpath, _dirnames, filenames in _walk_no_lights(run_dir):
        for fname in filenames:
            if "_thn" in fname.lower():
                continue
            if Path(fname).suffix.lower() in _EXPORT_EXTS:
                return True
    return False


def _edit_date_for_run(run_dir: Path, archive_root: Path, log_files: list[Path]) -> str | None:
    """A YYYY-MM-DD found in run_dir's path (relative to archive_root), else
    the newest mtime date among the run's exports/logs."""
    try:
        rel_parts = run_dir.relative_to(archive_root).parts
    except ValueError:
        rel_parts = run_dir.parts
    for part in rel_parts:
        m = _DATE_RE.search(part)
        if m:
            return m.group(0)

    mtimes: list[float] = []
    for lf in log_files:
        try:
            mtimes.append(lf.stat().st_mtime)
        except OSError:
            pass
    for dirpath, _dirnames, filenames in _walk_no_lights(run_dir):
        for fname in filenames:
            if Path(fname).suffix.lower() in _EXPORT_EXTS:
                try:
                    mtimes.append((Path(dirpath) / fname).stat().st_mtime)
                except OSError:
                    pass
    if not mtimes:
        return None
    return date.fromtimestamp(max(mtimes)).isoformat()


def collect_runs(target_dir: Path, archive_root: Path) -> list[RunEvidence]:
    """Find every WBPP "run folder" under target_dir and its log evidence.

    A run folder is any directory that directly contains a logs/
    subdirectory holding at least one *.log file that yields at least one
    light night (via parse_log_nights). A logs/ dir whose logs contribute no
    light nights is not a run at all — it's skipped so its containing folder
    keeps contributing to (rather than being excluded from) F1's date-bound
    evidence pool.
    """
    runs: list[RunEvidence] = []
    for dirpath, dirnames, _filenames in os.walk(target_dir):
        for dname in dirnames:
            if dname.lower() != "logs":
                continue
            logs_dir = Path(dirpath) / dname
            log_files = sorted(
                p for p in logs_dir.iterdir()
                if p.is_file() and p.suffix.lower() == ".log"
            )
            if not log_files:
                continue

            nights: set[str] = set()
            for lf in log_files:
                nights |= parse_log_nights(lf)
            if not nights:
                continue

            run_dir = Path(dirpath)
            runs.append(RunEvidence(
                run_dir=run_dir,
                edit_date=_edit_date_for_run(run_dir, archive_root, log_files),
                nights=frozenset(nights),
                has_export=_has_export(run_dir),
            ))
    return runs
