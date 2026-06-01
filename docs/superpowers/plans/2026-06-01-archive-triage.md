# darkroom triage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `darkroom triage` — a FastAPI + htmx web app that scans a staging copy of the astrophotography archive for structural and metadata problems, presents them one folder at a time for human review, and applies approved changes (moves, renames, FITS header corrections) with a full audit log and per-item revert.

**Architecture:** `scanner.py` walks `staging/` and writes `triage_items` rows to a local `triage.db`; `server.py` (FastAPI + htmx) presents items for review and streams commit progress via SSE; `actions.py` handles all filesystem mutations with pre/post audit log writes so every operation is revertible.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, htmx 2.x (CDN), astropy, astroquery, opencv-python-headless, uvicorn, SQLite

---

## File Map

| File | Role |
|---|---|
| `darkroom/triage/__init__.py` | Empty package marker |
| `darkroom/triage/db.py` | SQLite DDL, upsert, status updates, audit log |
| `darkroom/triage/actions.py` | move / rename / trash / copy_corrected with audit |
| `darkroom/triage/checks.py` | OBJECT header check + RA/DEC vs SIMBAD |
| `darkroom/triage/preview.py` | FITS → debayered + ZScale-stretched JPEG |
| `darkroom/triage/scanner.py` | Walks archive, produces TriageCandidate per category |
| `darkroom/triage/server.py` | FastAPI app: all routes + SSE |
| `darkroom/triage/cli.py` | `add_subparser()` + scan/serve/commit dispatch |
| `darkroom/templates/triage/base.html` | Nav + htmx CDN |
| `darkroom/templates/triage/dashboard.html` | Progress summary |
| `darkroom/templates/triage/queue.html` | Filterable item table |
| `darkroom/templates/triage/item.html` | Detail pane + action form |
| `darkroom/templates/triage/commit.html` | Pre-commit review + SSE progress |
| `darkroom/templates/triage/audit.html` | Audit log + revert buttons |
| `tests/triage/test_db.py` | Database layer tests |
| `tests/triage/test_actions.py` | File operation tests |
| `tests/triage/test_checks.py` | Header check tests |
| `tests/triage/test_preview.py` | Thumbnail generation tests |
| `tests/triage/test_scanner.py` | Scanner category detection tests |
| `tests/triage/test_server.py` | FastAPI route tests |
| Modify: `darkroom/cli.py` | Add triage subparser import + dispatch |
| Modify: `pyproject.toml` | Add new dependencies |

---

## Task 1: Dependencies + package skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `darkroom/triage/__init__.py`
- Create: `tests/triage/__init__.py`

- [ ] **Step 1: Add dependencies to pyproject.toml**

In the `[project]` `dependencies` list, add:

```toml
[project]
dependencies = [
    "astropy>=5.0",
    "astroquery>=0.4",
    "datasette>=0.65",
    "fastapi>=0.111",
    "jinja2>=3.1",
    "opencv-python-headless>=4.9",
    "pyyaml>=6.0.3",
    "uvicorn[standard]>=0.29",
]

[project.optional-dependencies]
dev = [
    "httpx>=0.27",
    "pytest>=8.0",
    "pytest-anyio>=0.0.0",
]
```

- [ ] **Step 2: Sync dependencies**

```bash
uv sync --extra dev
```

Expected: resolves without errors.

- [ ] **Step 3: Create package markers**

```bash
mkdir -p darkroom/triage darkroom/templates/triage tests/triage
touch darkroom/triage/__init__.py tests/triage/__init__.py
```

- [ ] **Step 4: Verify import works**

```bash
uv run python -c "import darkroom.triage; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml darkroom/triage/__init__.py tests/triage/__init__.py
git commit -m "feat(triage): bootstrap package skeleton and dependencies"
```

---

## Task 2: Database layer (`db.py`)

**Files:**
- Create: `darkroom/triage/db.py`
- Create: `tests/triage/test_db.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/triage/test_db.py
import json
import sqlite3
from pathlib import Path

import pytest

from darkroom.triage.db import (
    open_db,
    upsert_item,
    update_status,
    get_item,
    list_items,
    count_items,
    log_action,
    complete_action,
    list_audit,
    get_audit_entry,
    mark_reverted,
)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "triage.db")
    yield c
    c.close()


class TestOpenDb:
    def test_creates_tables(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "triage_items" in tables
        assert "audit_log" in tables

    def test_idempotent(self, tmp_path):
        db = tmp_path / "t.db"
        c1 = open_db(db)
        c1.close()
        c2 = open_db(db)  # must not raise
        c2.close()


class TestUpsertItem:
    def test_inserts_new_item(self, conn):
        item_id = upsert_item(
            conn,
            category="flat_restructure",
            source_path="/staging/00_Calibration/Flats/20240110_FMA180_Canon6D_L-Pro",
            proposed_path="/staging/00_Calibration/Flats/FMA180_Canon6D_L-Pro/2024-01-10",
        )
        assert item_id > 0

    def test_returns_id_on_conflict(self, conn):
        src = "/staging/foo"
        id1 = upsert_item(conn, category="flat_restructure", source_path=src)
        id2 = upsert_item(conn, category="flat_restructure", source_path=src)
        assert id1 == id2

    def test_does_not_overwrite_non_pending(self, conn):
        src = "/staging/bar"
        item_id = upsert_item(conn, category="flat_restructure", source_path=src,
                              proposed_path="/staging/old")
        update_status(conn, item_id, "approved")
        upsert_item(conn, category="flat_restructure", source_path=src,
                    proposed_path="/staging/new")
        item = get_item(conn, item_id)
        assert item["proposed_path"] == "/staging/old"  # preserved

    def test_stores_fits_metadata_as_json(self, conn):
        meta = {"OBJECT": "M 81", "RA": 148.888}
        item_id = upsert_item(conn, category="missing_object", source_path="/p",
                              fits_metadata=meta)
        item = get_item(conn, item_id)
        assert item["fits_metadata"]["OBJECT"] == "M 81"


class TestUpdateStatus:
    def test_sets_status(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/s1")
        update_status(conn, item_id, "approved")
        assert get_item(conn, item_id)["status"] == "approved"

    def test_sets_notes_and_proposed_path(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/s2",
                              proposed_path="/orig")
        update_status(conn, item_id, "modified",
                      user_notes="changed dest",
                      proposed_path="/new")
        item = get_item(conn, item_id)
        assert item["user_notes"] == "changed dest"
        assert item["proposed_path"] == "/new"


class TestListAndCount:
    def test_list_filters_by_category(self, conn):
        upsert_item(conn, category="flat_restructure", source_path="/a")
        upsert_item(conn, category="legacy_session", source_path="/b")
        items = list_items(conn, category="flat_restructure")
        assert len(items) == 1
        assert items[0]["source_path"] == "/a"

    def test_count_by_status(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/c")
        update_status(conn, item_id, "approved")
        assert count_items(conn, status="approved") == 1
        assert count_items(conn, status="pending") == 0


class TestAuditLog:
    def test_log_and_complete(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/d")
        log_id = log_action(conn, triage_item_id=item_id,
                            action_type="rename",
                            source_path="/d", dest_path="/e",
                            source_sha256="abc123")
        assert log_id > 0
        entry = get_audit_entry(conn, log_id)
        assert entry["result"] is None  # pre-write sentinel

        complete_action(conn, log_id, "success")
        entry = get_audit_entry(conn, log_id)
        assert entry["result"] == "success"

    def test_mark_reverted(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/f")
        log_id = log_action(conn, triage_item_id=item_id,
                            action_type="move", source_path="/f", dest_path="/g")
        complete_action(conn, log_id, "success")
        mark_reverted(conn, log_id)
        entry = get_audit_entry(conn, log_id)
        assert entry["result"] == "reverted"
        assert entry["reverted_at"] is not None

    def test_list_audit_order(self, conn):
        item_id = upsert_item(conn, category="flat_restructure", source_path="/h")
        log_action(conn, triage_item_id=item_id, action_type="move",
                   source_path="/h", dest_path="/i")
        log_action(conn, triage_item_id=item_id, action_type="mkdir",
                   source_path="/h", dest_path="/j")
        entries = list_audit(conn)
        assert len(entries) == 2
        # newest first
        assert entries[0]["dest_path"] == "/j"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_db.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'darkroom.triage.db'`

- [ ] **Step 3: Implement `darkroom/triage/db.py`**

