from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
from astropy.io import fits
from astropy.visualization import AsinhStretch, ZScaleInterval
from PIL import Image


_BAYER_PATTERNS = {
    "RGGB": cv2.COLOR_BayerRG2RGB,
    "BGGR": cv2.COLOR_BayerBG2RGB,
    "GRBG": cv2.COLOR_BayerGR2RGB,
    "GBRG": cv2.COLOR_BayerGB2RGB,
}


def _cache_key(fits_path: Path) -> str:
    stat = fits_path.stat()
    raw = f"{fits_path}:{stat.st_mtime}:{stat.st_size}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _zscale_asinh(data: np.ndarray) -> np.ndarray:
    """Apply ZScale interval clipping then AsinhStretch, returning 0..1 float32."""
    vmin, vmax = ZScaleInterval().get_limits(data)
    clipped = np.clip(data, vmin, vmax)
    # Normalise to 0..1
    span = vmax - vmin if vmax != vmin else 1.0
    normed = (clipped - vmin) / span
    # AsinhStretch operates on 0..1 arrays directly
    stretched = AsinhStretch()(normed)
    return stretched.astype(np.float32)


def generate_thumbnail(
    fits_path: Path,
    cache_dir: Path,
    max_width: int = 600,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(fits_path)
    jpg_path = cache_dir / f"{key}.jpg"

    if jpg_path.exists():
        if jpg_path.stat().st_mtime >= fits_path.stat().st_mtime:
            return jpg_path

    with fits.open(fits_path) as hdul:
        raw = hdul[0].data
        bayer = hdul[0].header.get("BAYERPAT", "").strip().upper()

    if bayer in _BAYER_PATTERNS:
        # Debayer first, then stretch each channel on its own ZScale limits
        # (unlinked stretch). On raw OSC subs this neutralises the background
        # and balances the colour channels far better than a single shared
        # (linked) stretch applied to the interleaved mosaic.
        mosaic = raw.astype(np.uint16)
        rgb = cv2.cvtColor(mosaic, _BAYER_PATTERNS[bayer])  # H x W x 3, uint16
        channels = [
            _zscale_asinh(rgb[..., i].astype(np.float32)) for i in range(3)
        ]
        u8 = (np.stack(channels, axis=-1) * 255).astype(np.uint8)
        img = Image.fromarray(u8, mode="RGB")
    else:
        stretched = _zscale_asinh(raw.astype(np.float32))
        u8 = (stretched * 255).astype(np.uint8)
        img = Image.fromarray(u8, mode="L").convert("RGB")

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    img.save(jpg_path, "JPEG", quality=85)
    return jpg_path
