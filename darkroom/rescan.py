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
- a catalog-only and a disk-only session that share a *canonical* identity —
  one ``rename`` proposal rather than an unrelated delete + create pair. A
  legacy row stored as ``SH2-101_...`` is the same night as a fresh scan's
  ``Sh2-101_...``; applying that as delete + create would drop the row's
  ``id``/``created_at``, its ``processed_state`` and its ``session_guiding``
  row and re-add a bare one. A rename applies through
  ``update_session_fields``, which recomputes ``session_id``/``lights_path``
  on the same row and carries all of that forward.

Tiering (F8 contract, decided): an ``update`` whose only changed fields are
``frame_count``/``total_integration_sec`` is ``tier='safe'`` (pre-approved in
the queue UI — a pure interior-deletion divergence needs no human judgement).
Everything else — any ``create``, any ``delete``, any ``update`` touching a
pointing/timing/equipment field — is ``tier='review'``.

Two guards against mistaking "the archive isn't there" for "the archive is
empty" — the failure mode that would otherwise turn an unmounted NAS into a
proposal to delete every session in the catalog:

- ``ArchiveRootMissing`` — raised when ``<archive_root>/<dso_dirname>``
  doesn't exist. Always an error; never treated as "nothing on disk".
- ``EmptyDiskDivergence`` — raised when the DSO root exists but the walk
  finds zero sessions while the catalog has some. This module does no I/O
  and no prompting (that's the CLI's job — see ``catalog_cli._rescan_archive_run``);
  it only refuses to silently propose deleting everything. A caller that has
  confirmed this is intentional passes ``allow_empty_disk=True`` to proceed.
  The reverse case — an empty catalog with a full disk — is a legitimate
  first-run state and never raises.
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
from darkroom.names import KNOWN_FILTERS, _normalize_target, normalize_session_fields
from darkroom.parse import fits_files, normalize_filter, parse_panel

DEFAULT_POINTING_TOLERANCE_DEG = 0.5


class ArchiveRootMissing(Exception):
    """<archive_root>/<dso_dirname> is not an existing directory.

    Raised rather than treating a missing root as an empty one — an
    unmounted NAS or a wrong --archive must be a hard error, not "0 sessions
    on disk".
    """

    def __init__(self, dso_root: Path):
        self.dso_root = dso_root
        super().__init__(f"archive DSO root not found: {dso_root}")


class EmptyDiskDivergence(Exception):
    """The DSO root exists, but the walk found 0 sessions while the catalog has some.

    Raised instead of silently classifying every catalog session as a
    'delete' proposal — that shape (root present, walk empty) is what an
    unmounted-but-present mountpoint, a wrong subdirectory, or a permissions
    problem looks like, not what a genuinely wiped archive looks like. Pass
    allow_empty_disk=True to scan() once a caller (the CLI, after explicit
    confirmation) has established this is intentional.
    """

    def __init__(self, catalog_session_count: int):
        self.catalog_session_count = catalog_session_count
        super().__init__(
            f"0 sessions found on disk but {catalog_session_count} in the catalog"
        )

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

# Identity components — changing any of these changes the derived session_id.
# Mirrors catalog_db's own identity set; a 'rename' proposal is exactly a
# divergence in one or more of these.
_IDENTITY_FIELDS = ("target", "obs_date", "ota", "camera", "filter", "panel")


