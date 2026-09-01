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
    # Misspelling present on one archive folder (Astron-i-mik). Aliased rather
    # than left to fall through: since M2 made _filter_from_path reject
    # anything not in KNOWN_FILTERS, an unaliased typo would silently demote a
    # correctly-filtered session to UnknownFilter.
    "AstronimikL2": "AstronomikL2",
}

SESSION_GAP = timedelta(hours=4)

# ASIAir stores flat darks in the same folder as science darks with no
# distinguishing IMAGETYP/header field. Exposure time is the only reliable
# separator: science darks run 120s+, flat darks are sub-second to
# low-single-digit seconds. Single source of truth for the reclassification —
# shared by darkroom.cataloger.CalibrationCataloger.scan,
# darkroom.scanner._scan_calibration, and darkroom.triage.suggest — do not
# redefine this threshold locally; import it (and reclassify_flat_dark below)
# instead.
FLAT_DARK_THRESHOLD_SEC = 10.0


def reclassify_flat_dark(frame_type: str, exposure_sec: float | None) -> str:
    """Reclassify a "Dark" as "FlatDark" when its exposure is below threshold.

    No-op for any other frame_type (including one already "FlatDark"). Each
    caller resolves frame_type from its own source (FITS IMAGETYP header,
    archive folder name, or ASIAir source folder name) before calling this —
    that resolution step is intentionally NOT shared, since the three callers
    genuinely differ in how much they trust folder names vs. header data.
    """
    if frame_type == "Dark" and exposure_sec is not None and exposure_sec < FLAT_DARK_THRESHOLD_SEC:
        return "FlatDark"
    return frame_type


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
    return normalize_filter(s)


def parse_exposure(stem: str) -> str | None:
    """Return exposure string (e.g. '180.0s', '130.0ms') from filename stem."""
    m = EXPOSURE_RE.search(stem)
    return m.group(1) if m else None


def parse_temperature(stem: str) -> float | None:
    """Return sensor temperature in °C from filename stem (e.g. '-20.0C' → -20.0), or None."""
    for part in stem.split("_"):
        if TEMP_RE.match(part):
            return float(part[:-1])
    return None


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


# The telescopes, and the date each entered service. FOCALLEN alone cannot
# tell a scope from the Canon 100-400 zoom parked at the same focal length
# (F9): the zoom reads 180 like an FMA180 and 394 like an FRA400, and no
# header disambiguates them — TELESCOP is the mount. What *is* decidable is
# that a session shot before a scope was bought was not shot with it. Confirmed
# 2026-08-31: FMA180Pro from January 2023, FRA400 (and its 0.7x reducer) from
# January 2025. Every FMA180 row in the catalog is 2023-12 or later, so the
# rule only ever fires on the 400 end, where it is worth 8 mislabelled sessions
# and their 6 flat sets.
OTA_ACQUIRED = {
    "FMA180": "2023-01-01",
    "FRA400": "2025-01-01",
    "FRA400-07x": "2025-01-01",
}

# Tolerance windows — ASIAir reports measured focal length, not nominal
# (e.g. FRA400 reports 402, the 50mm reports 51-56).
_SCOPE_WINDOWS = (
    (170, 190, "FMA180"),
    (270, 290, "FRA400-07x"),
    (390, 410, "FRA400"),
)

# Canon EF glass, named Canon<nominal focal>mm. The zoom is only ever used at
# its marked stops (100/135/200/300/400), and each stop gets its own name
# rather than one "Canon100-400": flat matching keys on OTA + camera + filter,
# so a single name for the whole range would make a 100mm flat a legal match
# for a 400mm light. The archive already separates them this way by hand
# (00_Calibration/Flats/{100,135,200,300,400}mm_Canon6D).
#
# The 50mm window reaches to 60 because the header lies at that end: the M 17
# 2023-08-09 session reports FOCALLEN 56 and plate-solves to 51mm. Nothing of
# Jonathan's lives between 60 and 95, so the slack costs nothing.
_LENS_WINDOWS = (
    (45, 60, "Canon50mm"),
    (95, 110, "Canon100mm"),
    (125, 145, "Canon135mm"),
    (192, 215, "Canon200mm"),
    (285, 315, "Canon300mm"),
    (380, 410, "Canon400mm"),
)


