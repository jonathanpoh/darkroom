"""darkroom.sites — shared site-resolution and SQM-weighting logic.

Pure, stdlib-only (math) functions for matching a session's SITELAT/SITELONG
coordinates against the catalog's named `sites` table, and for weighting
integration time by relative sky brightness (SQM). Used by both `darkroom`
CLI subcommands and the webapi UI, so it must not import astropy or anything
else with a heavy/optional dependency.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 decimal-degree points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def resolve_site(lat: float | None, lon: float | None, sites: list[dict]) -> dict | None:
    """Return the nearest site whose radius_m covers (lat, lon), or None.

    None if lat/lon is None, sites is empty, or no site's radius_m reaches
    the point. When multiple sites are in range, the nearest one wins.
    """
    if lat is None or lon is None or not sites:
        return None
    best = None
    best_dist = None
    for site in sites:
        dist = haversine_m(lat, lon, site["lat"], site["lon"])
        if dist <= site["radius_m"] and (best_dist is None or dist < best_dist):
            best = site
            best_dist = dist
    return best


def home_sqm(sites: list[dict]) -> float | None:
    """Return the sqm of the is_home site, or None if there's no home or it lacks an sqm."""
    for site in sites:
        if site.get("is_home"):
            return site.get("sqm")
    return None


def session_weight(site: dict | None, home: float | None) -> float:
    """Flux-ratio weight for a session's integration time at `site` vs. home SQM.

    SQM is a log-magnitude/arcsec^2 scale, so each +5 mag/arcsec^2 (darker
    sky) corresponds to a 100x drop in sky-glow flux — i.e. a factor of
    10**(delta/2.5). Returns 1.0 (neutral) whenever site, its sqm, or home is
    missing, since there's nothing to weight against.
    """
    if site is None or home is None:
        return 1.0
    site_sqm = site.get("sqm")
    if site_sqm is None:
        return 1.0
    return 10 ** ((site_sqm - home) / 2.5)
