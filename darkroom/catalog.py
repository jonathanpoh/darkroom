# darkroom/catalog.py
"""darkroom.catalog — client-side calibration matching over a CatalogBackend.

This module is the astropy-free *matching* layer: it holds only the logic
that can't be expressed as a server-side equality filter (date proximity,
exposure tolerance, null-filter matching) and consumes rows fetched through
a darkroom.catalog_client.CatalogBackend (LocalBackend or HttpBackend), so
the same matching logic works whether the catalog lives in a local SQLite
file or behind the future webapi server (W9). Deliberately import-light:
only stdlib + darkroom.catalog_client (itself astropy/httpx-free at import
time) at module load.
"""
from __future__ import annotations

from datetime import date, timedelta

from darkroom.catalog_client import CatalogBackend


def query_all_sessions(backend: CatalogBackend) -> list[dict]:
    """Return all sessions ordered by target then obs_date."""
    return sorted(backend.query_sessions(), key=lambda r: (r["target"], r["obs_date"]))


DEFAULT_DARK_TEMP_TOLERANCE = 3.0


def dark_temp_sort_key(set_temp: float | None, session_temp: float) -> tuple[int, float]:
    """Rank a dark set against a session's sensor temperature: nearest first.

    Sets with no recorded temperature sort last — they match anything, but only
    as a fallback once every temperatured set has been considered.
    """
    if set_temp is None:
        return (1, 0.0)
    return (0, abs(set_temp - session_temp))


def dark_temp_ties(rows: list[dict], temperature_c: float) -> list[dict]:
    """Rows tied for nearest temperature at *different* set-points.

    Returns [] unless the pick is genuinely ambiguous — i.e. two or more distinct
    temperatures sit exactly the same distance from the session. A session at
    22.5C with masters on the 20C/25C rungs of an uncooled ladder is the real
    case: neither is more correct than the other, so the caller warns rather
    than silently taking row order. Sets at the same temperature are not a tie;
    either is equally right.
    """
    temped = [r for r in rows if r["temperature_c"] is not None]
    if not temped:
        return []
    best = min(abs(r["temperature_c"] - temperature_c) for r in temped)
    tied = [r for r in temped if abs(r["temperature_c"] - temperature_c) == best]
    if len({r["temperature_c"] for r in tied}) < 2:
        return []
    return tied


def find_darks(
    backend: CatalogBackend, *, camera: str, gain: int, exposure_sec: float,
    temperature_c: float | None = None,
    temp_tolerance: float = DEFAULT_DARK_TEMP_TOLERANCE,
) -> list[dict]:
    """Return Dark sets matching camera+gain+exposure, masters first.

    When `temperature_c` is given, sets further than `temp_tolerance` degrees
    from it are dropped and the rest are ranked nearest-first within the
    masters/raws split (see dark_temp_sort_key). Passing None skips temperature
    matching entirely — the pre-B11 behaviour, kept for callers that only want
    to know what darks exist at a given gain/exposure.

    Nearest-within-tolerance is applied to *every* camera, not just uncooled
    ones. Exact matching looks right for a cooled camera with deliberate
    set-points, but session `temperature_c` is the raw CCD-TEMP of the session's
    *first* frame (cataloger._read_header), taken while the sensor may still be
    settling, whereas calibration sets round to the nearest degree
    (scanner.py). On the live catalog that leaves 13 of 111 ZWOASI585MCPro
    sessions on values like -19.5, -16.5 or -15.0 that no master would match
    exactly. One rule with a tolerance handles both regimes and needs no
    per-camera cooled/uncooled registry, which the codebase does not have.
    """
    rows = backend.query_calibration_sets(
        frame_type="Dark", camera=camera, gain=gain, exposure_sec=exposure_sec
    )
    if temperature_c is None:
        return rows
    rows = [
        r for r in rows
        if r["temperature_c"] is None
        or abs(r["temperature_c"] - temperature_c) <= temp_tolerance
    ]
    # Masters stay ahead of raws (the query's contract, which _build_night's
    # partition-then-fallback relies on); temperature only orders within each.
    rows.sort(
        key=lambda r: (
            not r.get("is_master"),
            dark_temp_sort_key(r["temperature_c"], temperature_c),
        )
    )
    return rows