```python
# darkroom/triage/db.py
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS triage_items (
    id               INTEGER PRIMARY KEY,
    category         TEXT NOT NULL,
    source_path      TEXT UNIQUE NOT NULL,
    proposed_path    TEXT,
    proposed_value   TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    user_notes       TEXT,
    fits_metadata    TEXT,
    simbad_cache     TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    triage_item_id  INTEGER REFERENCES triage_items(id),
    action_type     TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    dest_path       TEXT NOT NULL,
    result          TEXT,
    error_msg       TEXT,
    source_sha256   TEXT,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    reverted_at     TEXT
);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def upsert_item(
    conn: sqlite3.Connection,
    *,
    category: str,
    source_path: str,
    proposed_path: str | None = None,
    proposed_value: str | None = None,
    fits_metadata: dict | None = None,
    simbad_cache: dict | None = None,
) -> int:
    meta_json = json.dumps(fits_metadata) if fits_metadata else None
    simbad_json = json.dumps(simbad_cache) if simbad_cache else None
    cur = conn.execute(
        """
        INSERT INTO triage_items
            (category, source_path, proposed_path, proposed_value, fits_metadata, simbad_cache)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            category      = excluded.category,
            proposed_path = COALESCE(proposed_path, excluded.proposed_path),
            proposed_value= COALESCE(proposed_value, excluded.proposed_value),
            fits_metadata = COALESCE(fits_metadata, excluded.fits_metadata),
            simbad_cache  = COALESCE(simbad_cache, excluded.simbad_cache),
            updated_at    = datetime('now')
        WHERE status = 'pending'
        """,
        (category, source_path, proposed_path, proposed_value, meta_json, simbad_json),
    )
    conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute(
        "SELECT id FROM triage_items WHERE source_path = ?", (source_path,)
    ).fetchone()
    return row["id"]


def update_status(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    *,
    user_notes: str | None = None,
    proposed_path: str | None = None,
    proposed_value: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE triage_items
        SET status        = ?,
            user_notes    = COALESCE(?, user_notes),
            proposed_path = COALESCE(?, proposed_path),
            proposed_value= COALESCE(?, proposed_value),
            updated_at    = datetime('now')
        WHERE id = ?
        """,
        (status, user_notes, proposed_path, proposed_value, item_id),
    )
    conn.commit()


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM triage_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("fits_metadata", "simbad_cache"):
        if d[key]:
            d[key] = json.loads(d[key])
    return d


def list_items(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM triage_items {where} ORDER BY category, id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        for key in ("fits_metadata", "simbad_cache"):
            if d[key]:
                d[key] = json.loads(d[key])
        result.append(d)
    return result


def count_items(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    status: str | None = None,
) -> int:
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT COUNT(*) FROM triage_items {where}", params
    ).fetchone()[0]


def log_action(
    conn: sqlite3.Connection,
    *,
    triage_item_id: int,
    action_type: str,
    source_path: str,
    dest_path: str,
    source_sha256: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO audit_log
            (triage_item_id, action_type, source_path, dest_path, source_sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        (triage_item_id, action_type, source_path, dest_path, source_sha256),
    )
    conn.commit()
    return cur.lastrowid


def complete_action(
    conn: sqlite3.Connection, log_id: int, result: str, error_msg: str | None = None
) -> None:
    conn.execute(
        "UPDATE audit_log SET result = ?, error_msg = ? WHERE id = ?",
        (result, error_msg, log_id),
    )
    conn.commit()


def list_audit(
    conn: sqlite3.Connection, limit: int = 100, offset: int = 0
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY applied_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def get_audit_entry(conn: sqlite3.Connection, log_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM audit_log WHERE id = ?", (log_id,)
    ).fetchone()
    return dict(row) if row else None


def mark_reverted(conn: sqlite3.Connection, log_id: int) -> None:
    conn.execute(
        """
        UPDATE audit_log
        SET result = 'reverted', reverted_at = datetime('now')
        WHERE id = ?
        """,
        (log_id,),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/triage/test_db.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/db.py tests/triage/test_db.py
git commit -m "feat(triage): database layer with triage_items and audit_log"
```

---

## Task 3: File actions (`actions.py`)

**Files:**
- Create: `darkroom/triage/actions.py`
- Create: `tests/triage/test_actions.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/triage/test_actions.py
import hashlib
from pathlib import Path

import pytest
from astropy.io import fits
import numpy as np

from darkroom.triage.db import open_db, upsert_item, get_audit_entry
from darkroom.triage.actions import move, rename, trash, copy_corrected, revert


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "triage.db")
    yield c
    c.close()


@pytest.fixture
def item_id(conn):
    return upsert_item(conn, category="flat_restructure", source_path="/fake")


def make_fits(path: Path, object_val: str = "M 81") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((10, 10), dtype=np.uint16))
    hdu.header["OBJECT"] = object_val
    hdu.header["RA"] = 148.888
    hdu.header["DEC"] = 69.065
    hdu.writeto(path, overwrite=True)
    return path


class TestMove:
    def test_moves_directory(self, conn, item_id, tmp_path):
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "frame.fit").write_bytes(b"FITS")
        dst = tmp_path / "dst_dir"

        move(conn, item_id, src, dst)

        assert dst.exists()
        assert not src.exists()

    def test_writes_audit_log(self, conn, item_id, tmp_path):
        src = tmp_path / "s"
        src.mkdir()
        dst = tmp_path / "d"
        move(conn, item_id, src, dst)

        entries = [
            r for r in conn.execute("SELECT * FROM audit_log").fetchall()
        ]
        assert len(entries) == 1
        assert entries[0]["result"] == "success"
        assert entries[0]["action_type"] == "move"

    def test_creates_parent_dirs(self, conn, item_id, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "deep" / "nested" / "dst"
        move(conn, item_id, src, dst)
        assert dst.exists()


class TestTrash:
    def test_moves_to_trash_dir(self, conn, item_id, tmp_path):
        archive = tmp_path / "archive"
        src = archive / "Flats" / "20240110_FMA180"
        src.mkdir(parents=True)
        trash_root = tmp_path / ".triage_trash"

        trash(conn, item_id, src, archive_root=archive, trash_root=trash_root)

        assert not src.exists()
        trashed = trash_root / "Flats" / "20240110_FMA180"
        assert trashed.exists()

    def test_audit_action_type_is_delete(self, conn, item_id, tmp_path):
        archive = tmp_path / "archive"
        src = archive / "thumb_thn.jpg"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"jpg")
        trash(conn, item_id, src, archive_root=archive,
              trash_root=tmp_path / ".trash")
        entry = conn.execute("SELECT * FROM audit_log").fetchone()
        assert entry["action_type"] == "delete"


class TestCopyCorrected:
    def test_writes_corrected_fits_files(self, conn, item_id, tmp_path):
        src_dir = tmp_path / "src"
        make_fits(src_dir / "frame001.fit", object_val="FOV")
        make_fits(src_dir / "frame002.fit", object_val="FOV")
        dst_dir = tmp_path / "dst"

        copy_corrected(conn, item_id, src_dir, dst_dir,
                       header_patches={"OBJECT": "M 81"})

        assert (dst_dir / "frame001.fit").exists()
        with fits.open(dst_dir / "frame001.fit") as hdul:
            assert hdul[0].header["OBJECT"] == "M 81"

    def test_preserves_mtime(self, conn, item_id, tmp_path):
        import os, time
        src_dir = tmp_path / "src"
        f = make_fits(src_dir / "frame.fit")
        original_mtime = f.stat().st_mtime
        dst_dir = tmp_path / "dst"

        copy_corrected(conn, item_id, src_dir, dst_dir,
                       header_patches={"OBJECT": "M 81"})

        copied = dst_dir / "frame.fit"
        assert abs(copied.stat().st_mtime - original_mtime) < 2.0

    def test_skips_non_fits_files(self, conn, item_id, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "thumb_thn.jpg").write_bytes(b"jpg")
        dst_dir = tmp_path / "dst"

        copy_corrected(conn, item_id, src_dir, dst_dir,
                       header_patches={"OBJECT": "M 81"})

        assert not (dst_dir / "thumb_thn.jpg").exists()


class TestRevert:
    def test_reverts_move(self, conn, item_id, tmp_path):
        src = tmp_path / "orig"
        src.mkdir()
        dst = tmp_path / "moved"
        move(conn, item_id, src, dst)

        log_id = conn.execute("SELECT id FROM audit_log").fetchone()["id"]
        revert(conn, log_id, trash_root=tmp_path / ".trash")

        assert src.exists()
        assert not dst.exists()

    def test_revert_copy_corrected_deletes_copy(self, conn, item_id, tmp_path):
        src_dir = tmp_path / "src"
        make_fits(src_dir / "frame.fit")
        dst_dir = tmp_path / "dst"
        copy_corrected(conn, item_id, src_dir, dst_dir,
                       header_patches={"OBJECT": "M 81"})

        log_id = conn.execute(
            "SELECT id FROM audit_log WHERE action_type = 'copy_corrected'"
        ).fetchone()["id"]
        revert(conn, log_id, trash_root=tmp_path / ".trash")

        assert not dst_dir.exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_actions.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'darkroom.triage.actions'`

- [ ] **Step 3: Implement `darkroom/triage/actions.py`**

```python
# darkroom/triage/actions.py
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

from astropy.io import fits

from darkroom.triage.db import complete_action, log_action, mark_reverted
from darkroom.parse import fits_files

_FITS_SUFFIXES = {".fit", ".fits"}


def _sha256_first_fits(directory: Path) -> str | None:
    for p in sorted(directory.rglob("*")):
        if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            return h
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
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/triage/test_actions.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/actions.py tests/triage/test_actions.py
git commit -m "feat(triage): file actions — move/rename/trash/copy_corrected with audit log"
```

---

## Task 4: FITS checks (`checks.py`)

