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
                    proposed_path=None,
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
