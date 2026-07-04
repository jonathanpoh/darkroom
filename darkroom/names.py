"""Pure name/coordinate normalization helpers shared across the catalog.

Deliberately dependency-light: astropy is imported lazily inside
_parse_coords (only the sexagesimal fallback needs it), not at module
load, so importing this module never pays astropy's import cost. This is
what lets darkroom/catalog.py (the read layer) avoid astropy entirely.
"""

import re

_DSLR_RE = re.compile(r"canon|nikon|sony|pentax|fuji", re.IGNORECASE)


def _format_gain(camera: str, gain: int) -> str:
    """Return 'ISO1600' for DSLRs or '200g' for astro cameras."""
    if _DSLR_RE.search(camera):
        return "ISOAuto" if gain == 0 else f"ISO{gain}"
    return f"{gain}g"


# Canonical prefixes in alternation order (longer/compound forms before their
# single-letter subsets so e.g. 'Col'/'Cr' win over 'C'). The casing here is the
# canonical casing we store and use to build archive folder paths.
_CATALOG_PREFIXES = (
    "NGC", "LBN", "LDN", "RCW", "GUM", "Ced", "vdB", "Col", "Mel",
    "Stock", "Abell", "IC", "Tr", "Cr", "B", "M", "C",
)
_CATALOG_RE = re.compile(
    r"^(" + "|".join(_CATALOG_PREFIXES) + r")\s*(\d.*)",
    re.IGNORECASE,
)
_CANON_PREFIX = {p.upper(): p for p in _CATALOG_PREFIXES}
_SH2_RE = re.compile(r"^Sh\s*2[-\s]*(\d+)", re.IGNORECASE)


def _normalize_target(name: str) -> str:
    """Ensure canonical spacing and casing in catalog designations.

    'M81' → 'M 81', 'c49' → 'C 49', 'ngc7000' → 'NGC 7000', 'SH2-103' → 'Sh2-103'.
    The prefix is normalised to its canonical casing (not just spacing) so the
    result can be used verbatim as a case-sensitive archive folder name.
    Unrecognised names pass through unchanged.
    """
    name = name.strip()
    m = _SH2_RE.match(name)
    if m:
        return f"Sh2-{m.group(1)}"
    m = _CATALOG_RE.match(name)
    return f"{_CANON_PREFIX[m.group(1).upper()]} {m.group(2)}" if m else name


# Canonical camera names, keyed on the whitespace-stripped form of the
# FITS INSTRUME header. e.g. "Canon EOS 6D" -> "CanonEOS6D" -> "Canon6D".
_CAMERA_ALIASES = {
    "CanonEOS6D": "Canon6D",
}


def _normalize_camera(name):
    """Canonicalize a camera name: strip whitespace, then apply known aliases.

    Idempotent and safe on None. e.g. "Canon EOS 6D" and "CanonEOS6D" both
    normalize to "Canon6D"; "ZWO ASI585MC Pro" -> "ZWOASI585MCPro".
    """
    if name is None:
        return None
    slug = re.sub(r"\s+", "", name)
    return _CAMERA_ALIASES.get(slug, slug)


def make_session_id(target: str, obs_date: str, ota: str, camera: str, filter_: str | None) -> str:
    """Build collision-resistant session primary key.

    Removes spaces from target and camera, strips dashes from date, and uses
    "UnknownFilter" when filter detection failed (signals needs-review, distinct
    from a session deliberately shot bare).

    Args:
        target: Target name (e.g. "M 81", "NGC 7380")
        obs_date: Observation date in YYYY-MM-DD format
        ota: OTA abbreviation (e.g. "FRA400", "FMA180")
        camera: Camera model (e.g. "ASI585MC", "Canon6D")
        filter_: Filter name (e.g. "L-Pro", "L-Extreme"), or None/empty string

    Returns:
        Session ID: {TargetSlug}_{YYYYMMDD}_{OTA}_{Camera}_{Filter}
        (e.g. "M81_20260219_FRA400_ASI585MC_L-Pro")
    """
    slug = re.sub(r"\s+", "", target)
    camera_slug = _normalize_camera(camera)
    date = obs_date.replace("-", "")
    # "UnknownFilter" means parse failed AND no FITS FILTER header — needs manual review.
    # A session legitimately shot bare would need to be flagged explicitly (future work).
    f = filter_ or "UnknownFilter"
    return f"{slug}_{date}_{ota}_{camera_slug}_{f}"


def _round_exposure(x):
    """Round an exposure value to 4 decimals. Safe on None."""
    return None if x is None else round(float(x), 4)


def _parse_coords(ra, dec) -> tuple[float | None, float | None]:
    """Return (ra_deg, dec_deg) from FITS header values, or (None, None).

    ASIAir typically writes RA/DEC as float degrees. Older or different rigs
    may write sexagesimal strings ("09 55 33", "+69 03 55"). Handles both.
    """
    if ra is None or dec is None:
        return None, None
    try:
        return float(ra), float(dec)
    except (TypeError, ValueError):
        try:
            import astropy.units as u
            from astropy.coordinates import SkyCoord

            c = SkyCoord(ra=str(ra), dec=str(dec), unit=(u.hourangle, u.deg))
            return c.ra.deg, c.dec.deg
        except Exception:
            return None, None
