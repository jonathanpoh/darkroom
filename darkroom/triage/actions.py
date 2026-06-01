from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

from astropy.io import fits

from darkroom.triage.db import complete_action, log_action, mark_reverted

_FITS_SUFFIXES = {".fit", ".fits"}


def _sha256_first_fits(directory: Path) -> str | None:
    for p in sorted(directory.rglob("*")):
        if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name:
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
    return None


def move(
    conn: sqlite3.Connection,
    item_id: int,
    src: Path,
    dst: Path,
) -> None:
    sha = _sha256_first_fits(src) if src.is_dir() else None
    log_id = log_action(
        conn,
        triage_item_id=item_id,
        action_type="move",
        source_path=str(src),
        dest_path=str(dst),
        source_sha256=sha,
    )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        complete_action(conn, log_id, "success")
    except Exception as exc:
        complete_action(conn, log_id, "error", str(exc))
        raise


def rename(
    conn: sqlite3.Connection,
    item_id: int,
    src: Path,
    dst: Path,
) -> None:
    move(conn, item_id, src, dst)


def trash(
    conn: sqlite3.Connection,
    item_id: int,
    src: Path,
    *,
    archive_root: Path,
    trash_root: Path,
) -> None:
    rel = src.relative_to(archive_root)
    dst = trash_root / rel
    log_id = log_action(
        conn,
        triage_item_id=item_id,
        action_type="delete",
        source_path=str(src),
        dest_path=str(dst),
    )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        complete_action(conn, log_id, "success")
    except Exception as exc:
        complete_action(conn, log_id, "error", str(exc))
        raise


def copy_corrected(
    conn: sqlite3.Connection,
    item_id: int,
    src_dir: Path,
    dst_dir: Path,
    header_patches: dict,
) -> None:
    fits_paths = [
        p for p in sorted(src_dir.rglob("*"))
        if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name
    ]
    log_id = log_action(
        conn,
        triage_item_id=item_id,
        action_type="copy_corrected",
        source_path=str(src_dir),
        dest_path=str(dst_dir),
    )
    try:
        for src_file in fits_paths:
            rel = src_file.relative_to(src_dir)
            dst_file = dst_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            orig_stat = src_file.stat()
            with fits.open(src_file) as hdul:
                for key, val in header_patches.items():
                    hdul[0].header[key] = val
                hdul.writeto(dst_file, overwrite=True)
            os.utime(dst_file, (orig_stat.st_atime, orig_stat.st_mtime))
        complete_action(conn, log_id, "success")
    except Exception as exc:
        complete_action(conn, log_id, "error", str(exc))
        raise


def revert(
    conn: sqlite3.Connection,
    log_id: int,
    trash_root: Path,
) -> None:
    row = conn.execute(
        "SELECT * FROM audit_log WHERE id = ?", (log_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"audit_log id {log_id} not found")

    action_type = row["action_type"]
    src = Path(row["source_path"])
    dst = Path(row["dest_path"])

    if action_type in ("move", "rename", "delete"):
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
    elif action_type == "copy_corrected":
        if dst.exists():
            shutil.rmtree(dst)
    else:
        raise ValueError(f"Cannot revert action_type={action_type!r}")

    mark_reverted(conn, log_id)
