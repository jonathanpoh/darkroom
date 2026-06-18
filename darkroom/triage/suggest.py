"""Best-effort canonical-path suggestions for triage items.

For ``legacy_session`` and ``calibration_in_target`` candidates the canonical
destination depends on metadata read from the FITS frames (camera, OTA, filter,
date). Where a component resolves confidently we fill it in; where it doesn't we
emit a ``{PLACEHOLDER?}`` token so the suggestion is obviously incomplete and the
user knows exactly which portion to finish before approving.

A suggestion that still contains any ``{...?}`` token is *partial* and must not
be committed as-is — ``has_placeholder`` detects this and the commit step blocks
it the same way it blocks an empty destination.
"""
from __future__ import annotations

import re
from pathlib import Path

from astropy.time import Time

from darkroom.cataloger import FITSHeaderExtractor, _normalize_camera
from darkroom.ingest import cal_dest_rel
from darkroom.parse import parse_filter, parse_ota

_FITS_SUFFIXES = {".fit", ".fits"}
_FLAT_DARK_THRESHOLD_SEC = 10.0
_PLACEHOLDER_RE = re.compile(r"\{[A-Z]+\?\}")

# Folder name (lowercased) → canonical frame type used by cal_dest_rel.
_FRAME_TYPE_BY_NAME = {
    "flat": "Flat", "flats": "Flat",
    "dark": "Dark", "darks": "Dark",
    "bias": "Bias", "biases": "Bias",
    "flatdark": "FlatDark", "flatdarks": "FlatDark",
}


def has_placeholder(path: str | None) -> bool:
    """True if the path is empty or still contains a ``{...?}`` placeholder."""
    if not path:
        return True
    return bool(_PLACEHOLDER_RE.search(path))


def _first_fits(directory: Path) -> Path | None:
    for p in sorted(directory.rglob("*")):
        if p.suffix.lower() in _FITS_SUFFIXES and "_thn" not in p.name:
            return p
    return None


def _date_from_meta(meta: dict) -> str | None:
    date_obs = meta.get("date_obs", "")
    if not date_obs:
        return None
    try:
        return Time(date_obs, format="isot").datetime.strftime("%Y-%m-%d")
    except Exception:
        return None


def suggest_legacy_session(session_dir: Path, target_dir: Path) -> tuple[str | None, list[str]]:
    """Suggest the canonical ``YYYY-MM-DD_OTA_Camera`` folder for a legacy session.

    Returns ``(absolute_path, missing_fields)``. ``missing_fields`` names the
    components that could not be resolved and were left as ``{...?}`` tokens.
    Returns ``(None, ["frames"])`` if no readable FITS frame is found.
    """
    sample = _first_fits(session_dir)
    if sample is None:
        return None, ["frames"]
    meta = FITSHeaderExtractor.extract_metadata(sample)
    if not meta:
        return None, ["frames"]

    missing: list[str] = []

    date_str = _date_from_meta(meta)
    if date_str is None:
        date_str = "{DATE?}"
        missing.append("date")

    ota = parse_ota(meta.get("focallen"))
    if not ota or ota == "Unknown":
        ota = "{OTA?}"
        missing.append("ota")

    camera = _normalize_camera(meta.get("camera"))
    if not camera or camera == "Unknown":
        camera = "{CAMERA?}"
        missing.append("camera")

    folder = f"{date_str}_{ota}_{camera}"
    return str(target_dir / folder), missing


def suggest_calibration_dest(calib_dir: Path, archive_root: Path) -> tuple[str | None, list[str]]:
    """Suggest the canonical ``00_Calibration/...`` destination for a calibration folder.

    Returns ``(absolute_path, missing_fields)``. Darks/Bias/FlatDarks are fully
    determined by camera; Flats additionally need OTA, filter, and date — any
    unresolved component becomes a ``{...?}`` token listed in ``missing_fields``.
    """
    frame_type = _FRAME_TYPE_BY_NAME.get(calib_dir.name.lower())
    if frame_type is None:
        return None, ["frame_type"]

    sample = _first_fits(calib_dir)
    if sample is None:
        return None, ["frames"]
    meta = FITSHeaderExtractor.extract_metadata(sample)
    if not meta:
        return None, ["frames"]

    # A short "Dark" is actually a flat dark, matching the ingest reclassification.
    if frame_type == "Dark" and meta.get("exposure", 0.0) < _FLAT_DARK_THRESHOLD_SEC:
        frame_type = "FlatDark"

    missing: list[str] = []

    camera = _normalize_camera(meta.get("camera"))
    if not camera or camera == "Unknown":
        camera = "{CAMERA?}"
        missing.append("camera")

    ota = ""
    filter_ = None
    capture_date = ""
    if frame_type == "Flat":
        ota = parse_ota(meta.get("focallen"))
        if not ota or ota == "Unknown":
            ota = "{OTA?}"
            missing.append("ota")
        filter_ = parse_filter(sample.stem) or meta.get("filter_header")
        if not filter_:
            filter_ = "{FILTER?}"
            missing.append("filter")
        capture_date = _date_from_meta(meta)
        if not capture_date:
            capture_date = "{DATE?}"
            missing.append("date")

    rel = cal_dest_rel(frame_type, camera, ota, filter_, capture_date)
    return str(archive_root / rel), missing