def _canonical_session_id(row: dict) -> str:
    """session_id this row WOULD have if its identity fields were canonical.

    Every identity component is put through the same canonicalization the
    disk-side scan applies, because each one can drift:

    - **target**: `make_session_id` only strips whitespace, while the scan
      also applies `_normalize_target` — so a legacy `SH2-101_...` row and a
      fresh `Sh2-101_...` scan are the same night under two spellings.
    - **filter**: a stored value that isn't a real filter (a mosaic panel
      name like `IC4604_1-1`, U2) or is a misspelling (`AstronimikL2`) no
      longer survives `_filter_from_path`'s KNOWN_FILTERS guard (M2), so the
      fresh scan reports `UnknownFilter`/`AstronomikL2` for a row the catalog
      still holds under the old value.
    - **filter, again — `NoFilter` vs NULL**: the archive cannot tell them
      apart. `session_dest_rel` writes `Lights/NoFilter/` for a NULL filter
      while `make_session_id` writes `..._UnknownFilter`, so as soon as a
      NULL-filter session's folder is canonicalised, the disk reads back
      `NoFilter` and the row diverges from its own archive. Both therefore
      canonicalize to None here. The cost is that a deliberate NULL ->
      'NoFilter' correction is not *detected* by a rescan; the benefit is that
      ~20 sessions stop surfacing as delete + create pairs that would drop
      processed_state on apply. (Found live 2026-09-01, after it had already
      cost one `processed` row.)

    Without canonicalizing both, each of those surfaces as an unrelated
    delete + create — which on apply would drop the row's id/created_at,
    processed_state and session_guiding row and re-add a bare one.
    """
    filter_ = normalize_filter(row.get("filter") or "")
    # M1: a stored row can still carry the panel inside its target
    # ("IC 4604_1-1", the pre-M1 shape U2 flags), while the disk-side scan now
    # splits it out — so canonicalize the same way here or every legacy panel
    # row reads as a delete + create instead of a rename.
    base_target, panel = parse_panel(_normalize_target(row.get("target") or ""))
    return make_session_id(
        base_target,
        row.get("obs_date") or "",
        row.get("ota") or "",
        row.get("camera") or "",
        filter_ if filter_ in KNOWN_FILTERS and filter_ != "NoFilter" else None,
        panel=row.get("panel") or panel,
    )


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


def _phantom_filter_change(field: str, current, proposed) -> bool:
    """True for `filter: NULL -> 'NoFilter'`, which is not a real divergence.

    `session_dest_rel` has no directory name for "filter unknown", so it writes
    `Lights/NoFilter/` and the disk always reads that back. Reporting it would
    quietly convert *unrecorded* into *deliberately unfiltered* and drop the
    session out of U2's filter queue as though the question had been answered —
    and 95 sessions carry no filter, most of which did use one.

    Shared by the update path (`_diff_fields`) and the rename path, which adds
    identity fields directly; without it here the rename still carried the
    phantom change and applying it would set the filter.
    """
    return field == "filter" and not current and proposed == "NoFilter"


def _diff_fields(catalog_row: dict, disk_row: dict, tolerance: float) -> dict:
    changes = {}
    for field in _CHANGE_FIELDS:
        current = catalog_row.get(field)
        proposed = disk_row.get(field)
        if _phantom_filter_change(field, current, proposed):
            continue
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
                panel=session.get("panel"),
            )
            session["session_id"] = session_id
            session["lights_path"] = str(lights_path.relative_to(archive_root))
            # Compare against what upsert_session would STORE, not the raw
            # header values — otherwise every session with a camera diverges
            # (raw INSTRUME "Canon EOS 6D" vs the stored canonical "Canon6D")
            # and exposure noise reads as a real change. See
            # names.normalize_session_fields.
            sessions[session_id] = normalize_session_fields(session)

    return sessions


def _pair_renames(
    disk_sessions: dict[str, dict], catalog_sessions: dict[str, dict]
) -> dict[str, tuple[str, str]]:
    """Match catalog-only against disk-only sessions that share a canonical identity.

    Returns {session_id: (catalog_id, disk_id)} with an entry for BOTH ids of
    each pair, so the caller can skip either side and emit one 'rename'.

    Only unambiguous pairs are returned: a canonical id claimed by more than
    one session on either side is left alone and falls through to the ordinary
    create/delete classification. A wrong pairing would rewrite one session's
    identity to another's, which is worse than two rows a human has to read —
    so ambiguity declines to guess, in keeping with the rest of F8.
    """
    catalog_only = set(catalog_sessions) - set(disk_sessions)
    disk_only = set(disk_sessions) - set(catalog_sessions)
    if not catalog_only or not disk_only:
        return {}

    def index(ids, rows):
        out: dict[str, list[str]] = {}
        for sid in ids:
            out.setdefault(_canonical_session_id(rows[sid]), []).append(sid)
        return out

    cat_index = index(catalog_only, catalog_sessions)
    disk_index = index(disk_only, disk_sessions)

    pairs: dict[str, tuple[str, str]] = {}
    for key, cat_ids in cat_index.items():
        disk_ids = disk_index.get(key)
        if not disk_ids or len(cat_ids) != 1 or len(disk_ids) != 1:
            continue
        pair = (cat_ids[0], disk_ids[0])
        pairs[cat_ids[0]] = pair
        pairs[disk_ids[0]] = pair
    return pairs