**Files:**
- Create: `darkroom/triage/checks.py`
- Create: `tests/triage/test_checks.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/triage/test_checks.py
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest
from astropy.io import fits

from darkroom.triage.checks import (
    check_object_value,
    check_fits_object,
    check_ra_dec,
)


def make_fits(path: Path, **headers) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((10, 10), dtype=np.uint16))
    for k, v in headers.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


class TestCheckObjectValue:
    def test_none_is_missing(self):
        assert check_object_value(None) == "MISSING"

    def test_blank_is_missing(self):
        assert check_object_value("  ") == "MISSING"

    def test_fov_detected(self):
        assert check_object_value("FOV") == "FOV"
        assert check_object_value("fov") == "FOV"

    def test_valid_returns_none(self):
        assert check_object_value("M 81") is None
        assert check_object_value("NGC 6960") is None


class TestCheckFitsObject:
    def test_good_object(self, tmp_path):
        f = make_fits(tmp_path / "good.fit", OBJECT="NGC 6960")
        reason, val = check_fits_object(f)
        assert reason is None
        assert val == "NGC 6960"

    def test_fov_object(self, tmp_path):
        f = make_fits(tmp_path / "fov.fit", OBJECT="FOV")
        reason, val = check_fits_object(f)
        assert reason == "FOV"

    def test_missing_object(self, tmp_path):
        f = make_fits(tmp_path / "missing.fit")
        reason, val = check_fits_object(f)
        assert reason == "MISSING"


class TestCheckRaDec:
    def test_matching_coords_returns_none(self, tmp_path):
        # M 81 is at RA~148.9, Dec~69.1
        f = make_fits(tmp_path / "m81.fit", RA=148.888, DEC=69.065)
        mock_table = MagicMock()
        mock_table["RA"][0] = 148.888
        mock_table["DEC"][0] = 69.065

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 81", threshold_deg=5.0)

        assert result is None

    def test_mismatch_returns_dict(self, tmp_path):
        # Frame points at M 81 but folder says NGC 224 (M 31 — far away)
        f = make_fits(tmp_path / "wrong.fit", RA=148.888, DEC=69.065)
        mock_table = MagicMock()
        mock_table["RA"][0] = 10.685  # M 31
        mock_table["DEC"][0] = 41.269

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 31", threshold_deg=5.0)

        assert result is not None
        assert result["separation_deg"] > 5.0
        assert "simbad_ra" in result

    def test_no_ra_dec_header_returns_none(self, tmp_path):
        f = make_fits(tmp_path / "noradec.fit", OBJECT="M 81")
        result = check_ra_dec(f, "M 81")
        assert result is None

    def test_simbad_unknown_target_returns_none(self, tmp_path):
        f = make_fits(tmp_path / "frame.fit", RA=10.0, DEC=20.0)
        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = None
            result = check_ra_dec(f, "Unknown Nebula X")
        assert result is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_checks.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'darkroom.triage.checks'`

- [ ] **Step 3: Implement `darkroom/triage/checks.py`**

```python
# darkroom/triage/checks.py
from __future__ import annotations

from pathlib import Path

from astropy.coordinates import SkyCoord
from astropy.io import fits
from astroquery.simbad import Simbad


def check_object_value(value: str | None) -> str | None:
    """Return 'MISSING' or 'FOV' if the OBJECT header is problematic, else None."""
    if value is None or str(value).strip() == "":
        return "MISSING"
    if str(value).strip().upper() == "FOV":
        return "FOV"
    return None


def check_fits_object(fits_path: Path) -> tuple[str | None, str | None]:
    """Return (reason, object_val). reason is None if OBJECT is valid."""
    try:
        with fits.open(fits_path) as hdul:
            val = hdul[0].header.get("OBJECT")
    except Exception:
        return ("MISSING", None)
    return (check_object_value(val), str(val).strip() if val else None)


def check_ra_dec(
    fits_path: Path,
    target_name: str,
    threshold_deg: float = 5.0,
    simbad_cache: dict | None = None,
) -> dict | None:
    """
    Return a dict with mismatch details if RA/DEC is > threshold_deg from the
    SIMBAD position for target_name. Returns None if coords agree or can't be checked.
    """
    try:
        with fits.open(fits_path) as hdul:
            hdr = hdul[0].header
            ra = hdr.get("RA") or hdr.get("OBJCTRA")
            dec = hdr.get("DEC") or hdr.get("OBJCTDEC")
    except Exception:
        return None

    if ra is None or dec is None:
        return None

    if simbad_cache and "ra" in simbad_cache:
        simbad_ra = simbad_cache["ra"]
        simbad_dec = simbad_cache["dec"]
    else:
        table = Simbad.query_object(target_name)
        if table is None:
            return None
        simbad_ra = float(table["RA"][0])
        simbad_dec = float(table["DEC"][0])

    frame_coord = SkyCoord(ra=float(ra), dec=float(dec), unit="deg")
    simbad_coord = SkyCoord(ra=simbad_ra, dec=simbad_dec, unit="deg")
    sep = frame_coord.separation(simbad_coord).deg

    if sep <= threshold_deg:
        return None

    return {
        "frame_ra": float(ra),
        "frame_dec": float(dec),
        "simbad_ra": simbad_ra,
        "simbad_dec": simbad_dec,
        "separation_deg": sep,
        "target_name": target_name,
    }
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/triage/test_checks.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/checks.py tests/triage/test_checks.py
git commit -m "feat(triage): FITS header checks — OBJECT and RA/DEC vs SIMBAD"
```

---

## Task 5: FITS preview (`preview.py`)

**Files:**
- Create: `darkroom/triage/preview.py`
- Create: `tests/triage/test_preview.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/triage/test_preview.py
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image

from darkroom.triage.preview import generate_thumbnail


def make_mono_fits(path: Path, shape=(100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.random.randint(100, 1000, shape, dtype=np.uint16)
    hdu = fits.PrimaryHDU(data=data)
    hdu.writeto(path, overwrite=True)
    return path


def make_bayer_fits(path: Path, shape=(100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.random.randint(100, 4000, shape, dtype=np.uint16)
    hdu = fits.PrimaryHDU(data=data)
    hdu.header["BAYERPAT"] = "RGGB"
    hdu.writeto(path, overwrite=True)
    return path


class TestGenerateThumbnail:
    def test_produces_jpeg(self, tmp_path):
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        result = generate_thumbnail(src, cache)
        assert result.suffix == ".jpg"
        assert result.exists()

    def test_valid_image(self, tmp_path):
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        jpg = generate_thumbnail(src, cache)
        img = Image.open(jpg)
        assert img.size[0] <= 600

    def test_bayer_produces_rgb(self, tmp_path):
        src = make_bayer_fits(tmp_path / "bayer.fit")
        cache = tmp_path / ".cache"
        jpg = generate_thumbnail(src, cache)
        img = Image.open(jpg)
        assert img.mode == "RGB"

    def test_cached_on_second_call(self, tmp_path):
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        jpg1 = generate_thumbnail(src, cache)
        mtime1 = jpg1.stat().st_mtime
        jpg2 = generate_thumbnail(src, cache)
        assert jpg1 == jpg2
        assert jpg2.stat().st_mtime == mtime1  # not regenerated

    def test_regenerates_if_source_newer(self, tmp_path):
        import time
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        jpg = generate_thumbnail(src, cache)
        old_mtime = jpg.stat().st_mtime
        time.sleep(0.05)
        make_mono_fits(src)  # overwrite → newer mtime
        jpg2 = generate_thumbnail(src, cache)
        assert jpg2.stat().st_mtime > old_mtime
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_preview.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'darkroom.triage.preview'`

- [ ] **Step 3: Implement `darkroom/triage/preview.py`**

```python
# darkroom/triage/preview.py
from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
from astropy.io import fits
from astropy.visualization import AsinhStretch, ImageNormalize, ZScaleInterval
from PIL import Image


_BAYER_PATTERNS = {
    "RGGB": cv2.COLOR_BayerRG2RGB,
    "BGGR": cv2.COLOR_BayerBG2RGB,
    "GRBG": cv2.COLOR_BayerGR2RGB,
    "GBRG": cv2.COLOR_BayerGB2RGB,
}


def _cache_key(fits_path: Path) -> str:
    stat = fits_path.stat()
    raw = f"{fits_path}:{stat.st_mtime}:{stat.st_size}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def generate_thumbnail(
    fits_path: Path,
    cache_dir: Path,
    max_width: int = 600,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(fits_path)
    jpg_path = cache_dir / f"{key}.jpg"

    if jpg_path.exists():
        if jpg_path.stat().st_mtime >= fits_path.stat().st_mtime:
            return jpg_path

    with fits.open(fits_path) as hdul:
        data = hdul[0].data.astype(np.float32)
        bayer = hdul[0].header.get("BAYERPAT", "").strip().upper()

    norm = ImageNormalize(data, interval=ZScaleInterval(), stretch=AsinhStretch())
    stretched = norm(data)  # 0..1 float

    if bayer in _BAYER_PATTERNS:
        u16 = (stretched * 65535).astype(np.uint16)
        rgb = cv2.cvtColor(u16, _BAYER_PATTERNS[bayer])
        u8 = (rgb / 256).astype(np.uint8)
        img = Image.fromarray(u8, mode="RGB")
    else:
        u8 = (stretched * 255).astype(np.uint8)
        img = Image.fromarray(u8, mode="L").convert("RGB")

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    img.save(jpg_path, "JPEG", quality=85)
    return jpg_path
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/triage/test_preview.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/preview.py tests/triage/test_preview.py
git commit -m "feat(triage): FITS thumbnail generator — debayer + ZScale stretch"
```

