"""darkroom.procscan — scan the archive for processing output and reconcile
each session's processed_state to what's actually on disk (F1).

Strictly read-only on the archive: every function here only reads files via
os.walk/stat, never moves/writes/deletes anything. The only write path is
``apply``, which calls ``darkroom.cataloger.set_processed_state`` — imported
lazily so that ``scan`` (the dry-run path) never pulls in astropy and never
touches the catalog schema (no ``init_db``). Sessions are read via
``darkroom.catalog.query_all_sessions``, a plain ``sqlite3.connect`` with no
schema mutation, matching the read layer's existing astropy-free contract.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from darkroom.catalog import query_all_sessions

# Subs / raw lights — never evidence of processing, regardless of location.
_SUB_EXTS = frozenset({".fit", ".fits", ".orf", ".cr2"})

# Final export formats.
_EXPORT_EXTS = frozenset({".tif", ".tiff", ".jpg", ".jpeg", ".png", ".psd", ".psb"})

# Stacked/editing-in-progress evidence: WBPP masters, hand-edit intermediates,
# and PixInsight project files.
_XISF = ".xisf"
_INPROGRESS_EXTS = frozenset({_XISF, ".xpsm", ".xosm"})

# Monotonic ordering for the auto-upgrade rule in scan(). 'skipped' is
# deliberately absent — it's handled specially (never auto-changed) rather
# than ranked.
_STATE_RANK = {"unprocessed": 0, "in_progress": 1, "processed": 2}

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class Transition:
    """One session's proposed processed_state change (or non-change)."""

    session_id: str
    target: str
    obs_date: str
    current_state: str
    proposed_state: str
    evidence: str
    evidence_date: str | None
    change: bool


def _evidence_date(path: Path, archive_root: Path) -> str | None:
    """Return a YYYY-MM-DD evidence date for an artifact path.

    Searches the path's ancestor folder-name components (relative to
    archive_root, when path is under it) for a YYYY-MM-DD substring — this is
    how the ``_Processed/<date>/`` edit-date convention is recovered,
    regardless of how deep the artifact sits under that folder. Falls back to
    the file's mtime date if no dated folder component is found.
    """
    try:
        rel_parts = path.relative_to(archive_root).parts
    except ValueError:
        rel_parts = path.parts
    for part in rel_parts:
        m = _DATE_RE.search(part)
        if m:
            return m.group(0)
    try:
        return date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


def classify_target(target_dir: Path, archive_root: Path) -> dict:
    """Walk a target's directory tree and collect processing-evidence dates.

    Ignores anything under a path component named 'Lights'/'lights' (subs,
    including ASIAir thumbnails) and any file whose extension is a raw sub
    format — those never count as processing evidence no matter where they
    sit. Returns
        {"export_dates": [...], "inprogress_dates": [...]}
    each a sorted list of YYYY-MM-DD strings (possibly empty).
    """
    export_dates: list[str] = []
    inprogress_dates: list[str] = []

    for dirpath, dirnames, filenames in _walk(target_dir):
        cur = Path(dirpath)
        for fname in filenames:
            if "_thn" in fname.lower():
                continue
            fpath = cur / fname
            ext = fpath.suffix.lower()
            if ext in _SUB_EXTS:
                continue
            if ext in _EXPORT_EXTS:
                d = _evidence_date(fpath, archive_root)
                if d is not None:
                    export_dates.append(d)
            elif ext in _INPROGRESS_EXTS:
                d = _evidence_date(fpath, archive_root)
                if d is not None:
                    inprogress_dates.append(d)

    return {
        "export_dates": sorted(export_dates),
        "inprogress_dates": sorted(inprogress_dates),
    }


