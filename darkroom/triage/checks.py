from __future__ import annotations

from pathlib import Path

from astropy.coordinates import SkyCoord
from astropy.io import fits
from astroquery.simbad import Simbad


def check_object_value(value: str | None) -> str | None:
    """Return 'MISSING' or 'FOV' if the OBJECT header is problematic, else None."""
    if value is None or str(value).strip() == "":
        return "MISSING"
    if str(value).strip().upper() == "FOV":
        return "FOV"
    return None


def check_fits_object(fits_path: Path) -> tuple[str | None, str | None]:
    """Return (reason, object_val). reason is None if OBJECT is valid."""
    try:
        with fits.open(fits_path) as hdul:
            val = hdul[0].header.get("OBJECT")
    except Exception:
        return ("MISSING", None)
    return (check_object_value(val), str(val).strip() if val else None)


def check_ra_dec(
    fits_path: Path,
    target_name: str,
    threshold_deg: float = 5.0,
    simbad_cache: dict | None = None,
) -> dict | None:
    """
    Return a dict with mismatch details if RA/DEC is > threshold_deg from the
    SIMBAD position for target_name. Returns None if coords agree or can't be checked.
    """
    try:
        with fits.open(fits_path) as hdul:
            hdr = hdul[0].header
            ra = hdr.get("RA") or hdr.get("OBJCTRA")
            dec = hdr.get("DEC") or hdr.get("OBJCTDEC")
    except Exception:
        return None

    if ra is None or dec is None:
        return None

    if simbad_cache and "ra" in simbad_cache:
        simbad_ra = simbad_cache["ra"]
        simbad_dec = simbad_cache["dec"]
    else:
        table = Simbad.query_object(target_name)
        if table is None or len(table) == 0:
            return None
        # astroquery >= 0.4.7 returns lowercase column names
        ra_col = "ra" if "ra" in table.colnames else "RA"
        dec_col = "dec" if "dec" in table.colnames else "DEC"
        simbad_ra = float(table[ra_col][0])
        simbad_dec = float(table[dec_col][0])

    frame_coord = SkyCoord(ra=float(ra), dec=float(dec), unit="deg")
    simbad_coord = SkyCoord(ra=simbad_ra, dec=simbad_dec, unit="deg")
    sep = frame_coord.separation(simbad_coord).deg

    if sep <= threshold_deg:
        return None

    return {
        "frame_ra": float(ra),
        "frame_dec": float(dec),
        "simbad_ra": simbad_ra,
        "simbad_dec": simbad_dec,
        "separation_deg": sep,
        "target_name": target_name,
    }