---

## Task 6: Scanner — structural categories

**Files:**
- Create: `darkroom/triage/scanner.py` (initial, structural categories only)
- Create: `tests/triage/test_scanner.py` (structural tests)

- [ ] **Step 1: Write failing tests**

```python
# tests/triage/test_scanner.py
import re
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from darkroom.triage.scanner import (
    TriageCandidate,
    scan_flat_restructure,
    scan_calibration_in_target,
    scan_processed_dirs,
    scan_thumbnail_cleanup,
    scan_legacy_sessions,
    scan_archive,
)

_FLAT_DATE_RE = re.compile(r"^\d{8}_")
_CANONICAL_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\w+_\w+")


def make_fits(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16))
    hdu.header["OBJECT"] = "M 81"
    hdu.header["FOCALLEN"] = 400
    hdu.writeto(path, overwrite=True)
    return path


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "staging"


class TestScanFlatRestructure:
    def test_detects_yyyymmdd_flat_folder(self, archive):
        flat_dir = archive / "00_Calibration" / "Flats" / "20240110_FMA180_Canon6D_L-Pro"
        flat_dir.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert len(candidates) == 1
        c = candidates[0]
        assert c.category == "flat_restructure"
        assert "FMA180_Canon6D_L-Pro" in c.proposed_path
        assert "2024-01-10" in c.proposed_path

    def test_skips_already_canonical(self, archive):
        canon = (archive / "00_Calibration" / "Flats"
                 / "FMA180_Canon6D_L-Pro" / "2024-01-10")
        canon.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert candidates == []

    def test_normalises_nofilter_typo(self, archive):
        flat_dir = (archive / "00_Calibration" / "Flats"
                    / "20250203_FRA400_Canon6D_NoFIlter")
        flat_dir.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert candidates[0].proposed_path.endswith("NoFilter/2025-02-03")

    def test_unknown_ota_flagged(self, archive):
        flat_dir = (archive / "00_Calibration" / "Flats"
                    / "20230716_100mm_Canon6D")
        flat_dir.mkdir(parents=True)
        candidates = scan_flat_restructure(archive / "00_Calibration")
        assert len(candidates) == 1
        assert candidates[0].proposed_path is None  # can't auto-map


class TestScanCalibrationInTarget:
    def test_detects_flats_subdir(self, archive):
        flats = (archive / "04_Deep Sky Objects" / "M 42" / "2025-01-17" / "Flats")
        flats.mkdir(parents=True)
        make_fits(flats / "flat001.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert any(c.category == "calibration_in_target" for c in candidates)

    def test_case_insensitive(self, archive):
        darks = (archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23" / "darks")
        darks.mkdir(parents=True)
        make_fits(darks / "dark001.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert len(candidates) == 1

    def test_plural_variants(self, archive):
        for name in ("flat", "Flats", "bias", "biases", "flatdarks"):
            d = (archive / "04_Deep Sky Objects" / "NGC 6960" / "2024-01-01" / name)
            d.mkdir(parents=True)
            make_fits(d / "frame.fit")
        candidates = scan_calibration_in_target(archive / "04_Deep Sky Objects")
        assert len(candidates) == 5


class TestScanProcessedDirs:
    def test_detects_pixinsight_dir(self, archive):
        pi = archive / "04_Deep Sky Objects" / "NGC 6960" / "Pixinsight"
        pi.mkdir(parents=True)
        (pi / "project.pxiproject").write_text("x")
        candidates = scan_processed_dirs(archive / "04_Deep Sky Objects")
        assert len(candidates) == 1
        assert candidates[0].category == "processed_dir"
        assert candidates[0].proposed_path.endswith("_Processed")

    def test_skips_already_canonical(self, archive):
        proc = archive / "04_Deep Sky Objects" / "NGC 6960" / "_Processed"
        proc.mkdir(parents=True)
        candidates = scan_processed_dirs(archive / "04_Deep Sky Objects")
        assert candidates == []


class TestScanThumbnailCleanup:
    def test_detects_thn_jpg(self, archive):
        thn = archive / "04_Deep Sky Objects" / "M 81" / "2024-01-01" / "img_thn.jpg"
        thn.parent.mkdir(parents=True)
        thn.write_bytes(b"jpg")
        candidates = scan_thumbnail_cleanup(archive)
        assert len(candidates) == 1
        assert candidates[0].category == "thumbnail_cleanup"

    def test_case_insensitive_extension(self, archive):
        thn = archive / "frame_thn.JPG"
        archive.mkdir(parents=True, exist_ok=True)
        thn.write_bytes(b"jpg")
        candidates = scan_thumbnail_cleanup(archive)
        assert len(candidates) == 1


class TestScanLegacySessions:
    def test_detects_date_only_folder(self, archive):
        session = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23"
        make_fits(session / "Lights" / "frame.fit")
        candidates = scan_legacy_sessions(archive / "04_Deep Sky Objects")
        assert any(c.category == "legacy_session" for c in candidates)

    def test_skips_canonical_session(self, archive):
        session = (archive / "04_Deep Sky Objects" / "M 42"
                   / "2026-02-22_FRA400_ZWOASI585MCPro")
        make_fits(session / "Lights" / "L-Pro" / "frame.fit")
        candidates = scan_legacy_sessions(archive / "04_Deep Sky Objects")
        assert candidates == []

    def test_skips_processed_dirs(self, archive):
        proc = archive / "04_Deep Sky Objects" / "M 42" / "_Processed"
        proc.mkdir(parents=True)
        candidates = scan_legacy_sessions(archive / "04_Deep Sky Objects")
        assert candidates == []
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_scanner.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'darkroom.triage.scanner'`

- [ ] **Step 3: Implement `darkroom/triage/scanner.py`**

```python
# darkroom/triage/scanner.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from astropy.io import fits

from darkroom.parse import ota_from_focallen

_FITS_SUFFIXES = {".fit", ".fits"}
_CALIB_NAMES = {
    "flat", "flats", "dark", "darks", "bias", "biases", "flatdark", "flatdarks",
}
_CANONICAL_SESSION_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_[A-Za-z0-9]+_[A-Za-z0-9]+"
)
_FLAT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(.+)$")
_SKIP_TARGET_CHILDREN = {"_Processed", "Pixinsight", ".DS_Store"}
_KNOWN_OTA = {"FMA180", "FRA400"}
_FILTER_NORMALISE = {"nofilter": "NoFilter", "nofilIer": "NoFilter"}


@dataclass
class TriageCandidate:
    category: str
    source_path: str
    proposed_path: str | None = None
    proposed_value: str | None = None
    fits_metadata: dict = field(default_factory=dict)


def _has_fits(directory: Path) -> bool:
    return any(
        p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name
        for p in directory.rglob("*")
        if p.is_file()
    )


def _sample_focallen(directory: Path) -> int | None:
    for p in directory.rglob("*"):
        if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name:
            try:
                with fits.open(p) as hdul:
                    val = hdul[0].header.get("FOCALLEN")
                    if val is not None:
                        return int(float(val))
            except Exception:
                pass
    return None


def _normalise_filter(name: str) -> str:
    return _FILTER_NORMALISE.get(name, _FILTER_NORMALISE.get(name.lower(), name))


def scan_flat_restructure(calibration_root: Path) -> list[TriageCandidate]:
    """Detect YYYYMMDD_OTA_Camera[_Filter] flat folders needing restructuring."""
    flats_dir = calibration_root / "Flats"
    if not flats_dir.exists():
        return []
    candidates = []
    for child in flats_dir.iterdir():
        if not child.is_dir():
            continue
        m = _FLAT_DATE_RE.match(child.name)
        if not m:
            continue  # already canonical OTA_Camera/Date structure
        year, month, day, rest = m.groups()
        date_str = f"{year}-{month}-{day}"
        # rest = OTA_Camera[_Filter] or just OTA_Camera
        parts = rest.split("_", 2)
        ota = parts[0] if parts else rest
        # Normalise filter name if present
        if len(parts) >= 3:
            parts[2] = _normalise_filter(parts[2])
        rest_normalised = "_".join(parts)
        if ota not in _KNOWN_OTA:
            # Unknown OTA (old lens) — flag but can't auto-propose
            candidates.append(TriageCandidate(
                category="flat_restructure",
                source_path=str(child),
                proposed_path=None,
                fits_metadata={"raw_name": child.name, "unknown_ota": ota},
            ))
        else:
            proposed = str(flats_dir / rest_normalised / date_str)
            candidates.append(TriageCandidate(
                category="flat_restructure",
                source_path=str(child),
                proposed_path=proposed,
                fits_metadata={"raw_name": child.name},
            ))
    return candidates


def scan_calibration_in_target(dso_root: Path) -> list[TriageCandidate]:
    """Find calibration subdirs (Flats/Darks/Bias etc.) inside target session folders."""
    candidates = []
    for target_dir in dso_root.iterdir():
        if not target_dir.is_dir():
            continue
        for subdir in target_dir.rglob("*"):
            if not subdir.is_dir():
                continue
            if subdir.name.lower() in _CALIB_NAMES and _has_fits(subdir):
                candidates.append(TriageCandidate(
                    category="calibration_in_target",
                    source_path=str(subdir),
                    proposed_path=None,  # destination requires user input
                    fits_metadata={"parent": str(subdir.parent)},
                ))
    return candidates


def scan_processed_dirs(dso_root: Path) -> list[TriageCandidate]:
    """Detect Pixinsight/ dirs that should be renamed to _Processed/."""
    candidates = []
    for target_dir in dso_root.iterdir():
        if not target_dir.is_dir():
            continue
        for child in target_dir.iterdir():
            if child.is_dir() and child.name == "Pixinsight":
                proposed = str(target_dir / "_Processed")
                candidates.append(TriageCandidate(
                    category="processed_dir",
                    source_path=str(child),
                    proposed_path=proposed,
                ))
    return candidates


def scan_thumbnail_cleanup(archive_root: Path) -> list[TriageCandidate]:
    """Find ASIAir _thn.jpg thumbnail files throughout the archive."""
    candidates = []
    for p in archive_root.rglob("*_thn.jpg"):
        candidates.append(TriageCandidate(
            category="thumbnail_cleanup",
            source_path=str(p),
        ))
    for p in archive_root.rglob("*_thn.JPG"):
        candidates.append(TriageCandidate(
            category="thumbnail_cleanup",
            source_path=str(p),
        ))
    return candidates


def scan_legacy_sessions(dso_root: Path) -> list[TriageCandidate]:
    """
    Detect session dirs inside target dirs that don't match the canonical
    YYYY-MM-DD_OTA_Camera pattern.
    """
    candidates = []
    for target_dir in dso_root.iterdir():
        if not target_dir.is_dir():
            continue
        for child in target_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name in _SKIP_TARGET_CHILDREN:
                continue
            if child.name.startswith("."):
                continue
            if _CANONICAL_SESSION_RE.match(child.name):
                continue
            if not _has_fits(child):
                continue
            # Flag as legacy session
            focal = _sample_focallen(child)
            ota = ota_from_focallen(focal) if focal else None
            candidates.append(TriageCandidate(
                category="legacy_session",
                source_path=str(child),
                proposed_path=None,
                fits_metadata={
                    "target": target_dir.name,
                    "focallen": focal,
                    "suggested_ota": ota,
                },
            ))
    return candidates


def scan_archive(archive_root: Path) -> list[TriageCandidate]:
    """Run all structural scanners and return combined candidates."""
    calib = archive_root / "00_Calibration"
    dso = archive_root / "04_Deep Sky Objects"
    results: list[TriageCandidate] = []
    if calib.exists():
        results += scan_flat_restructure(calib)
    if dso.exists():
        results += scan_calibration_in_target(dso)
        results += scan_processed_dirs(dso)
        results += scan_legacy_sessions(dso)
    results += scan_thumbnail_cleanup(archive_root)
    return results
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/triage/test_scanner.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/scanner.py tests/triage/test_scanner.py
git commit -m "feat(triage): scanner — structural categories (flat_restructure, calibration_in_target, processed_dir, thumbnail_cleanup, legacy_session)"
```

