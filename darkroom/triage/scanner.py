from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from astropy.io import fits

from darkroom.parse import ota_from_focallen
from darkroom.triage.checks import check_fits_object, check_ra_dec
from darkroom.triage.suggest import suggest_calibration_dest, suggest_legacy_session

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
_FILTER_NORMALISE = {"nofilter": "NoFilter"}


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
            # Skip processed-output subtrees — they may contain dark/flat-named
            # folders that aren't raw calibration frames.
            rel_parts = subdir.relative_to(target_dir).parts
            if any(part in ("_Processed", "Pixinsight") for part in rel_parts):
                continue
            if subdir.name.lower() in _CALIB_NAMES and _has_fits(subdir):
                proposed, missing = suggest_calibration_dest(
                    subdir, dso_root.parent
                )
                candidates.append(TriageCandidate(
                    category="calibration_in_target",
                    source_path=str(subdir),
                    proposed_path=proposed,
                    fits_metadata={
                        "parent": str(subdir.parent),
                        "missing_fields": missing,
                        "suggestion": "partial" if missing else "complete",
                    },
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
    return [
        TriageCandidate(category="thumbnail_cleanup", source_path=str(p))
        for p in archive_root.rglob("*")
        if p.is_file() and p.name.lower().endswith("_thn.jpg")
    ]


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
            proposed, missing = suggest_legacy_session(child, target_dir)
            candidates.append(TriageCandidate(
                category="legacy_session",
                source_path=str(child),
                proposed_path=proposed,
                fits_metadata={
                    "target": target_dir.name,
                    "focallen": focal,
                    "suggested_ota": ota,
                    "missing_fields": missing,
                    "suggestion": "partial" if missing else "complete",
                },
            ))
    return candidates


def scan_fits_headers(dso_root: Path) -> list[TriageCandidate]:
    """
    Sample one FITS file per Lights/ directory to detect missing/FOV OBJECT
    headers and RA/DEC mismatches. One candidate per session folder, not per file.

    Only canonical session folders are examined. Legacy (non-canonical) sessions
    share the same folder path and are handled by ``scan_legacy_sessions`` first;
    once renamed to canonical form, a re-scan picks up any header issues. This
    keeps the two scanners from emitting the same ``source_path`` (a two-pass
    workflow: fix structure, then re-scan for headers).
    """
    candidates = []
    seen_sessions: set[str] = set()

    for lights_dir in dso_root.rglob("Lights"):
        if not lights_dir.is_dir():
            continue
        session_dir = lights_dir.parent
        if str(session_dir) in seen_sessions:
            continue
        if not _CANONICAL_SESSION_RE.match(session_dir.name):
            continue  # legacy session — handled by scan_legacy_sessions
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
