"""Pure name/coordinate normalization helpers shared across the catalog.

Deliberately dependency-light: astropy is imported lazily inside
_parse_coords (only the sexagesimal fallback needs it), not at module
load, so importing this module never pays astropy's import cost. This is
what lets darkroom/catalog.py (the read layer) avoid astropy entirely.
"""

import re
from pathlib import Path
from typing import overload

from darkroom.parse import PANEL_LABEL_RE

_DSLR_RE = re.compile(r"canon|nikon|sony|pentax|fuji", re.IGNORECASE)

# Physical filters that have actually been used, in dropdown order. 'NoFilter'
# means confirmed shot bare — distinct from NULL/'UnknownFilter', which mean
# the filter is simply unknown (see make_session_id below). Used by the U2
# filter-assignment cleanup queue (darkroom.webapi.ui) to tell a real filter
# value apart from a null/garbage one, and by the U3 ingest review prompts
# (darkroom.ingest_review) as the filter pick-list. Single source of truth —
# darkroom.ingest re-exports this rather than keeping its own copy.
KNOWN_FILTERS = (
    "L-Pro", "L-Extreme", "L-Synergy", "L-Enhance", "L-Ultimate",
    "BaaderNeodymium", "AstronomikL2", "OmegonHelievo", "NoFilter",
)


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


@overload
def _normalize_camera(name: str) -> str: ...
@overload
def _normalize_camera(name: None) -> None: ...


def _normalize_camera(name: str | None) -> str | None:
    """Canonicalize a camera name: strip whitespace, then apply known aliases.

    Idempotent and safe on None. e.g. "Canon EOS 6D" and "CanonEOS6D" both
    normalize to "Canon6D"; "ZWO ASI585MC Pro" -> "ZWOASI585MCPro".
    """
    if name is None:
        return None
    slug = re.sub(r"\s+", "", name)
    return _CAMERA_ALIASES.get(slug, slug)


