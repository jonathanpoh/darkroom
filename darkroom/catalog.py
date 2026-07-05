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


def find_flats(
    backend: CatalogBackend, *, camera: str, ota: str, filter_: str | None,
    obs_date: str, window_days: int = 3,
) -> list[dict]:
    """Return Flat calibration sets within ±window_days, ordered by date proximity.

    Archived flats may have been taken on a different occasion than the session,
    so matching is by date proximity (default ±3 days) rather than exact date.
    """
    d = date.fromisoformat(obs_date)
    lo = d - timedelta(days=window_days)
    hi = d + timedelta(days=window_days)
    rows = backend.query_calibration_sets(frame_type="Flat", camera=camera, ota=ota)
    rows = [r for r in rows if r["filter"] == filter_]
    # NULL capture_date never matches, same as the old SQL BETWEEN.
    rows = [r for r in rows if r["capture_date"] is not None]
    rows = [r for r in rows if lo <= date.fromisoformat(r["capture_date"]) <= hi]
    rows.sort(key=lambda r: abs((date.fromisoformat(r["capture_date"]) - d).days))
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
