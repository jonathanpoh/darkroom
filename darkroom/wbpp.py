# darkroom/wbpp.py
import re
import shutil
from datetime import date
from pathlib import Path

from darkroom.parse import fits_files, parse_datetime, parse_exposure


def next_session_num(target_dir: Path) -> int:
    """Return N+1 where N is the highest SESSION_N number in target_dir (or 1)."""
    nums = []
    if target_dir.exists():
        for p in target_dir.iterdir():
            m = re.fullmatch(r"SESSION_(\d+)", p.name)
            if m and p.is_dir():
                nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def discover_lights(folder: Path) -> list[Path]:
    """Return all .fit files in folder (using fits_files to exclude thumbnails)."""
    if not folder.exists():
        return []
    return fits_files(folder)


def discover_darks(folder: Path, *, exposure_sec: float) -> list[Path]:
    """Return .fit files in folder whose filename exposure matches exposure_sec."""
    if not folder.exists():
        return []
    target = f"{exposure_sec}s"
    result = []
    for f in fits_files(folder):
        exp = parse_exposure(f.stem)
        if exp == target:
            result.append(f)
    return result


def discover_flat_files(folder: Path) -> list[Path]:
    """Return all .fit files in folder (folder is already date-specific)."""
    if not folder.exists():
        return []
    return fits_files(folder)


def discover_flat_darks(folder: Path, *, capture_date: date) -> list[Path]:
    """Return .fit files in folder whose filename datetime matches capture_date."""
    if not folder.exists():
        return []
    result = []
    for f in fits_files(folder):
        dt = parse_datetime(f.stem)
        if dt is not None and dt.date() == capture_date:
            result.append(f)
    return result


def make_symlinks(files: list[Path], dest_dir: Path) -> int:
    """Create absolute symlinks in dest_dir for each file. Returns count created."""
    if not files:
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for src in files:
        link = dest_dir / src.name
        if link.exists() or link.is_symlink():
            continue
        link.symlink_to(src.resolve())
        created += 1
    return created


def find_real_files(target_dir: Path) -> list[Path]:
    """Recursively find non-symlink files under target_dir."""
    if not target_dir.exists():
        return []
    result = []
    for p in target_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            result.append(p)
    return result


def clear_sessions(target_dir: Path) -> None:
    """Delete all SESSION_N subdirectories inside target_dir."""
    if not target_dir.exists():
        return
    for p in list(target_dir.iterdir()):
        if re.fullmatch(r"SESSION_\d+", p.name) and p.is_dir():
            shutil.rmtree(p)