def make_session_id(
    target: str, obs_date: str, ota: str, camera: str, filter_: str | None, *, panel: str | None = None
) -> str:
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
        panel: Mosaic panel label (e.g. "1-1"), or None for an ordinary
            single-pointing session (the overwhelming majority). Without this,
            a same-night multi-panel mosaic has every panel's session collide
            on the same session_id, since obs_date/OTA/camera/filter are
            otherwise identical — the collision `catalog_db.rename_target`
            currently reports as a per-row error.

    Returns:
        Session ID: {TargetSlug}_{YYYYMMDD}_{OTA}_{Camera}_{Filter}[_P{Panel}]
        (e.g. "M81_20260219_FRA400_ASI585MC_L-Pro", or with a panel,
        "IC4604_20250426_FRA400_Canon6D_NoFilter_P1-1")
    """
    slug = re.sub(r"\s+", "", target)
    camera_slug = _normalize_camera(camera)
    date = obs_date.replace("-", "")
    # "UnknownFilter" means parse failed AND no FITS FILTER header — needs manual review.
    # A session legitimately shot bare would need to be flagged explicitly (future work).
    f = filter_ or "UnknownFilter"
    session_id = f"{slug}_{date}_{ota}_{camera_slug}_{f}"
    if panel:
        session_id += f"_P{panel}"
    return session_id


def session_dest_rel(
    target: str, obs_date: str, ota: str, camera: str, filter_: str | None, *, panel: str | None = None
) -> Path:
    """Return relative destination path for a session's Lights/<filter>/ folder.

    Single source of truth for `lights_path` derivation: `darkroom.ingest` computes
    new sessions' destinations from this, and `catalog_db.update_session_fields`
    recomputes `lights_path` from this on identity edits.

    When `panel` is given (a mosaic panel label, e.g. "1-1"), an extra
    `P<panel>` directory is nested under the filter level so each panel of a
    same-night mosaic gets its own Lights subfolder instead of dumping every
    panel's frames into one.
    """
    f = filter_ or "NoFilter"
    folder = f"{obs_date}_{ota}_{_normalize_camera(camera)}"
    rel = Path("01_Deep Sky Objects") / target / folder / "Lights" / f
    if panel:
        rel = rel / f"P{panel}"
    return rel


# ── mosaic panel directory names ─────────────────────────────────────────────
#
# Three different places spell a panel differently, on purpose, and the
# distinctions have already caused one documentation error — so they live here
# rather than being formatted inline at each call site:
#
#   session_id            ..._L-Pro_P1-1   the "P" disambiguates a flat string
#   archive Lights/       Lights/L-Pro/P1-1/            (session_dest_rel)
#   WBPP target dir       IC4604/PANEL_1-1/SESSION_N/   (this module, below)
#   archive _Processed/   _Processed/<date>/1-1/        bare — see below
#
# The WBPP form is verbose because it sits beside SESSION_N and reads as a
# grouping level. The _Processed form is bare because Jonathan's archive
# already uses `1-1/` there and a "P" prefix adds nothing inside a directory
# whose siblings are all panels.

WBPP_PANEL_PREFIX = "PANEL_"


def wbpp_panel_dir(panel: str) -> str:
    """Directory name for one mosaic panel inside a WBPP target dir.

    `"1-1"` -> `"PANEL_1-1"`. Panels get a directory of their own because a
    panel is not a stacking boundary to WBPP: a custom grouping keyword groups
    the calibration stages and then integration merges every panel anyway
    (tested 2026-08-31). Only a separate WBPP run keeps them apart, and a
    separate run means a separate directory with its own `Output/`.
    """
    return f"{WBPP_PANEL_PREFIX}{panel}"


def parse_wbpp_panel_dir(name: str) -> str | None:
    """Inverse of `wbpp_panel_dir`: `"PANEL_1-1"` -> `"1-1"`, else None.

    How `darkroom.finish` discovers whether a WBPP target holds a mosaic,
    and which panel each subdirectory belongs to.
    """
    if not name.startswith(WBPP_PANEL_PREFIX):
        return None
    label = name[len(WBPP_PANEL_PREFIX):]
    return label if PANEL_LABEL_RE.fullmatch(label) else None


def processed_panel_dir(panel: str) -> str:
    """Subdirectory for one panel's stack under `_Processed/<date>/`.

    Bare (`"1-1"`), matching what is already on disk — deliberately *not*
    `P1-1`. Trivial today, but it is the one place to change if that
    convention ever moves, and the P-prefix confusion has bitten once already.
    """
    return panel


def target_slug(target: str) -> str:
    """Strip spaces from a target name for use as an archive/WBPP folder name.

    Single source of truth shared by `darkroom.wbpp` (which creates
    `<wbpp_root>/<slug>/`) and `darkroom.finish` (which looks it up) — the
    wbpp -> finish handoff depends on these staying identical.
    """
    return target.replace(" ", "")


def _round_exposure(x):
    """Round an exposure value to 4 decimals. Safe on None."""
    return None if x is None else round(float(x), 4)


def normalize_session_fields(session: dict) -> dict:
    """Return a copy of *session* with the fields the catalog canonicalizes on write.

    ``upsert_session`` canonicalizes `camera` and `exposure_sec` on the way in,
    so the value a scan produces is not necessarily the value that gets stored:
    a raw FITS INSTRUME of "Canon EOS 6D" is stored as "Canon6D", and an
    EXPTIME of 0.000125000005937181 is stored as 0.0001.

    That mattered enough to extract. Anything comparing a fresh scan against
    the catalog (`darkroom.rescan`) has to compare like with like — against
    what *would* be stored, not the raw header values — or every session with
    a camera reads as diverging. F8's first dry run on the live catalog
    proposed rewriting 209 of 231 sessions from the canonical `Canon6D`/
    `ZWOASI585MCPro` back to the raw header spellings for exactly this reason.

    Pure: returns a copy, never mutates the argument.
    """
    out = dict(session)
    out["camera"] = _normalize_camera(out.get("camera"))
    out["exposure_sec"] = _round_exposure(out.get("exposure_sec"))
    return out


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