def _walk(root: Path):
    """os.walk(root), pruning any 'Lights'/'lights' subtree (case-insensitive)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() != "lights"]
        if Path(dirpath).name.lower() == "lights":
            continue
        yield dirpath, dirnames, filenames


def _covers(dates: list[str | None], obs_date: str) -> tuple[bool, str | None]:
    """Return (covers_obs_date, evidence_date) for a list of evidence dates.

    An evidence date "covers" obs_date if it is >= obs_date (the edit
    happened on or after the imaging night). Robustness: _evidence_date
    always resolves to a real date (dated folder or mtime fallback), so a
    None entry should never occur in practice — but if one ever did, that
    entry is undated evidence and we coarsen to "applies to all nights"
    (covers unconditionally, with no specific evidence_date) rather than
    silently dropping it.
    """
    if not dates:
        return False, None
    if any(d is None for d in dates):
        return True, None
    comparable = [d for d in dates if d >= obs_date]
    if not comparable:
        return False, None
    return True, min(comparable)


def classify_session(obs_date: str, target_ev: dict) -> tuple[str, str, str | None]:
    """Return (proposed_state, evidence, evidence_date) for one session.

    Date-bound: a session is only "covered" by processing evidence dated on
    or after its own obs_date (an edit fuses several imaging nights, but
    can't retroactively process a night that happened after the edit).
    Export evidence outranks in-progress evidence; absent either, the session
    is unprocessed.
    """
    export_dates = target_ev.get("export_dates") or []
    inprogress_dates = target_ev.get("inprogress_dates") or []

    covers, ev_date = _covers(export_dates, obs_date)
    if covers:
        return "processed", "export", ev_date

    covers, ev_date = _covers(inprogress_dates, obs_date)
    if covers:
        return "in_progress", "xisf/master", ev_date

    return "unprocessed", "", None


def scan(
    archive_root: Path, catalog: Path, *, dso_dirname: str = "01_Deep Sky Objects"
) -> list[Transition]:
    """Read every session from the catalog and propose a processed_state.

    Pure read: uses darkroom.catalog.query_all_sessions (plain connect, no
    schema mutation) and only reads the archive filesystem. Each target's
    directory is classified once and cached across its sessions. A session
    whose target folder is missing on disk gets proposed='unprocessed' with
    change=False (reported, not acted on — we don't downgrade a session just
    because its folder briefly vanished, e.g. an unmounted share).

    change is True only for a monotonic upgrade (unprocessed -> in_progress
    -> processed) and never for a 'skipped' row, which is never touched.

    Returns every transition, including no-change ones, so callers can
    report the full picture.
    """
    archive_root = Path(archive_root)
    dso_root = archive_root / dso_dirname
    rows = query_all_sessions(catalog)

    cache: dict[str, dict | None] = {}
    transitions: list[Transition] = []

    for row in rows:
        target = row["target"]
        obs_date = row["obs_date"]
        current = row.get("processed_state") or "unprocessed"

        if target not in cache:
            target_dir = dso_root / target
            cache[target] = classify_target(target_dir, archive_root) if target_dir.is_dir() else None
        target_ev = cache[target]

        if target_ev is None:
            proposed, evidence, evidence_date = "unprocessed", "", None
        else:
            proposed, evidence, evidence_date = classify_session(obs_date, target_ev)

        change = (
            current != "skipped"
            and _STATE_RANK.get(proposed, 0) > _STATE_RANK.get(current, 0)
        )

        transitions.append(Transition(
            session_id=row["session_id"],
            target=target,
            obs_date=obs_date,
            current_state=current,
            proposed_state=proposed,
            evidence=evidence,
            evidence_date=evidence_date,
            change=change,
        ))

    return transitions


def apply(catalog: Path, transitions: list[Transition]) -> int:
    """Write every change=True transition's proposed_state to the catalog.

    Returns the number of transitions applied. processed_date is only passed
    through when the transition carries one (undated in-progress/processed
    evidence leaves the existing processed_date untouched).
    """
    from darkroom.cataloger import set_processed_state  # lazy: keep scan() astropy-free

    count = 0
    for t in transitions:
        if not t.change:
            continue
        kwargs = {"state": t.proposed_state}
        if t.evidence_date is not None:
            kwargs["processed_date"] = t.evidence_date
        set_processed_state(catalog, t.session_id, **kwargs)
        count += 1
    return count
