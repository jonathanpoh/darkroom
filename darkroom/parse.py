"""Filename-based metadata extraction for ASIAir FITS files.

ASIAir does not write FILTER, IMGTYPE, or BINNING to FITS headers. All filter
and timing information must be extracted from the filename instead.

Naming convention:
    Light_<Target>_<Exposure>_Bin1_<Camera>_gain<N>_<YYYYMMDD-HHMMSS>_<Temp>_[<Filter>]_<FrameN>.fit

Examples:
    Light_M 81_180.0s_Bin1_585MC_gain200_20260220-064944_-20.0C_L-Pro_0186.fit
    Flat_180.0s_Bin1_585MC_gain200_20260221-093012_-20.0C_0003.fit  (no filter)
    Dark_180.0s_Bin1_585MC_gain200_20260221-092145_-19.5C_0001.fit
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path


TEMP_RE = re.compile(r"^-?\d+\.?\d*C$")
EXPOSURE_RE = re.compile(r"_(\d+\.?\d*(?:ms|s))_")
DATETIME_RE = re.compile(r"_(\d{8}-\d{6})_")

# Patterns that appear at parts[-2] in filenames without a filter field
_NOT_FILTER_RE = re.compile(
    r"^\d+$"                        # sequence number (0001, 0025)
    r"|^\d+\.?\d*(ms|s)$"          # exposure (20.00s, 180.0s)
    r"|\d{4}-\d{2}-\d{2}T"         # old datetime (2023-07-15T23-57-14)
    r"|^\d{8}-\d{6}$"              # new datetime (20250915-010333)
)

# ASIAir custom-text field strips non-alphanumeric chars; map back to canonical names
_FILTER_ALIASES: dict[str, str] = {
    "LExtreme": "L-Extreme",
    "LSynergy": "L-Synergy",
    "LPro": "L-Pro",
    "LEnhance": "L-Enhance",
    "LUltimate": "L-Ultimate",
}

SESSION_GAP = timedelta(hours=4)


def normalize_filter(raw: str) -> str:
    """Apply canonical filter aliases (e.g. 'LPro' → 'L-Pro')."""
    return _FILTER_ALIASES.get(raw, raw)


def parse_filter(stem: str) -> str | None:
    """Return filter string from filename stem, or None if absent.

    Filter sits at parts[-2] of the underscore-split stem. Returns None if
    that slot is a temperature (-20.0C), sequence number (0001), exposure
    (20.00s), or datetime — all of which appear there in filterless files.
    """
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    s = parts[-2]
    if TEMP_RE.match(s) or _NOT_FILTER_RE.search(s):
        return None
    return _FILTER_ALIASES.get(s, s)


def parse_exposure(stem: str) -> str | None:
    """Return exposure string (e.g. '180.0s', '130.0ms') from filename stem."""
    m = EXPOSURE_RE.search(stem)
    return m.group(1) if m else None


def parse_datetime(stem: str) -> datetime | None:
    """Return capture datetime from filename stem, or None."""
    m = DATETIME_RE.search(stem)
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S") if m else None


def flat_morning_date(end_dt: datetime) -> date:
    """Return the calendar date when morning-after flats were taken.

    If the session ran past midnight and ended before noon (hour < 12),
    flats are taken that same morning. Otherwise they're the next morning.
    """
    return end_dt.date() if end_dt.hour < 12 else end_dt.date() + timedelta(days=1)


def parse_ota(focallen) -> str:
    """Infer OTA name from FOCALLEN header value.

    Tolerance windows — ASIAir reports measured focal length, not nominal
    (e.g. FRA400 reports 402).
    """
    try:
        fl = int(focallen)
    except (TypeError, ValueError):
        return "Unknown"
    if 170 <= fl <= 190:
        return "FMA180"
    if 270 <= fl <= 290:
        return "FRA400-07x"
    if 390 <= fl <= 410:
        return "FRA400"
    return "Unknown"


def ota_from_focallen(focal_length: int | float | None) -> str:
    """Alias kept for backward compatibility."""
    return parse_ota(focal_length)


def fits_files(directory: Path, recursive: bool = False) -> list[Path]:
    """Return sorted FITS files in directory, excluding thumbnails."""
    if not directory.is_dir():
        return []
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        f for f in iterator
        if f.suffix.lower() in (".fit", ".fits") and "_thn" not in f.name
    )
