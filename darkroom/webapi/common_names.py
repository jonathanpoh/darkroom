"""darkroom.webapi.common_names — hardcoded target -> common-name lookup.

v1: a small hardcoded dict lifted from the safelight design mock, covering the
targets in Jonathan's catalog as of the mock's design session. Deliberately
dependency-light (no DB, no network) so the UI router can call it at request
time without cost.

Future: replace with a `common_name` column on `sessions` (or a separate
targets table) backfilled from a SIMBAD lookup, so new targets get a common
name automatically instead of requiring a code change here.
"""

from __future__ import annotations

COMMON_NAMES: dict[str, str] = {
    "C 49": "Rosette Nebula",
    "IC 1318": "Butterfly Nebula",
    "IC 1396": "Elephant's Trunk region",
    "IC 1396A": "Elephant's Trunk Nebula",
    "IC 1805": "Heart Nebula",
    "IC 1848": "Soul Nebula",
    "IC 405": "Flaming Star Nebula",
    "IC 434": "Horsehead Nebula",
    "IC 4604": "Rho Ophiuchi Cloud",
    "IC 5070": "Pelican Nebula",
    "LDN 1688": "Rho Ophiuchi dark cloud",
    "M 101": "Pinwheel Galaxy",
    "M 13": "Great Hercules Cluster",
    "M 17": "Omega Nebula",
    "M 31": "Andromeda Galaxy",
    "M 33": "Triangulum Galaxy",
    "M 38": "Starfish Cluster",
    "M 42": "Orion Nebula",
    "M 44": "Beehive Cluster",
    "M 45": "Pleiades",
    "M 51": "Whirlpool Galaxy",
    "M 63": "Sunflower Galaxy",
    "M 78": "reflection nebula in Orion",
    "M 8": "Lagoon Nebula",
    "M 81": "Bode's Galaxy",
    "M 81 M 82": "Bode's & Cigar",
    "M 82 M 82": "Cigar Galaxy",
    "M 94": "Croc's Eye Galaxy",
    "NGC 1499": "California Nebula",
    "NGC 281": "Pacman Nebula",
    "NGC 281W": "Pacman Nebula (W)",
    "NGC 6888": "Crescent Nebula",
    "NGC 6960": "Western Veil",
    "NGC 6992": "Eastern Veil",
    "NGC 7000": "North America Nebula",
    "NGC 7380": "Wizard Nebula",
    "NGC 896": "Fish Head Nebula",
    "Sh2-103": "Cygnus Loop",
    "Sh2-220": "California Nebula",
    "Sh2-273": "Cone Nebula region",
}


def common_name(target: str) -> str | None:
    """Return the common name for a catalog designation, or None if unknown."""
    return COMMON_NAMES.get(target)