def find_bias(backend: CatalogBackend, *, camera: str, gain: int) -> list[dict]:
    """Return Bias calibration sets matching camera+gain, masters first."""
    return backend.query_calibration_sets(frame_type="Bias", camera=camera, gain=gain)


def flat_offset_days(capture_date: str, obs_date: str) -> int:
    """Signed days from a session's night to a flat set's capture date.

    0 = shot the same evening (before midnight), +1 = the following morning.
    Both are "this run"; see flat_sort_key.
    """
    return (date.fromisoformat(capture_date) - date.fromisoformat(obs_date)).days


def flat_sort_key(capture_date: str, obs_date: str) -> tuple[int, int, int]:
    """Rank a flat set for a session night — the flat-morning rule, directional.

    Flats belonging to the session's own run come first: offset 0 (shot that
    evening, e.g. either side of a mid-session filter change) or +1 (the usual
    case, shot the morning after). Within that group the morning-after set wins,
    since that is the habitual workflow.

    Everything else in the window is a fallback — flats from a different
    occasion entirely — ranked by proximity, preferring later over earlier on a
    tie for the same reason.

    Plain proximity used to rank these, which made ±1 a tie broken by whatever
    order the backend happened to return: a session on night N would routinely
    be handed the *previous* night's flats over the ones shot the morning after.
    The two sets can differ a lot (sky brightness changes the flat exposure), so
    this is not cosmetic. `infer_flat_filter` and `find_flat_darks` already
    encoded the same directional 0..+1 rule; this brings find_flats in line.
    """
    delta = flat_offset_days(capture_date, obs_date)
    in_run = 0 if 0 <= delta <= 1 else 1
    # Third element prefers the later date; second is inert for in-run rows so
    # the tuple stays comparable across both groups.
    return (in_run, abs(delta) if in_run else 0, -delta)


def find_flats(
    backend: CatalogBackend, *, camera: str, ota: str, filter_: str | None,
    obs_date: str, window_days: int = 3,
) -> list[dict]:
    """Return Flat calibration sets within ±window_days, best match first.

    Archived flats may have been taken on a different occasion than the session,
    so the window is date proximity (default ±3 days) rather than an exact date
    — but ordering within it follows the flat-morning rule (see flat_sort_key),
    not raw proximity.
    """
    d = date.fromisoformat(obs_date)
    lo = d - timedelta(days=window_days)
    hi = d + timedelta(days=window_days)
    rows = backend.query_calibration_sets(frame_type="Flat", camera=camera, ota=ota)
    rows = [r for r in rows if r["filter"] == filter_]
    # NULL capture_date never matches, same as the old SQL BETWEEN.
    rows = [r for r in rows if r["capture_date"] is not None]
    rows = [r for r in rows if lo <= date.fromisoformat(r["capture_date"]) <= hi]
    rows.sort(key=lambda r: flat_sort_key(r["capture_date"], obs_date))
    return rows


def find_flat_darks(
    backend: CatalogBackend, *, camera: str, flat_exposure_sec: float,
    flat_capture_date: str,
) -> list[dict]:
    """Return FlatDark sets matching camera + exposure (±10%) + date (flat_date or flat_date+1)."""
    lo = flat_exposure_sec * 0.9
    hi = flat_exposure_sec * 1.1
    d = date.fromisoformat(flat_capture_date)
    d1 = (d + timedelta(days=1)).isoformat()
    rows = backend.query_calibration_sets(frame_type="FlatDark", camera=camera)
    return [
        r for r in rows
        if lo <= r["exposure_sec"] <= hi and r["capture_date"] in (flat_capture_date, d1)
    ]
