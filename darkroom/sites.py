"""darkroom.sites — shared site-resolution and SQM-weighting logic.

Pure, stdlib-only (math) functions for matching a session's SITELAT/SITELONG
coordinates against the catalog's named `sites` table, and for weighting
integration time by relative sky brightness (SQM). Used by both `darkroom`
CLI subcommands and the webapi UI, so it must not import astropy or anything
else with a heavy/optional dependency.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Iterable

EARTH_RADIUS_M = 6371000.0

# Frames further than this from a session's modal position are treated as a bad
# fix worth reporting rather than ordinary GPS jitter.
SITE_DISAGREEMENT_M = 1000.0


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


def modal_site(
    positions: Iterable[tuple[float | None, float | None]],
) -> tuple[float | None, float | None, dict[tuple[float, float], int]]:
    """Most common (lat, lon) among *positions*, plus any that materially disagree.

    The ASIAir takes its position from the phone running the app, and only
    re-reads it at autorun start. A stale fix — or a WiFi-geolocated one, when
    the phone has no cellular signal and resolves to wherever the access point
    is registered rather than where you are — can therefore contaminate part or
    all of a session. Picking any single frame lets one bad frame decide the
    whole night, so take the most common position instead.

    Returns (lat, lon, outliers), where outliers maps each distinct position
    further than SITE_DISAGREEMENT_M from the modal one to its frame count.
    A session never genuinely moves, so a non-empty outliers means the
    coordinates need a human look. Returns (None, None, {}) when no position
    is usable.
    """
    counts: Counter = Counter(
        (lat, lon) for lat, lon in positions if lat is not None and lon is not None
    )
    if not counts:
        return None, None, {}

    (lat, lon), _ = counts.most_common(1)[0]
    outliers = {
        pos: n
        for pos, n in counts.items()
        if haversine_m(lat, lon, *pos) > SITE_DISAGREEMENT_M
    }
    return lat, lon, outliers


def describe_disagreement(
    label: str,
    lat: float,
    lon: float,
    outliers: dict[tuple[float, float], int],
    total: int,
) -> list[str]:
    """Human-readable warning lines for a session whose frames disagree on position.

    Returned rather than printed so callers control the stream and prefix.
    """
    kept = total - sum(outliers.values())
    lines = [
        f"Warning: {label}: site coordinates disagree across frames — "
        f"using {lat}, {lon} ({kept}/{total} frames)"
    ]
    for (o_lat, o_lon), n in sorted(outliers.items(), key=lambda kv: -kv[1]):
        km = haversine_m(lat, lon, o_lat, o_lon) / 1000
        lines.append(f"         {n} frame(s) at {o_lat}, {o_lon} ({km:.1f} km away)")
    return lines


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