---

## Task 7: Scanner — FITS header categories + `scan_all`

**Files:**
- Modify: `darkroom/triage/scanner.py`
- Modify: `tests/triage/test_scanner.py`

- [ ] **Step 1: Write failing tests (append to test_scanner.py)**

```python
# Append to tests/triage/test_scanner.py
from unittest.mock import patch, MagicMock
from darkroom.triage.scanner import scan_fits_headers


class TestScanFitsHeaders:
    def test_detects_missing_object(self, archive):
        session = archive / "04_Deep Sky Objects" / "M 81" / "2023-08-06"
        make_fits(session / "Lights" / "frame.fit")
        # Patch check_fits_object to return MISSING for any file
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("MISSING", None)):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert any(c.category == "missing_object" for c in candidates)

    def test_detects_fov_object(self, archive):
        session = archive / "04_Deep Sky Objects" / "NGC 6960" / "2024-05-07"
        make_fits(session / "Lights" / "frame.fit")
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("FOV", "FOV")):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert any(c.category == "missing_object" for c in candidates)

    def test_proposes_object_from_folder_name(self, archive):
        session = archive / "04_Deep Sky Objects" / "NGC 6960" / "2024-05-07"
        make_fits(session / "Lights" / "frame.fit")
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=("FOV", "FOV")):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        c = next(c for c in candidates if c.category == "missing_object")
        assert c.proposed_value == "NGC 6960"

    def test_ra_dec_mismatch_flagged(self, archive):
        session = archive / "04_Deep Sky Objects" / "M 81" / "2024-01-01"
        make_fits(session / "Lights" / "frame.fit")
        mismatch = {"separation_deg": 30.0, "simbad_ra": 10.0, "simbad_dec": 40.0,
                    "frame_ra": 100.0, "frame_dec": 20.0, "target_name": "M 81"}
        with patch("darkroom.triage.scanner.check_fits_object",
                   return_value=(None, "M 81")), \
             patch("darkroom.triage.scanner.check_ra_dec", return_value=mismatch):
            candidates = scan_fits_headers(archive / "04_Deep Sky Objects")
        assert any(c.category == "ra_dec_mismatch" for c in candidates)
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_scanner.py::TestScanFitsHeaders -v 2>&1 | head -15
```

Expected: `ImportError` on `scan_fits_headers`

- [ ] **Step 3: Add `scan_fits_headers` and update `scan_archive` in scanner.py**

Add these imports at the top of `scanner.py`:

```python
from darkroom.triage.checks import check_fits_object, check_ra_dec
```

Add this function after `scan_legacy_sessions`:

```python
def scan_fits_headers(dso_root: Path) -> list[TriageCandidate]:
    """
    Sample one FITS file per Lights/ directory to detect missing/FOV OBJECT
    headers and RA/DEC mismatches. One candidate per session folder, not per file.
    """
    candidates = []
    seen_sessions: set[str] = set()

    for lights_dir in dso_root.rglob("Lights"):
        if not lights_dir.is_dir():
            continue
        session_dir = lights_dir.parent
        if str(session_dir) in seen_sessions:
            continue
        target_dir = session_dir.parent
        target_name = target_dir.name

        # Sample first FITS file
        sample = next(
            (p for p in sorted(lights_dir.rglob("*"))
             if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name),
            None,
        )
        if sample is None:
            continue

        seen_sessions.add(str(session_dir))
        reason, obj_val = check_fits_object(sample)

        if reason is not None:
            # Corrected copy goes to staging/_corrected/<relative>
            corrected = str(
                dso_root.parent / "_corrected"
                / session_dir.relative_to(dso_root.parent)
            )
            candidates.append(TriageCandidate(
                category="missing_object",
                source_path=str(session_dir),
                proposed_path=corrected,
                proposed_value=target_name,
                fits_metadata={
                    "sample_file": str(sample),
                    "object_val": obj_val,
                    "reason": reason,
                    "target": target_name,
                },
            ))
            continue

        # Only check RA/DEC if OBJECT is valid
        mismatch = check_ra_dec(sample, target_name)
        if mismatch:
            corrected = str(
                dso_root.parent / "_corrected"
                / session_dir.relative_to(dso_root.parent)
            )
            candidates.append(TriageCandidate(
                category="ra_dec_mismatch",
                source_path=str(session_dir),
                proposed_path=corrected,
                proposed_value=target_name,
                fits_metadata={
                    "sample_file": str(sample),
                    **mismatch,
                },
            ))

    return candidates
```

Update `scan_archive` to include `scan_fits_headers`:

```python
def scan_archive(archive_root: Path) -> list[TriageCandidate]:
    """Run all scanners and return combined candidates."""
    calib = archive_root / "00_Calibration"
    dso = archive_root / "04_Deep Sky Objects"
    results: list[TriageCandidate] = []
    if calib.exists():
        results += scan_flat_restructure(calib)
    if dso.exists():
        results += scan_calibration_in_target(dso)
        results += scan_processed_dirs(dso)
        results += scan_legacy_sessions(dso)
        results += scan_fits_headers(dso)
    results += scan_thumbnail_cleanup(archive_root)
    return results
```

- [ ] **Step 4: Run all scanner tests**

```bash
uv run pytest tests/triage/test_scanner.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/scanner.py tests/triage/test_scanner.py
git commit -m "feat(triage): scanner — FITS header checks (missing_object, ra_dec_mismatch) + scan_archive"
```

---

## Task 8: FastAPI server (`server.py`)

