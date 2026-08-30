"""darkroom.rescan — diff the archive against the catalog and propose fixes (F8).

Strictly read-only on both sides: ``scan`` only reads FITS headers off disk
(via ``cataloger.find_lights_folders``/``parse.fits_files``/
``cataloger.FITSHeaderExtractor``/``cataloger.SessionAnalyzer``, the same walk
``scan-lights`` already does) and reads sessions from a
``darkroom.catalog_client.CatalogBackend`` (local SQLite or the webapi server,
per how the backend was resolved) via ``darkroom.catalog.query_all_sessions``.
It never calls ``upsert_session``/``update_session_fields`` and never touches
a session row. The only write path is ``apply``, and even that does not
write to ``sessions`` — it calls ``backend.replace_rescan_proposals``, which
pushes the findings to the ``rescan_proposals`` review queue for a human (or
the queue's pre-approved 'safe' tier) to apply from there. See BACKLOG.md F8
and the shared contract both agents built this against.

Every ``session_id`` seen on either side is classified:

- on disk, matches the catalog (within tolerance) — no proposal.
- on disk, diverges from the catalog — an ``update`` proposal, session_id
  unchanged. B14 fixed ``SessionAnalyzer.analyze_sessions`` to pick its
  representative frame by chronologically-earliest ``DATE-OBS`` rather than
  directory-walk order — going through it here (rather than reimplementing
  frame selection) is exactly how this module avoids reintroducing that bug.
- on disk, no catalog row — a ``create`` proposal (folder exists but was
  never ingested/committed).
- in the catalog, ``lights_path`` missing on disk — a ``delete`` proposal,
  never an automatic delete.

Tiering (F8 contract, decided): an ``update`` whose only changed fields are
``frame_count``/``total_integration_sec`` is ``tier='safe'`` (pre-approved in
the queue UI — a pure interior-deletion divergence needs no human judgement).
Everything else — any ``create``, any ``delete``, any ``update`` touching a
pointing/timing/equipment field — is ``tier='review'``.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from darkroom.catalog import query_all_sessions
from darkroom.cataloger import (
    FITSHeaderExtractor,
    SessionAnalyzer,
    find_lights_folders,
    make_session_id,
)
from darkroom.parse import fits_files

DEFAULT_POINTING_TOLERANCE_DEG = 0.5

# Fields compared for an 'update' divergence, and reported in full for a
# 'create'/'delete' proposal's `changes` dict. target/obs_date are
# deliberately absent: together with ota/camera/filter (which ARE compared)
# they're baked into session_id itself, so a change to target or obs_date
# shows up as a create+delete pair, never an update.
_SAFE_FIELDS = frozenset({"frame_count", "total_integration_sec"})

_CHANGE_FIELDS = (
    "ota", "camera", "filter", "gain", "temperature_c", "exposure_sec",
    "focal_length", "frame_count", "total_integration_sec",
    "ra_deg", "dec_deg", "start_utc", "end_utc",
)

_FLOAT_FIELDS = frozenset({"exposure_sec", "temperature_c", "focal_length"})
_INT_FIELDS = frozenset({"frame_count", "total_integration_sec", "gain"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ra_diff_deg(a: float, b: float) -> float:
    """Angular difference between two RA values in degrees, wrapping at 360.

    min(|a-b|, 360-|a-b|) per the F8 contract — a plain subtraction would say
    359.9 and 0.1 are 359.8 degrees apart instead of the true 0.2.
    """
    d = abs(a - b)
    return min(d, 360.0 - d)


def _diverges(field: str, current, proposed, tolerance: float) -> bool:
    """True if `current` (catalog) and `proposed` (disk) count as different.

    None-safe: differing None-ness is always a divergence. RA compares as an
    angular difference wrapping at 360; Dec as a plain absolute difference;
    both against `tolerance` degrees, with the boundary itself counted as a
    divergence ("below tolerance" is the only case that's *not* reported).
    Other floats compare with a small epsilon rather than `!=` on a REAL
    column; ints and strings compare exactly.
    """
    if current is None or proposed is None:
        return current != proposed
    if field == "ra_deg":
        return _ra_diff_deg(float(current), float(proposed)) >= tolerance
    if field == "dec_deg":
        return abs(float(current) - float(proposed)) >= tolerance
    if field in _FLOAT_FIELDS:
        return not math.isclose(float(current), float(proposed), rel_tol=1e-9, abs_tol=1e-6)
    if field in _INT_FIELDS:
        return int(current) != int(proposed)
    return current != proposed


def _diff_fields(catalog_row: dict, disk_row: dict, tolerance: float) -> dict:
    changes = {}
    for field in _CHANGE_FIELDS:
        current = catalog_row.get(field)
        proposed = disk_row.get(field)
        if _diverges(field, current, proposed, tolerance):
            changes[field] = {"current": current, "proposed": proposed}
    return changes


def _scan_disk(dso_root: Path, archive_root: Path) -> dict[str, dict]:
    """Rebuild every session dict currently on disk under dso_root, keyed by session_id.

    Mirrors cataloger.scan_all_command's own walk exactly (find_lights_folders
    -> fits_files -> FITSHeaderExtractor.extract_metadata ->
    SessionAnalyzer.analyze_sessions -> make_session_id), so it never
    reimplements frame-selection logic (B14) or the imaging-night grouping.
    """
    sessions: dict[str, dict] = {}
    if not dso_root.is_dir():
        return sessions

    for lights_path in sorted(find_lights_folders(dso_root)):
        frame_paths = fits_files(lights_path)
        metadata_list = [
            m for m in (FITSHeaderExtractor.extract_metadata(f) for f in frame_paths) if m
        ]
        if not metadata_list:
            continue

        for session in SessionAnalyzer.analyze_sessions(metadata_list, lights_path):
            session_id = make_session_id(
                session["target"], session["obs_date"],
                session["ota"], session["camera"], session["filter"],
            )
            session["session_id"] = session_id
            session["lights_path"] = str(lights_path.relative_to(archive_root))
            sessions[session_id] = session

    return sessions


def scan(
    archive_root: Path,
    backend,
    *,
    dso_dirname: str = "01_Deep Sky Objects",
    pointing_tolerance_deg: float = DEFAULT_POINTING_TOLERANCE_DEG,
) -> list[dict]:
    """Diff <archive_root>/<dso_dirname> against the catalog; return proposal dicts.

    ``backend`` is a darkroom.catalog_client.CatalogBackend (LocalBackend or
    HttpBackend); sessions are fetched via darkroom.catalog.query_all_sessions
    over it. Only reads the archive filesystem otherwise — never mutates
    session rows or the review queue (that's `apply`). Every proposal shares
    one `detected_at` timestamp for this run. See the module docstring for
    the classification/tiering rules; shape is the F8 shared contract.
    """
    archive_root = Path(archive_root)
    dso_root = archive_root / dso_dirname

    disk_sessions = _scan_disk(dso_root, archive_root)
    catalog_sessions = {row["session_id"]: row for row in query_all_sessions(backend)}

    detected_at = _now_iso()
    proposals: list[dict] = []

    for session_id in sorted(set(disk_sessions) | set(catalog_sessions)):
        disk = disk_sessions.get(session_id)
        cat = catalog_sessions.get(session_id)

        if disk is not None and cat is not None:
            changes = _diff_fields(cat, disk, pointing_tolerance_deg)
            if not changes:
                continue
            tier = "safe" if set(changes) <= _SAFE_FIELDS else "review"
            proposals.append({
                "session_id": session_id,
                "kind": "update",
                "tier": tier,
                "target": cat.get("target") or disk.get("target"),
                "obs_date": cat.get("obs_date") or disk.get("obs_date"),
                "lights_path": disk.get("lights_path"),
                "changes": changes,
                "detected_at": detected_at,
            })
        elif disk is not None:
            proposals.append({
                "session_id": session_id,
                "kind": "create",
                "tier": "review",
                "target": disk.get("target"),
                "obs_date": disk.get("obs_date"),
                "lights_path": disk.get("lights_path"),
                "changes": {
                    field: {"current": None, "proposed": disk.get(field)}
                    for field in _CHANGE_FIELDS
                },
                "detected_at": detected_at,
            })
        else:
            proposals.append({
                "session_id": session_id,
                "kind": "delete",
                "tier": "review",
                "target": cat.get("target"),
                "obs_date": cat.get("obs_date"),
                "lights_path": cat.get("lights_path"),
                "changes": {
                    field: {"current": cat.get(field), "proposed": None}
                    for field in _CHANGE_FIELDS
                },
                "detected_at": detected_at,
            })

    return proposals


def apply(backend, proposals: list[dict]) -> int:
    """Push `proposals` to the review queue. Does NOT write to sessions.

    Calls backend.replace_rescan_proposals(proposals), which replaces the
    *pending* proposal set (applied/dismissed rows are untouched — they're
    the audit trail). Returns the number of rows written. Nothing here ever
    calls upsert_session/update_session_fields; a human (or the queue's
    pre-approved 'safe' tier) applies each proposal from the review queue.
    """
    return backend.replace_rescan_proposals(proposals)