def _predates_acquisition(ota: str, obs_date) -> bool:
    """True if `obs_date` falls before `ota` was acquired (F9)."""
    acquired = OTA_ACQUIRED.get(ota)
    # An empty/missing date is "unknown", never "before" — a blank DATE-OBS
    # must not silently reassign an optic (`"" < "2025-01-01"` is True).
    if acquired is None or not obs_date:
        return False
    return str(obs_date) < acquired


def parse_ota(focallen, *, obs_date=None) -> str:
    """Infer OTA name from FOCALLEN, disambiguated by the session date.

    `obs_date` (an ISO date string or `datetime.date`) is optional and
    keyword-only: without it the answer is exactly what it was before F9, so
    every legacy call site is unchanged. With it, a scope window that predates
    the scope's acquisition falls through to the Canon lens of the same focal
    length — which is what a 394mm frame from 2023 actually was.
    """
    try:
        fl = int(focallen)
    except (TypeError, ValueError):
        return "Unknown"

    for lo, hi, name in _SCOPE_WINDOWS:
        if lo <= fl <= hi:
            if not _predates_acquisition(name, obs_date):
                return name
            break  # the scope did not exist yet — it was the zoom

    for lo, hi, name in _LENS_WINDOWS:
        if lo <= fl <= hi:
            return name

    return "Unknown"


def ota_from_focallen(focal_length: int | float | None, *, obs_date=None) -> str:
    """Alias kept for backward compatibility."""
    return parse_ota(focal_length, obs_date=obs_date)


# Every OTA name parse_ota can produce, excluding the "Unknown" fallback —
# the pick-list offered when focal-length inference has to be corrected by hand
# (darkroom.ingest_review). Derived from the windows above so a new optic
# cannot be added to one without appearing in the other.
#
# The Canon<focal>mm names are the convention for Canon lenses (brand in the
# name; scopes don't carry one). Canon400mm is never *inferred* for a session
# after January 2025 — at ~400mm the scope window wins — so it is here mainly
# so review can correct a night that really was shot on the zoom.
KNOWN_OTAS = tuple(name for _, _, name in (*_SCOPE_WINDOWS, *_LENS_WINDOWS))


# A mosaic panel label on its own ("1-1"). Digit runs are bounded to 1-2 each
# so a catalogue designation like "NGC 7000-7001" can't be eaten. Single source
# of truth for the shape — `darkroom.ingest_review` validates hand-typed panels
# against this rather than re-stating the pattern.
PANEL_LABEL = r"\d{1,2}-\d{1,2}"
PANEL_LABEL_RE = re.compile(PANEL_LABEL)

# The same label as a trailing "_N-M" or " N-M" suffix. A non-empty base is
# required before the separator (a bare "1-1" has nothing to split from).
# Greedy `.+` backtracks from the full string, so a name with more than one
# underscore/space still splits at the trailing occurrence, keeping everything
# before it in the base.
PANEL_RE = re.compile(rf"(.+)[_ ]({PANEL_LABEL})")


# The archive's own panel directory, one level under `Lights/<filter>/`, as
# written by `names.session_dest_rel`. The archive-side scan has to recognise
# it or a panel folder reads as an unknown extra level: `_filter_from_path`
# would look at "P1-1" instead of the filter dir above it.
PANEL_DIR_RE = re.compile(rf"P({PANEL_LABEL})")


def panel_from_dirname(name: str) -> str | None:
    """Return the panel label for an archive panel dir ("P1-1" -> "1-1"), else None."""
    m = PANEL_DIR_RE.fullmatch(name)
    return m.group(1) if m else None


def parse_panel(name: str) -> tuple[str, str | None]:
    """Split a trailing mosaic panel label (`_N-M`) off an object name.

    The ASIAir writes one folder per mosaic panel, so `name` is a folder/object
    name (e.g. "IC4604_1-1"), not a full filename stem.

    Returns (base_name, panel), or (name, None) when there is no panel label.
    """
    m = PANEL_RE.fullmatch(name)
    if not m:
        return name, None
    return m.group(1), m.group(2)


def fits_files(directory: Path, recursive: bool = False) -> list[Path]:
    """Return sorted FITS files in directory, excluding thumbnails."""
    if not directory.is_dir():
        return []
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        f for f in iterator
        if f.suffix.lower() in (".fit", ".fits") and "_thn" not in f.name
    )
