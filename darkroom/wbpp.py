# darkroom/wbpp.py
import re
import shutil
from datetime import date
from pathlib import Path

from darkroom.parse import fits_files, parse_datetime, parse_exposure, parse_temperature


_SESSION_DIR_RE = re.compile(r"SESSION_(\d+)")


def session_dirs(target_dir: Path) -> list[Path]:
    """Return the SESSION_N dirs directly inside target_dir, sorted; [] if it doesn't exist.

    The one definition of what a session dir looks like — `next_session_num`,
    `clear_sessions` and `darkroom.finish` all go through it.
    """
    if not target_dir.exists():
        return []
    return sorted(
        p for p in target_dir.iterdir()
        if p.is_dir() and _SESSION_DIR_RE.fullmatch(p.name)
    )


def next_session_num(target_dir: Path) -> int:
    """Return N+1 where N is the highest SESSION_N number in target_dir (or 1)."""
    nums = [int(_SESSION_DIR_RE.fullmatch(p.name).group(1)) for p in session_dirs(target_dir)]
    return max(nums, default=0) + 1


def discover_lights(folder: Path) -> list[Path]:
    """Return all .fit files in folder (using fits_files to exclude thumbnails)."""
    if not folder.exists():
        return []
    return fits_files(folder)


def discover_darks(
    folder: Path, *, exposure_sec: float,
    temperature_c: float | None = None, temp_tolerance: float = 0.0,
) -> list[Path]:
    """Return .fit files in folder whose filename exposure matches exposure_sec.

    Raw dark sets at different temperatures share the same `Darks/<Camera>/`
    folder on the NAS, so an exposure-only scan leaks out-of-tolerance raws
    (B11 follow-up). When temperature_c is given, also drop files whose
    filename temperature differs from it by more than temp_tolerance. Files
    with no parseable temperature are kept — the catalog row doesn't
    distinguish files by temperature either, so this mirrors the NULL-passes
    rule in catalog.py:find_darks.
    """
    if not folder.exists():
        return []
    target = f"{float(exposure_sec)}s"
    result = []
    for f in fits_files(folder):
        exp = parse_exposure(f.stem)
        if exp != target:
            continue
        if temperature_c is not None:
            temp = parse_temperature(f.stem)
            if temp is not None and abs(temp - temperature_c) > temp_tolerance:
                continue
        result.append(f)
    return result


def discover_flat_files(folder: Path) -> list[Path]:
    """Return all .fit files in folder (folder is already date-specific)."""
    if not folder.exists():
        return []
    return fits_files(folder)


def discover_flat_darks(folder: Path, *, capture_date: date) -> list[Path]:
    """Return .fit files in folder whose filename datetime date matches capture_date.

    capture_date should be the flat set's capture_date from the catalog (the date
    stored in the calibration_sets row), not the imaging night date.
    """
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
    for p in session_dirs(target_dir):
        shutil.rmtree(p)