**Files:**
- Create: `darkroom/triage/server.py`
- Create: `tests/triage/test_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/triage/test_server.py
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from darkroom.triage.db import open_db, upsert_item, update_status
from darkroom.triage.server import create_app


@pytest.fixture
def db_and_archive(tmp_path):
    archive = tmp_path / "staging"
    (archive / "04_Deep Sky Objects").mkdir(parents=True)
    (archive / "00_Calibration").mkdir(parents=True)
    trash_root = archive / ".triage_trash"
    corrected_root = archive / "_corrected"
    cache_dir = archive / ".triage_cache"
    db_path = tmp_path / "triage.db"
    conn = open_db(db_path)
    return conn, db_path, archive


@pytest.fixture
def client(db_and_archive):
    conn, db_path, archive = db_and_archive
    app = create_app(db_path=db_path, archive_root=archive)
    return TestClient(app), conn


class TestDashboard:
    def test_returns_200(self, client):
        c, conn = client
        resp = c.get("/")
        assert resp.status_code == 200
        assert "triage" in resp.text.lower()


class TestQueue:
    def test_empty_queue(self, client):
        c, conn = client
        resp = c.get("/queue")
        assert resp.status_code == 200

    def test_shows_items(self, client):
        c, conn = client
        upsert_item(conn, category="flat_restructure",
                    source_path="/s/foo", proposed_path="/s/bar")
        resp = c.get("/queue")
        assert "flat_restructure" in resp.text


class TestItemDetail:
    def test_item_returns_200(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/x", proposed_path="/s/y")
        resp = c.get(f"/item/{item_id}")
        assert resp.status_code == 200

    def test_missing_item_returns_404(self, client):
        c, conn = client
        resp = c.get("/item/9999")
        assert resp.status_code == 404


class TestItemActions:
    def test_approve_sets_status(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/a", proposed_path="/s/b")
        resp = c.post(f"/item/{item_id}/approve")
        assert resp.status_code in (200, 303)
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "approved"

    def test_skip_sets_status(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/c", proposed_path="/s/d")
        c.post(f"/item/{item_id}/skip")
        row = conn.execute(
            "SELECT status FROM triage_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row[0] == "skipped"


class TestCommitPage:
    def test_commit_page_loads(self, client):
        c, conn = client
        resp = c.get("/commit")
        assert resp.status_code == 200

    def test_shows_approved_items(self, client):
        c, conn = client
        item_id = upsert_item(conn, category="flat_restructure",
                              source_path="/s/p", proposed_path="/s/q")
        update_status(conn, item_id, "approved")
        resp = c.get("/commit")
        assert "/s/p" in resp.text


class TestAuditPage:
    def test_audit_returns_200(self, client):
        c, conn = client
        resp = c.get("/audit")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/triage/test_server.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'darkroom.triage.server'`

- [ ] **Step 3: Implement `darkroom/triage/server.py`**

```python
# darkroom/triage/server.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from darkroom.triage import db as triage_db
from darkroom.triage.actions import copy_corrected, move, rename, trash, revert
from darkroom.triage.preview import generate_thumbnail

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "triage"

_CATEGORIES = [
    "flat_restructure",
    "calibration_in_target",
    "legacy_session",
    "processed_dir",
    "thumbnail_cleanup",
    "missing_object",
    "ra_dec_mismatch",
]


def create_app(*, db_path: Path, archive_root: Path) -> FastAPI:
    app = FastAPI(title="darkroom triage")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    cache_dir = archive_root / ".triage_cache"
    trash_root = archive_root / ".triage_trash"
    corrected_root = archive_root / "_corrected"
    cache_dir.mkdir(exist_ok=True)

    app.mount(
        "/thumbnails",
        StaticFiles(directory=str(cache_dir), check_dir=False),
        name="thumbnails",
    )

    def _conn():
        return triage_db.open_db(db_path)

    def _counts(conn):
        total = triage_db.count_items(conn)
        done = triage_db.count_items(conn, status="applied")
        by_cat = {
            cat: {
                "pending": triage_db.count_items(conn, category=cat, status="pending"),
                "approved": triage_db.count_items(conn, category=cat, status="approved"),
                "skipped": triage_db.count_items(conn, category=cat, status="skipped"),
                "applied": triage_db.count_items(conn, category=cat, status="applied"),
            }
            for cat in _CATEGORIES
        }
        return {"total": total, "done": done, "by_cat": by_cat}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        conn = _conn()
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "counts": _counts(conn)},
        )

    @app.get("/queue", response_class=HTMLResponse)
    def queue(
        request: Request,
        category: str | None = None,
        status: str | None = None,
        offset: int = 0,
    ):
        conn = _conn()
        items = triage_db.list_items(
            conn, category=category, status=status, limit=50, offset=offset
        )
        return templates.TemplateResponse(
            "queue.html",
            {
                "request": request,
                "items": items,
                "category": category,
                "status": status,
                "offset": offset,
                "categories": _CATEGORIES,
            },
        )

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item_detail(request: Request, item_id: int):
        conn = _conn()
        item = triage_db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")

        thumbnail_url = None
        meta = item.get("fits_metadata") or {}
        sample = meta.get("sample_file")
        if sample and Path(sample).exists():
            try:
                jpg = generate_thumbnail(Path(sample), cache_dir)
                thumbnail_url = f"/thumbnails/{jpg.name}"
            except Exception:
                pass

        # Next pending item id for keyboard navigation
        rows = triage_db.list_items(conn, status="pending", limit=2)
        next_id = next(
            (r["id"] for r in rows if r["id"] != item_id), None
        )

        return templates.TemplateResponse(
            "item.html",
            {
                "request": request,
                "item": item,
                "thumbnail_url": thumbnail_url,
                "next_id": next_id,
            },
        )

    @app.post("/item/{item_id}/approve")
    def approve_item(
        item_id: int,
        proposed_path: str = Form(default=""),
        proposed_value: str = Form(default=""),
        user_notes: str = Form(default=""),
    ):
        conn = _conn()
        item = triage_db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(status_code=404)
        status = "modified" if (proposed_path or proposed_value) else "approved"
        triage_db.update_status(
            conn,
            item_id,
            status,
            user_notes=user_notes or None,
            proposed_path=proposed_path or None,
            proposed_value=proposed_value or None,
        )
        # Redirect to next pending item
        rows = triage_db.list_items(conn, status="pending", limit=1)
        if rows:
            return RedirectResponse(f"/item/{rows[0]['id']}", status_code=303)
        return RedirectResponse("/queue", status_code=303)

    @app.post("/item/{item_id}/skip")
    def skip_item(item_id: int, user_notes: str = Form(default="")):
        conn = _conn()
        triage_db.update_status(conn, item_id, "skipped",
                                 user_notes=user_notes or None)
        rows = triage_db.list_items(conn, status="pending", limit=1)
        if rows:
            return RedirectResponse(f"/item/{rows[0]['id']}", status_code=303)
        return RedirectResponse("/queue", status_code=303)

    @app.post("/item/{item_id}/flag")
    def flag_item(item_id: int, user_notes: str = Form(default="")):
        conn = _conn()
        triage_db.update_status(conn, item_id, "pending",
                                 user_notes=user_notes or None)
        return RedirectResponse(f"/item/{item_id}", status_code=303)

    @app.get("/commit", response_class=HTMLResponse)
    def commit_page(request: Request):
        conn = _conn()
        approved = triage_db.list_items(conn, status="approved", limit=500)
        modified = triage_db.list_items(conn, status="modified", limit=500)
        return templates.TemplateResponse(
            "commit.html",
            {"request": request, "items": approved + modified},
        )

    @app.post("/commit/execute")
    def commit_execute():
        """Stream SSE progress as approved items are applied."""

        def generate():
            conn = _conn()
            items = (
                triage_db.list_items(conn, status="approved", limit=500)
                + triage_db.list_items(conn, status="modified", limit=500)
            )
            for item in items:
                item_id = item["id"]
                src = Path(item["source_path"])
                dst = Path(item["proposed_path"]) if item["proposed_path"] else None
                cat = item["category"]
                try:
                    if cat == "thumbnail_cleanup":
                        trash(conn, item_id, src,
                              archive_root=archive_root, trash_root=trash_root)
                    elif cat in ("flat_restructure", "processed_dir"):
                        rename(conn, item_id, src, dst)
                    elif cat == "calibration_in_target":
                        move(conn, item_id, src, dst)
                    elif cat == "legacy_session":
                        rename(conn, item_id, src, dst)
                    elif cat in ("missing_object", "ra_dec_mismatch"):
                        patches = {}
                        if item.get("proposed_value"):
                            patches["OBJECT"] = item["proposed_value"]
                        copy_corrected(conn, item_id, src, dst, patches)
                    triage_db.update_status(conn, item_id, "applied")
                    yield f"data: {json.dumps({'id': item_id, 'result': 'success'})}\n\n"
                except Exception as exc:
                    triage_db.update_status(conn, item_id, "error")
                    yield f"data: {json.dumps({'id': item_id, 'result': 'error', 'msg': str(exc)})}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, offset: int = 0):
        conn = _conn()
        entries = triage_db.list_audit(conn, limit=100, offset=offset)
        return templates.TemplateResponse(
            "audit.html",
            {"request": request, "entries": entries, "offset": offset},
        )

    @app.post("/audit/{log_id}/revert")
    def revert_action(log_id: int):
        conn = _conn()
        entry = triage_db.get_audit_entry(conn, log_id)
        if entry is None:
            raise HTTPException(status_code=404)
        revert(conn, log_id, trash_root=trash_root)
        return RedirectResponse("/audit", status_code=303)

    @app.get("/audit/export.csv")
    def export_csv():
        conn = _conn()
        entries = triage_db.list_audit(conn, limit=10000)
        lines = ["id,triage_item_id,action_type,source_path,dest_path,result,applied_at,reverted_at"]
        for e in entries:
            lines.append(
                f"{e['id']},{e['triage_item_id']},{e['action_type']},"
                f"\"{e['source_path']}\",\"{e['dest_path']}\","
                f"{e['result'] or ''},{e['applied_at']},{e['reverted_at'] or ''}"
            )
        csv_text = "\n".join(lines)
        return StreamingResponse(
            iter([csv_text]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
        )

    return app
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/triage/test_server.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add darkroom/triage/server.py tests/triage/test_server.py
git commit -m "feat(triage): FastAPI server — dashboard, queue, item detail, commit SSE, audit"
```

