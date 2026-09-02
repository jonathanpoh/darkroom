"""darkroom.sites — shared site-resolution and SQM-weighting logic.

Stdlib-only functions for matching a session's SITELAT/SITELONG
coordinates against the catalog's named `sites` table, and for weighting
integration time by relative sky brightness (SQM). Used by both `darkroom`
CLI subcommands and the webapi UI, so it must not import astropy or anything
else with a heavy/optional dependency.
"""

from __future__ import annotations

import math
import sys
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


def session_site(
    positions: Iterable[tuple[float | None, float | None]], label: str
) -> tuple[float | None, float | None]:
    """A session's (lat, lon): modal_site, warning on stderr when frames disagree.

    The one call every scan makes (B16). Ingest, the archive-side scan and
    `backfill-sites` each used to spell out the modal_site + describe_
    disagreement pair, and the archive-side scan did not — it read the
    chronologically first frame, the one most likely to carry the stale fix,
    so a rescan could propose "correcting" a good position back to a bad one.
    *label* names the session in the warning.
    """
    positions = list(positions)
    lat, lon, outliers = modal_site(positions)
    if outliers:
        usable = sum(1 for la, lo in positions if la is not None and lo is not None)
        for line in describe_disagreement(label, lat, lon, outliers, usable):
            print(line, file=sys.stderr)
    return lat, lon


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


def annotate_sessions(
    rows: list[dict],
    sites: list[dict] | None = None,
    *,
    home: float | None = None,
) -> list[dict]:
    """Return copies of each session row with SQM-derived fields added.

    Mirrors the per-row math `darkroom.webapi.ui._build_aggregate` already did
    inline, lifted out so the JSON API (`GET /api/sessions`) and the
    server-rendered HTML aggregate share one implementation — SQM weighting
    was only ever reachable from the dashboard before this (S2).

    Pure: the input rows are never mutated, so a caller can annotate rows it
    does not own (and annotate the same list twice) without surprising whoever
    else holds a reference. Each output row is a shallow copy carrying every
    original key plus three new ones:
      * `site`           — resolved site's `name`, or None when coords are
                           absent or no site's radius_m covers the position
      * `weight`         — `round(session_weight(site, home), 3)` (1.0 when
                           site or home SQM is unknown)
      * `weighted_hours` — `h * weight`, where
                           `h = (total_integration_sec or 0) / 3600.0`

    `weighted_hours` deliberately multiplies by the *rounded* weight, so a
    consumer that recomputes `h * weight` from the published fields gets the
    published `weighted_hours` back exactly. Rounding one and not the other
    left the two disagreeing in the last few decimals.

    `home` defaults to `home_sqm(sites)` when None (computed once per call).
    Pass it in to avoid re-deriving across batches. With `sites` empty/None
    or no home SQM, every row gets `weight=1.0`, `weighted_hours=h`,
    `site=None` — i.e. the math is a no-op and callers that never configured
    sites see the same `h` they always did, under `weighted_hours`.
    """
    if sites is None:
        sites = []
    if home is None:
        home = home_sqm(sites)
    annotated = []
    for row in rows:
        h = (row.get("total_integration_sec") or 0) / 3600.0
        site = resolve_site(row.get("site_lat"), row.get("site_lon"), sites)
        weight = round(session_weight(site, home), 3)
        annotated.append({
            **row,
            "site": site["name"] if site else None,
            "weight": weight,
            "weighted_hours": h * weight,
        })
    return annotated
