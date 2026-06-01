import hashlib
from pathlib import Path

import pytest
from astropy.io import fits
import numpy as np

from darkroom.triage.db import open_db, upsert_item, get_audit_entry, update_status, get_item
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

    def test_revert_reopens_item_as_pending(self, conn, item_id, tmp_path):
        # After commit an item is "applied"; reverting should re-open it for
        # review (back to "pending") so it reappears in the queue.
        src = tmp_path / "orig"
        src.mkdir()
        dst = tmp_path / "moved"
        move(conn, item_id, src, dst)
        update_status(conn, item_id, "applied")  # as commit would set it

        log_id = conn.execute("SELECT id FROM audit_log").fetchone()["id"]
        revert(conn, log_id, trash_root=tmp_path / ".trash")

        assert get_item(conn, item_id)["status"] == "pending"
