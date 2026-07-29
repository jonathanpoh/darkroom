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


def find_darks(
    backend: CatalogBackend, *, camera: str, gain: int, exposure_sec: float
) -> list[dict]:
    """Return Dark calibration sets matching camera+gain+exposure, masters first."""
    return backend.query_calibration_sets(
        frame_type="Dark", camera=camera, gain=gain, exposure_sec=exposure_sec
    )


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