def scan(
    archive_root: Path,
    backend,
    *,
    dso_dirname: str = "01_Deep Sky Objects",
    pointing_tolerance_deg: float = DEFAULT_POINTING_TOLERANCE_DEG,
    allow_empty_disk: bool = False,
) -> list[dict]:
    """Diff <archive_root>/<dso_dirname> against the catalog; return proposal dicts.

    ``backend`` is a darkroom.catalog_client.CatalogBackend (LocalBackend or
    HttpBackend); sessions are fetched via darkroom.catalog.query_all_sessions
    over it. Only reads the archive filesystem otherwise — never mutates
    session rows or the review queue (that's `apply`). Every proposal shares
    one `detected_at` timestamp for this run. See the module docstring for
    the classification/tiering rules; shape is the F8 shared contract.

    Raises ``ArchiveRootMissing`` if the DSO root doesn't exist, and
    ``EmptyDiskDivergence`` if it exists but the walk finds 0 sessions while
    the catalog has some (pass ``allow_empty_disk=True`` to proceed anyway,
    once a caller has confirmed that's intentional). An empty catalog with a
    non-empty disk — ordinary first-run — never raises either way.
    """
    archive_root = Path(archive_root)
    dso_root = archive_root / dso_dirname
    if not dso_root.is_dir():
        raise ArchiveRootMissing(dso_root)

    disk_sessions = _scan_disk(dso_root, archive_root)
    catalog_sessions = {row["session_id"]: row for row in query_all_sessions(backend)}

    if not disk_sessions and catalog_sessions and not allow_empty_disk:
        raise EmptyDiskDivergence(len(catalog_sessions))

    detected_at = _now_iso()
    proposals: list[dict] = []

    # Pair off catalog-only and disk-only sessions that are the same night
    # under two spellings of its identity (legacy `SH2-101` vs canonical
    # `Sh2-101`). Without this they'd surface as an unrelated delete + create,
    # and applying that pair would drop the row's id/created_at, its
    # processed_state and its session_guiding row, then re-add a bare one.
    # A rename applies through update_session_fields instead, which recomputes
    # session_id and lights_path on the SAME row (catalog_db's anti-orphan
    # guarantee) and carries all of that forward.
    renames = _pair_renames(disk_sessions, catalog_sessions)

    for session_id in sorted(set(disk_sessions) | set(catalog_sessions)):
        if session_id in renames:
            old_id, new_id = renames[session_id]
            if session_id != old_id:
                continue  # emitted once, keyed on the catalog-side id
            cat, disk = catalog_sessions[old_id], disk_sessions[new_id]
            changes = _diff_fields(cat, disk, pointing_tolerance_deg)
            changes.update({
                f: {"current": cat.get(f), "proposed": disk.get(f)}
                for f in _IDENTITY_FIELDS
                if cat.get(f) != disk.get(f)
                and not _phantom_filter_change(f, cat.get(f), disk.get(f))
            })
            if not changes:
                # Nothing actually differs. The two ids can still disagree when
                # the only gap is the NULL/'NoFilter' round-trip above, and a
                # rename with no changed field would be an empty write that
                # keeps reappearing on every scan. Not a divergence.
                continue
            proposals.append({
                "session_id": old_id,
                "kind": "rename",
                "tier": "review",
                "target": cat.get("target"),
                "obs_date": cat.get("obs_date"),
                "lights_path": disk.get("lights_path"),
                "changes": changes,
                "detected_at": detected_at,
            })
            continue

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