---

## Task 9: HTML templates

**Files:**
- Create: `darkroom/templates/triage/base.html`
- Create: `darkroom/templates/triage/dashboard.html`
- Create: `darkroom/templates/triage/queue.html`
- Create: `darkroom/templates/triage/item.html`
- Create: `darkroom/templates/triage/commit.html`
- Create: `darkroom/templates/triage/audit.html`

- [ ] **Step 1: `base.html`**

```html
<!-- darkroom/templates/triage/base.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>darkroom triage</title>
  <script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
  <style>
    * { box-sizing: border-box; }
    body { font-family: monospace; margin: 0; background: #111; color: #ddd; }
    nav { background: #1a1a1a; padding: 0.5rem 1rem; display: flex; gap: 1.5rem; align-items: center; border-bottom: 1px solid #333; }
    nav a { color: #7af; text-decoration: none; }
    nav a:hover { color: #fff; }
    .badge { display: inline-block; padding: 0.1rem 0.4rem; border-radius: 3px; font-size: 0.75rem; }
    .badge-flat_restructure { background: #2255aa; }
    .badge-calibration_in_target { background: #aa5522; }
    .badge-legacy_session { background: #554400; }
    .badge-processed_dir { background: #225522; }
    .badge-thumbnail_cleanup { background: #333; }
    .badge-missing_object { background: #882222; }
    .badge-ra_dec_mismatch { background: #662266; }
    .badge-pending { background: #444; }
    .badge-approved { background: #226622; }
    .badge-modified { background: #446622; }
    .badge-skipped { background: #333; }
    .badge-applied { background: #115511; }
    .badge-error { background: #661111; }
    main { padding: 1rem 2rem; max-width: 1400px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #222; font-size: 0.85rem; }
    th { background: #1a1a1a; color: #888; }
    tr:hover { background: #1a1a1a; }
    a { color: #7af; }
    input, textarea, select { background: #222; border: 1px solid #444; color: #ddd; padding: 0.3rem 0.5rem; border-radius: 3px; font-family: monospace; }
    button, .btn { background: #226; border: 1px solid #448; color: #ddd; padding: 0.35rem 0.8rem; border-radius: 3px; cursor: pointer; font-family: monospace; }
    button:hover, .btn:hover { background: #338; }
    .btn-danger { background: #622; border-color: #844; }
    .btn-danger:hover { background: #733; }
    .btn-success { background: #262; border-color: #484; }
    .path { font-size: 0.8rem; color: #888; word-break: break-all; }
  </style>
</head>
<body>
<nav>
  <strong style="color:#fff">darkroom triage</strong>
  <a href="/">Dashboard</a>
  <a href="/queue">Queue</a>
  <a href="/commit">Commit</a>
  <a href="/audit">Audit Log</a>
</nav>
<main>
{% block content %}{% endblock %}
</main>
</body>
</html>
```

- [ ] **Step 2: `dashboard.html`**

```html
<!-- darkroom/templates/triage/dashboard.html -->
{% extends "triage/base.html" %}
{% block content %}
<h2>Archive Triage — Dashboard</h2>
{% set total = counts.total %}
{% set done = counts.done %}
<p>{{ done }} / {{ total }} items applied
  {% if total > 0 %}({{ (done / total * 100)|int }}%){% endif %}
</p>

<table>
  <thead>
    <tr>
      <th>Category</th><th>Pending</th><th>Approved</th><th>Skipped</th><th>Applied</th><th></th>
    </tr>
  </thead>
  <tbody>
  {% for cat, c in counts.by_cat.items() %}
    {% if c.pending + c.approved + c.skipped + c.applied > 0 %}
    <tr>
      <td><span class="badge badge-{{ cat }}">{{ cat }}</span></td>
      <td>{{ c.pending }}</td>
      <td>{{ c.approved }}</td>
      <td>{{ c.skipped }}</td>
      <td>{{ c.applied }}</td>
      <td><a href="/queue?category={{ cat }}&status=pending">Review →</a></td>
    </tr>
    {% endif %}
  {% endfor %}
  </tbody>
</table>

{% if counts.by_cat.values() | selectattr('pending', 'gt', 0) | list %}
<p style="margin-top:1.5rem">
  {% set first_pending = none %}
  <a href="/queue?status=pending" class="btn btn-success">Start reviewing →</a>
</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 3: `queue.html`**

```html
<!-- darkroom/templates/triage/queue.html -->
{% extends "triage/base.html" %}
{% block content %}
<h2>Queue</h2>
<form method="get" style="margin-bottom:1rem; display:flex; gap:0.5rem; flex-wrap:wrap">
  <select name="category">
    <option value="">All categories</option>
    {% for cat in categories %}
    <option value="{{ cat }}" {% if cat == category %}selected{% endif %}>{{ cat }}</option>
    {% endfor %}
  </select>
  <select name="status">
    <option value="">All statuses</option>
    {% for s in ['pending','approved','modified','skipped','applied','error'] %}
    <option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s }}</option>
    {% endfor %}
  </select>
  <button type="submit">Filter</button>
</form>

<table>
  <thead>
    <tr><th>ID</th><th>Category</th><th>Source path</th><th>Status</th><th></th></tr>
  </thead>
  <tbody>
  {% for item in items %}
  <tr>
    <td>{{ item.id }}</td>
    <td><span class="badge badge-{{ item.category }}">{{ item.category }}</span></td>
    <td class="path">{{ item.source_path }}</td>
    <td><span class="badge badge-{{ item.status }}">{{ item.status }}</span></td>
    <td><a href="/item/{{ item.id }}">Review →</a></td>
  </tr>
  {% else %}
  <tr><td colspan="5" style="color:#666">No items found.</td></tr>
  {% endfor %}
  </tbody>
</table>

<div style="margin-top:1rem; display:flex; gap:1rem">
  {% if offset >= 50 %}
  <a href="?category={{ category or '' }}&status={{ status or '' }}&offset={{ offset - 50 }}">← Previous</a>
  {% endif %}
  {% if items|length == 50 %}
  <a href="?category={{ category or '' }}&status={{ status or '' }}&offset={{ offset + 50 }}">Next →</a>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 4: `item.html`**

```html
<!-- darkroom/templates/triage/item.html -->
{% extends "triage/base.html" %}
{% block content %}
<div style="display:flex; gap:2rem; align-items:flex-start">

  <!-- Left: preview + metadata -->
  <div style="flex:0 0 620px">
    {% if thumbnail_url %}
    <img src="{{ thumbnail_url }}" style="max-width:600px; border:1px solid #333; display:block; margin-bottom:1rem">
    {% else %}
    <div style="width:600px;height:400px;background:#1a1a1a;display:flex;align-items:center;justify-content:center;color:#444;margin-bottom:1rem">
      No preview
    </div>
    {% endif %}

    <table>
      <tr><th>Category</th><td><span class="badge badge-{{ item.category }}">{{ item.category }}</span></td></tr>
      <tr><th>Status</th><td><span class="badge badge-{{ item.status }}">{{ item.status }}</span></td></tr>
      {% if item.fits_metadata %}
        {% for k, v in item.fits_metadata.items() %}
        <tr><th>{{ k }}</th><td class="path">{{ v }}</td></tr>
        {% endfor %}
      {% endif %}
      {% if item.simbad_cache %}
        {% for k, v in item.simbad_cache.items() %}
        <tr><th>simbad_{{ k }}</th><td>{{ v }}</td></tr>
        {% endfor %}
      {% endif %}
    </table>
  </div>

  <!-- Right: action panel -->
  <div style="flex:1; min-width:400px">
    <h3 style="margin-top:0">Item #{{ item.id }}</h3>
    <p class="path"><strong>Source:</strong> {{ item.source_path }}</p>

    <form method="post" action="/item/{{ item.id }}/approve">
      <div style="margin-bottom:0.75rem">
        <label>Proposed path / destination</label><br>
        <input type="text" name="proposed_path" id="proposed_path"
               value="{{ item.proposed_path or '' }}"
               style="width:100%; margin-top:0.3rem">
      </div>
      {% if item.category in ('missing_object', 'ra_dec_mismatch') %}
      <div style="margin-bottom:0.75rem">
        <label>Corrected OBJECT value</label><br>
        <input type="text" name="proposed_value" id="proposed_value"
               value="{{ item.proposed_value or '' }}"
               style="width:100%; margin-top:0.3rem">
      </div>
      {% endif %}
      <div style="margin-bottom:0.75rem">
        <label>Notes</label><br>
        <input type="text" name="user_notes" value="{{ item.user_notes or '' }}"
               style="width:100%; margin-top:0.3rem">
      </div>
      <div style="display:flex; gap:0.5rem; flex-wrap:wrap">
        <button type="submit" class="btn btn-success" id="btn-approve">✓ Approve (a)</button>
      </div>
    </form>

    <form method="post" action="/item/{{ item.id }}/skip" style="margin-top:0.5rem">
      <input type="hidden" name="user_notes" value="">
      <button type="submit" class="btn">⟶ Skip (s)</button>
    </form>

    {% if next_id %}
    <p style="margin-top:1.5rem"><a href="/item/{{ next_id }}">Next pending →</a></p>
    {% else %}
    <p style="margin-top:1.5rem; color:#888">No more pending items.</p>
    {% endif %}
  </div>
</div>

<script>
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'a') document.getElementById('btn-approve').closest('form').submit();
  if (e.key === 's') document.querySelector('form[action*="/skip"]').submit();
  if (e.key === 'e') { e.preventDefault(); document.getElementById('proposed_path').focus(); }
  {% if next_id %}
  if (e.key === 'j') window.location = '/item/{{ next_id }}';
  {% endif %}
});
</script>
{% endblock %}
```

