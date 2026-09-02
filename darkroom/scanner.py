from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from darkroom.cataloger import (
    FITSHeaderExtractor,
    capture_date_of,
    compute_imaging_night,
    compute_session_span,
    parse_date_obs,
    total_integration_sec,
)
from darkroom.names import _normalize_camera, _round_exposure
from darkroom.parse import (
    calibration_filter,
    fits_files,
    parse_filter,
    parse_ota,
    parse_panel,
    reclassify_flat_dark,
)
from darkroom.sites import session_site


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
    focal_length: float | None
    ra_deg: float | None
    dec_deg: float | None
    # B17: the per-frame sum, not frame_count * exposure_sec — `exposure_sec`
    # above is only the representative frame's, so a night whose exposure
    # changed mid-run is mis-counted by the product.
    total_integration_sec: int = 0
    # M1: mosaic panel label ("1-1"), None for an ordinary single-pointing
    # session. The ASIAir writes one folder per panel ("M 8_1-1"), so this is
    # split off the folder name at scan time — see _scan_lights.
    panel: str | None = None
    site_lat: float | None = None
    site_lon: float | None = None
    start_utc: str | None = None   # earliest frame DATE-OBS (ISO UTC)
    end_utc: str | None = None     # latest frame DATE-OBS + that frame's exposure
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

        # M1: a mosaic arrives as one ASIAir folder per panel ("M 8_1-1"), so
        # the base target and the panel label are separated here, before
        # anything downstream builds a session_id or a destination path. Every
        # panel of one night therefore groups under the real object name, and
        # the panel keeps the eight rows from colliding on one session_id.
        base_target, panel = parse_panel(target_dir.name)

        pairs: list[tuple[dict, Path]] = []
        for path in fits_files(target_dir, recursive=True):
            meta = FITSHeaderExtractor.extract_metadata(path)
            if meta:
                pairs.append((meta, path))

        if not pairs:
            continue

        # Group by imaging night + filter so multi-filter nights
        # produce separate sessions (each gets its own catalog entry
        # and archive Lights/<filter>/ subdir). DATE-OBS is parsed once
        # per frame and carried along, as in SessionAnalyzer.
        groups: dict[tuple[str, str | None], list[tuple[datetime, dict, Path]]] = {}
        for meta, path in pairs:
            dt = parse_date_obs(meta.get("date_obs", ""))
            night = compute_imaging_night(dt)
            if night is None:
                continue
            filt = parse_filter(meta["filename_stem"])
            groups.setdefault((night, filt), []).append((dt, meta, path))

        for (night, filter_), frames in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            # The chronologically-first frame is the representative.
            # `frames` is in directory-walk order (filename sort), not
            # capture order — B14.  The span is derived from all frames
            # (compute_session_span sorts by DATE-OBS itself).
            first_meta = min(frames, key=lambda f: f[0])[1]

            focallen = first_meta.get("focallen")
            start_utc, end_utc = compute_session_span(
                (dt, meta.get("exposure")) for dt, meta, _ in frames
            )
            site_lat, site_lon = session_site(
                ((meta.get("site_lat"), meta.get("site_lon")) for _, meta, _ in frames),
                f"{target_dir.name} {night} {filter_ or 'no-filter'}",
            )
            sessions.append(Session(
                target=base_target,
                panel=panel,
                obs_date=night,
                ota=parse_ota(focallen, obs_date=night),
                camera=_normalize_camera(first_meta["camera"]),
                filter=filter_,
                gain=first_meta["gain"],
                temperature_c=first_meta["temperature"],
                exposure_sec=_round_exposure(first_meta["exposure"]),
                total_integration_sec=total_integration_sec(
                    meta.get("exposure") for _, meta, _ in frames
                ),
                focal_length=float(focallen) if focallen is not None else None,
                ra_deg=first_meta.get("ra_deg"),
                dec_deg=first_meta.get("dec_deg"),
                site_lat=site_lat,
                site_lon=site_lon,
                start_utc=start_utc,
                end_utc=end_utc,
                files=[path for _, _, path in frames],
            ))

    return sessions


def _scan_calibration(source: Path) -> list[CalibrationGroup]:
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
            frame_type = reclassify_flat_dark(folder_name, meta["exposure"])

            capture_date = capture_date_of(meta.get("date_obs", ""))

            filter_ = calibration_filter(path.stem, frame_type, meta.get("filter_header"))

            temp_rounded = round(meta["temperature"])
            camera = _normalize_camera(meta["camera"])
            exposure = _round_exposure(meta["exposure"])
            ota = parse_ota(meta.get("focallen"), obs_date=capture_date)
            # Flats and FlatDarks are temperature-insensitive (like bias frames),
            # so don't split groups by temperature.
            temp_key = None if frame_type in ("Flat", "FlatDark", "Bias") else temp_rounded
            key = (frame_type, camera, ota, filter_, meta["gain"], exposure, temp_key, capture_date)

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
