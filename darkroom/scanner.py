from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from astropy.time import Time
from darkroom.cataloger import (
    FITSHeaderExtractor,
    _normalize_camera,
    _round_exposure,
    compute_imaging_night,
)
from darkroom.parse import fits_files, parse_filter, parse_ota


@dataclass
class Session:
    target: str
    obs_date: str          # YYYY-MM-DD local imaging night
    ota: str
    camera: str
    filter: str | None     # None when not detected in filenames
    gain: int
    temperature_c: float
    exposure_sec: float
    ra_deg: float | None
    dec_deg: float | None
    files: list[Path] = field(default_factory=list)


@dataclass
class CalibrationGroup:
    frame_type: str        # Flat | Dark | FlatDark | Bias
    camera: str
    ota: str
    filter: str | None     # only for Flat and FlatDark
    gain: int
    exposure_sec: float
    temperature_c: float   # rounded to nearest integer
    capture_date: str      # YYYY-MM-DD from DATE-OBS header
    files: list[Path] = field(default_factory=list)


@dataclass
class ScanResult:
    sessions: list[Session] = field(default_factory=list)
    calibration: list[CalibrationGroup] = field(default_factory=list)


_ASIAIR_SUBDIRS = ("Autorun", "Plan")


def scan_source(source: Path) -> ScanResult:
    """Scan an ASIAir source folder for sessions and calibration groups.

    If *source* contains Light/Dark/Flat dirs directly (e.g. pointing at
    Autorun/), scan it as-is.  Otherwise, scan the Autorun/ and Plan/
    children and merge results.
    """
    roots = _resolve_scan_roots(source)
    sessions: list[Session] = []
    calibration: list[CalibrationGroup] = []
    for root in roots:
        sessions.extend(_scan_lights(root / "Light"))
        calibration.extend(_scan_calibration(root))
    return ScanResult(sessions=sessions, calibration=calibration)


def _resolve_scan_roots(source: Path) -> list[Path]:
    if (source / "Light").is_dir():
        return [source]
    roots = [source / d for d in _ASIAIR_SUBDIRS if (source / d).is_dir()]
    return roots or [source]


def _scan_lights(light_root: Path) -> list[Session]:
    if not light_root.is_dir():
        return []

    sessions: list[Session] = []
    for target_dir in sorted(light_root.iterdir()):
        if not target_dir.is_dir() or target_dir.name.startswith("."):
            continue

        pairs: list[tuple[dict, Path]] = []
        for path in fits_files(target_dir):
            meta = FITSHeaderExtractor.extract_metadata(path)
            if meta:
                pairs.append((meta, path))

        if not pairs:
            continue

        # Group by imaging night (local Lisbon civil date)
        nights: dict[str, list[tuple[dict, Path]]] = {}
        for meta, path in pairs:
            night = compute_imaging_night(meta.get("date_obs", ""))
            if night is None:
                continue
            nights.setdefault(night, []).append((meta, path))

        for night, frames in sorted(nights.items()):
            first_meta = frames[0][0]

            # Filter: first filename that carries one wins
            filter_: str | None = None
            for meta, _ in frames:
                filter_ = parse_filter(meta["filename_stem"])
                if filter_ is not None:
                    break

            sessions.append(Session(
                target=target_dir.name,
                obs_date=night,
                ota=parse_ota(first_meta.get("focallen")),
                camera=_normalize_camera(first_meta["camera"]),
                filter=filter_,
                gain=first_meta["gain"],
                temperature_c=first_meta["temperature"],
                exposure_sec=_round_exposure(first_meta["exposure"]),
                ra_deg=first_meta.get("ra_deg"),
                dec_deg=first_meta.get("dec_deg"),
                files=[path for _, path in frames],
            ))

    return sessions


def _scan_calibration(source: Path) -> list[CalibrationGroup]:
    # Darks with exposure_sec below this threshold are flat darks
    FLAT_DARK_THRESHOLD_SEC = 10.0

    groups: dict[tuple, CalibrationGroup] = {}

    for folder_name in ("Flat", "Dark", "Bias"):
        folder = source / folder_name
        if not folder.is_dir():
            continue

        for path in fits_files(folder):
            meta = FITSHeaderExtractor.extract_metadata(path)
            if not meta:
                continue

            # Frame type from source folder name; reclassify short darks as flat darks
            frame_type = folder_name
            if frame_type == "Dark" and meta["exposure"] < FLAT_DARK_THRESHOLD_SEC:
                frame_type = "FlatDark"

            # DATE-OBS → YYYY-MM-DD
            capture_date = ""
            date_obs = meta.get("date_obs", "")
            if date_obs:
                try:
                    capture_date = Time(date_obs, format="isot").datetime.strftime("%Y-%m-%d")
                except Exception:
                    pass

            # Filter only meaningful for Flat and FlatDark
            filter_: str | None = None
            if frame_type in ("Flat", "FlatDark"):
                filter_ = parse_filter(path.stem)
                if filter_ is None:
                    filter_ = meta.get("filter_header")

            temp_rounded = round(meta["temperature"])
            camera = _normalize_camera(meta["camera"])
            exposure = _round_exposure(meta["exposure"])
            ota = parse_ota(meta.get("focallen"))
            key = (frame_type, camera, ota, filter_, meta["gain"], exposure, temp_rounded, capture_date)

            if key not in groups:
                groups[key] = CalibrationGroup(
                    frame_type=frame_type,
                    camera=camera,
                    ota=ota,
                    filter=filter_,
                    gain=meta["gain"],
                    exposure_sec=exposure,
                    temperature_c=float(temp_rounded),
                    capture_date=capture_date,
                    files=[],
                )
            groups[key].files.append(path)

    return list(groups.values())
