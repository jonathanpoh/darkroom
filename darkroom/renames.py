"""darkroom.renames — resolve the pending-renames ledger against the archive (U2 Phase 1).

The webapi host (an LXC) owns the catalog but has no NAS mount, so when a
catalog edit changes a session's derived ``lights_path``, the server can only
*record* the folder move it owes (``darkroom.catalog_db.pending_renames`` —
see ``darkroom.catalog_db._record_pending_rename``) — it can't touch the
archive filesystem itself. This module runs on the Mac, where the NAS *is*
mounted, and resolves those pending moves: ``darkroom catalog apply-renames``.

Dry-run by default (repo convention, see ``darkroom.procscan``):
``apply_renames(..., apply=False)`` only classifies each pending rename into
what it would do — no filesystem writes, no acks. ``apply=True`` performs the
classified action and acks the ledger row (via the passed-in
``darkroom.catalog_client.CatalogBackend``) for everything it resolved.

Import-light and astropy-free at module load, like its siblings — stdlib
(``shutil``, ``dataclasses``, ``pathlib``) plus ``darkroom.parse.fits_files``,
which is itself stdlib-only; the catalog backend comes in as a caller-supplied
object, never imported here.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from darkroom.parse import fits_files

# Outcomes a single pending rename can resolve to.
APPLIED = "applied"
ALREADY_DONE = "already_done"
CONFLICT = "conflict"
MISSING = "missing"
ERROR = "error"


@dataclass
class RenameResult:
    """The classification (and, under --apply, the outcome) of one pending rename."""

    rename_id: int
    session_id: str
    old_path: str
    new_path: str
    outcome: str
    detail: str = ""


def _is_safe_rel(path_str: str) -> bool:
    """True if path_str is a safe archive-relative path.

    Rejects absolute paths and any path with a '..' component — defense
    against a corrupt/hand-edited ledger row being used to write or delete
    outside the archive root.
    """
    p = Path(path_str)
    if p.is_absolute():
        return False
    return ".." not in p.parts


def _is_nested(new_path: str, old_path: str) -> bool:
    """True if new_path names a directory *inside* old_path.

    Compared on the ledger's own relative path components, never against the
    filesystem: the archive lives on a case-insensitive SMB mount, so a
    filesystem-level check would conflate an unrelated case-only rename
    (`SH2-101` -> `Sh2-101`, the same inode) with real nesting.
    """
    old_parts = Path(old_path).parts
    new_parts = Path(new_path).parts
    return len(new_parts) > len(old_parts) and new_parts[:len(old_parts)] == old_parts


def _prune_empty_ancestors(old_abs: Path, archive_root: Path) -> None:
    """Rmdir old_abs's parent, then its parent, ... while each is empty.

    Stops at the first non-empty directory, and never removes archive_root
    itself (or anything at/above it) — only strict descendants of
    archive_root that lie above old_abs are candidates.
    """
    cur = old_abs.parent
    while cur != archive_root and archive_root in cur.parents:
        try:
            if any(cur.iterdir()):
                return
        except FileNotFoundError:
            return
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def apply_renames(archive_root: Path, backend, *, apply: bool = False) -> list[RenameResult]:
    """Classify (and, if apply=True, execute) every pending rename in the ledger.

    ``archive_root`` is the archive's local/mounted root; ledger paths
    (old_path/new_path) are relative to it. ``backend`` is a
    ``darkroom.catalog_client.CatalogBackend``.

    Per pending rename:
      - old_path or new_path unsafe (absolute or contains '..') -> ERROR,
        left pending, nothing touched.
      - old missing, new exists -> ALREADY_DONE (the move already happened
        on disk, or was done by hand) -> acked under apply=True.
      - old missing, new missing -> MISSING, left pending.
      - old exists, new exists, and new is *inside* old -> a "deepening"
        edit (`<night>/` -> `<night>/Lights/<Filter>/`), where old existing
        is structural rather than ambiguous. ALREADY_DONE (acked under
        apply=True) if the frames already sit at new; otherwise CONFLICT
        pointing at ``catalog migrate-archive``, since moving a directory
        into itself is not a rename.
      - old exists, new exists -> CONFLICT, left pending (ambiguous: don't
        clobber either side).
      - old exists, new missing -> the normal case: create new's parent
        dirs, ``shutil.move`` old -> new, prune now-empty ancestor
        directories of old (never above archive_root), then ack.
        Classified as APPLIED even under a dry run (apply=False), since
        that's what *would* happen — but nothing is touched or acked.

    Returns one RenameResult per ledger row, in ledger order. Never raises
    for a single item's OSError during the move — that item is recorded as
    ERROR (with the exception message as detail) and left pending; other
    items still proceed.
    """
    archive_root = Path(archive_root)
    results: list[RenameResult] = []

    for row in backend.list_pending_renames():
        rename_id = row["id"]
        session_id = row["session_id"]
        old_path = row["old_path"]
        new_path = row["new_path"]

        if not (_is_safe_rel(old_path) and _is_safe_rel(new_path)):
            results.append(RenameResult(
                rename_id, session_id, old_path, new_path, ERROR,
                "unsafe path (absolute or contains '..') — refusing to touch the archive",
            ))
            continue

        old_abs = archive_root / old_path
        new_abs = archive_root / new_path
        old_exists = old_abs.exists()
        new_exists = new_abs.exists()

        if not old_exists and new_exists:
            results.append(RenameResult(rename_id, session_id, old_path, new_path, ALREADY_DONE))
            if apply:
                backend.ack_pending_rename(rename_id)
            continue

        if not old_exists and not new_exists:
            results.append(RenameResult(
                rename_id, session_id, old_path, new_path, MISSING,
                "neither old nor new path exists on disk — left pending",
            ))
            continue

        if old_exists and new_exists and _is_nested(new_path, old_path):
            # "Deepening" edit: the new path adds a level *under* the old one
            # (`<night>/` -> `<night>/Lights/<Filter>/`). old_exists is then
            # structurally guaranteed — it is new's own ancestor — so the
            # generic conflict below would be a false alarm that can never
            # clear, and there is no way to ack a stuck conflict by hand.
            #
            # It is also not a directory rename: shutil.move would be moving a
            # directory into itself. Deciding it needs the frames, so look at
            # where they actually are.
            if fits_files(old_abs):
                results.append(RenameResult(
                    rename_id, session_id, old_path, new_path, CONFLICT,
                    "new path is inside old, and frames are still loose in old — "
                    "this is a layout migration, not a rename; "
                    "run `darkroom catalog migrate-archive`",
                ))
            elif fits_files(new_abs):
                results.append(RenameResult(
                    rename_id, session_id, old_path, new_path, ALREADY_DONE,
                    "new path is inside old and already holds the frames",
                ))
                if apply:
                    backend.ack_pending_rename(rename_id)
            else:
                results.append(RenameResult(
                    rename_id, session_id, old_path, new_path, CONFLICT,
                    "new path is inside old, but neither holds frames — left pending",
                ))
            continue

        if old_exists and new_exists:
            results.append(RenameResult(
                rename_id, session_id, old_path, new_path, CONFLICT,
                "both old and new paths exist — left pending",
            ))
            continue

        # old_exists and not new_exists: the move this ledger row is for.
        if not apply:
            results.append(RenameResult(rename_id, session_id, old_path, new_path, APPLIED))
            continue

        try:
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_abs), str(new_abs))
            _prune_empty_ancestors(old_abs, archive_root)
        except OSError as e:
            results.append(RenameResult(rename_id, session_id, old_path, new_path, ERROR, str(e)))
            continue

        backend.ack_pending_rename(rename_id)
        results.append(RenameResult(rename_id, session_id, old_path, new_path, APPLIED))

    return results
