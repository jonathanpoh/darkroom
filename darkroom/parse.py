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

from fits_cataloger import parse_ota as _fits_parse_ota

TEMP_RE = re.compile(r"^-?\d+\.?\d*C$")
EXPOSURE_RE = re.compile(r"_(\d+\.?\d*(?:ms|s))_")
DATETIME_RE = re.compile(r"_(\d{8}-\d{6})_")

SESSION_GAP = timedelta(hours=4)


def parse_filter(stem: str) -> str | None:
    """Return filter string from filename stem, or None if absent.

    Filter sits at parts[-2] of the underscore-split stem. If that slot
    matches a temperature pattern (-20.0C) there is no filter in the filename.
    Normalises 'LExtreme' → 'L-Extreme'.
    """
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    s = parts[-2]
    if TEMP_RE.match(s):
        return None
    return "L-Extreme" if s == "LExtreme" else s


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


def ota_from_focallen(focal_length: int | float | None) -> str:
    """Infer OTA name from focal length header value (delegates to fits_cataloger)."""
    return _fits_parse_ota(focal_length)


def fits_files(directory: Path) -> list[Path]:
    """Return sorted FITS files in directory, excluding thumbnails."""
    if not directory.is_dir():
        return []
    return sorted(
        f for f in directory.iterdir()
        if f.suffix.lower() in (".fit", ".fits") and "_thn" not in f.name
    )