- [ ] **Step 5: `commit.html`**

```html
<!-- darkroom/templates/triage/commit.html -->
{% extends "triage/base.html" %}
{% block content %}
<h2>Commit Changes</h2>
<p>{{ items|length }} item(s) approved and ready to apply.</p>

{% if items %}
<table id="commit-table">
  <thead>
    <tr><th>ID</th><th>Category</th><th>Source</th><th>Destination</th><th>Status</th></tr>
  </thead>
  <tbody>
  {% for item in items %}
  <tr id="commit-row-{{ item.id }}">
    <td>{{ item.id }}</td>
    <td><span class="badge badge-{{ item.category }}">{{ item.category }}</span></td>
    <td class="path">{{ item.source_path }}</td>
    <td class="path">{{ item.proposed_path or '—' }}</td>
    <td id="commit-status-{{ item.id }}"><span class="badge badge-{{ item.status }}">{{ item.status }}</span></td>
  </tr>
  {% endfor %}
  </tbody>
</table>

<div style="margin-top:1.5rem">
  <button class="btn btn-success" id="execute-btn" onclick="executeCommit()">⚡ Execute all</button>
  <span id="commit-progress" style="margin-left:1rem; color:#888"></span>
</div>

<script>
async function executeCommit() {
  document.getElementById('execute-btn').disabled = true;
  const prog = document.getElementById('commit-progress');
  const es = new EventSource('/commit/execute');
  let done = 0;
  es.onmessage = function(e) {
    const d = JSON.parse(e.data);
    const cell = document.getElementById('commit-status-' + d.id);
    if (cell) {
      const cls = d.result === 'success' ? 'applied' : 'error';
      cell.innerHTML = '<span class="badge badge-' + cls + '">' + d.result + '</span>';
    }
    done++;
    prog.textContent = done + ' done';
  };
  es.onerror = function() { es.close(); prog.textContent += ' (complete)'; };
}
</script>
{% else %}
<p style="color:#888">No approved items. Go to the <a href="/queue?status=pending">queue</a> to review.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: `audit.html`**

```html
<!-- darkroom/templates/triage/audit.html -->
{% extends "triage/base.html" %}
{% block content %}
<h2>Audit Log</h2>
<div style="margin-bottom:1rem">
  <a href="/audit/export.csv" class="btn">↓ Export CSV</a>
</div>
<table>
  <thead>
    <tr>
      <th>ID</th><th>Item</th><th>Action</th>
      <th>Source</th><th>Destination</th>
      <th>Result</th><th>Applied</th><th></th>
    </tr>
  </thead>
  <tbody>
  {% for e in entries %}
  <tr>
    <td>{{ e.id }}</td>
    <td>{{ e.triage_item_id }}</td>
    <td>{{ e.action_type }}</td>
    <td class="path">{{ e.source_path }}</td>
    <td class="path">{{ e.dest_path }}</td>
    <td>
      {% if e.result %}
      <span class="badge badge-{{ e.result }}">{{ e.result }}</span>
      {% else %}<span style="color:#666">—</span>{% endif %}
    </td>
    <td style="white-space:nowrap">{{ e.applied_at[:16] }}</td>
    <td>
      {% if e.result == 'success' %}
      <form method="post" action="/audit/{{ e.id }}/revert"
            onsubmit="return confirm('Revert this action?')">
        <button type="submit" class="btn btn-danger" style="font-size:0.75rem">Revert</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% else %}
  <tr><td colspan="8" style="color:#666">No audit entries yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 7: Run server tests with templates in place**

```bash
uv run pytest tests/triage/test_server.py -v
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add darkroom/templates/triage/
git commit -m "feat(triage): HTML templates — dashboard, queue, item, commit, audit"
```

---

## Task 10: CLI wiring

**Files:**
- Create: `darkroom/triage/cli.py`
- Modify: `darkroom/cli.py`

- [ ] **Step 1: Create `darkroom/triage/cli.py`**

```python
# darkroom/triage/cli.py
from __future__ import annotations

import argparse
from pathlib import Path


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("triage", help="Interactive archive triage web UI")
    sub2 = p.add_subparsers(dest="triage_cmd", required=True)

    # scan
    scan_p = sub2.add_parser("scan", help="Scan archive and populate triage.db")
    scan_p.add_argument("--archive", required=True, type=Path,
                        help="Path to staging archive root")
    scan_p.add_argument("--db", type=Path,
                        help="Path to triage.db (default: <archive>/triage.db)")
    scan_p.set_defaults(func=_cmd_scan)

    # serve
    serve_p = sub2.add_parser("serve", help="Start triage web UI")
    serve_p.add_argument("--archive", required=True, type=Path)
    serve_p.add_argument("--db", type=Path)
    serve_p.add_argument("--port", type=int, default=8002)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.set_defaults(func=_cmd_serve)

    p.set_defaults(func=lambda args: p.print_help())


def _resolve_db(args) -> Path:
    if args.db:
        return args.db
    return Path(args.archive) / "triage.db"


def _cmd_scan(args) -> None:
    from darkroom.triage.db import open_db, upsert_item
    from darkroom.triage.scanner import scan_archive

    archive = Path(args.archive)
    db_path = _resolve_db(args)
    conn = open_db(db_path)

    candidates = scan_archive(archive)
    new_count = 0
    for c in candidates:
        prev = conn.execute(
            "SELECT status FROM triage_items WHERE source_path = ?",
            (c.source_path,),
        ).fetchone()
        if prev is None:
            upsert_item(
                conn,
                category=c.category,
                source_path=c.source_path,
                proposed_path=c.proposed_path,
                proposed_value=c.proposed_value,
                fits_metadata=c.fits_metadata if c.fits_metadata else None,
            )
            new_count += 1

    total = conn.execute("SELECT COUNT(*) FROM triage_items").fetchone()[0]
    print(f"Scan complete: {new_count} new items added, {total} total in {db_path}")


def _cmd_serve(args) -> None:
    import uvicorn
    from darkroom.triage.server import create_app

    archive = Path(args.archive)
    db_path = _resolve_db(args)

    app = create_app(db_path=db_path, archive_root=archive)
    print(f"Starting triage server at http://{args.host}:{args.port}")
    print(f"  archive: {archive}")
    print(f"  db:      {db_path}")
    uvicorn.run(app, host=args.host, port=args.port)
```

- [ ] **Step 2: Wire into `darkroom/cli.py`**

```python
# darkroom/cli.py
"""darkroom CLI entry point — dispatch to subcommands."""
from __future__ import annotations

import argparse

from darkroom import catalog_cli, finish, ingest, prep, serve
from darkroom.triage import cli as triage_cli


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="darkroom",
        description="Astrophotography pipeline: catalog, archive ingestion, "
                    "WBPP session prep, and finishing.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    catalog_cli.add_subparser(sub)
    ingest.add_subparser(sub)
    prep.add_subparser(sub)
    finish.add_subparser(sub)
    serve.add_subparser(sub)
    triage_cli.add_subparser(sub)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify CLI wiring**

```bash
uv run darkroom triage --help
uv run darkroom triage scan --help
uv run darkroom triage serve --help
```

Expected: help text for each subcommand prints without errors.

- [ ] **Step 4: Smoke test against staging**

```bash
uv run darkroom triage scan --archive ./staging
```

Expected: prints something like `Scan complete: N new items added, N total in staging/triage.db`

- [ ] **Step 5: Start the server and verify it loads**

```bash
uv run darkroom triage serve --archive ./staging --port 8002
```

Open `http://localhost:8002` in a browser. Verify the dashboard shows category counts.

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: all existing tests plus new triage tests pass.

- [ ] **Step 7: Commit**

```bash
git add darkroom/triage/cli.py darkroom/cli.py
git commit -m "feat(triage): CLI wiring — darkroom triage scan/serve subcommands"
```

---

## Self-Review Checklist

- [x] All 7 triage categories covered by scanner tasks
- [x] `proposed_path=None` for `calibration_in_target` (destination needs user judgment — shown in UI)
- [x] Thumbnail cleanup uses `trash()` not permanent delete
- [x] `copy_corrected` operates at folder level, not file level
- [x] Audit log written before AND after each operation (crash-safe sentinel)
- [x] `revert()` handles all action types: move/rename/delete (restore from trash), copy_corrected (delete corrected dir)
- [x] `scan_archive` is idempotent — upserts skip non-pending rows
- [x] SIMBAD lookups mocked in tests (no network calls in test suite)
- [x] `_thn.JPG` uppercase handled in thumbnail scanner
- [x] `NoFIlter` typo normalised in flat restructure scanner
- [x] Unknown OTA (old lenses) flagged with `proposed_path=None`
