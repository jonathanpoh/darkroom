from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image

from darkroom.triage.preview import generate_thumbnail


def make_mono_fits(path: Path, shape=(100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.random.randint(100, 1000, shape, dtype=np.uint16)
    hdu = fits.PrimaryHDU(data=data)
    hdu.writeto(path, overwrite=True)
    return path


def make_bayer_fits(path: Path, shape=(100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.random.randint(100, 4000, shape, dtype=np.uint16)
    hdu = fits.PrimaryHDU(data=data)
    hdu.header["BAYERPAT"] = "RGGB"
    hdu.writeto(path, overwrite=True)
    return path


class TestGenerateThumbnail:
    def test_produces_jpeg(self, tmp_path):
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        result = generate_thumbnail(src, cache)
        assert result.suffix == ".jpg"
        assert result.exists()

    def test_valid_image(self, tmp_path):
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        jpg = generate_thumbnail(src, cache)
        img = Image.open(jpg)
        assert img.size[0] <= 600

    def test_bayer_produces_rgb(self, tmp_path):
        src = make_bayer_fits(tmp_path / "bayer.fit")
        cache = tmp_path / ".cache"
        jpg = generate_thumbnail(src, cache)
        img = Image.open(jpg)
        assert img.mode == "RGB"

    def test_unlinked_stretch_balances_channels(self, tmp_path):
        # Build an RGGB mosaic where the blue pixels are ~10x brighter than red,
        # each with its own spatial gradient. An *unlinked* stretch normalises
        # each channel to its own range, so both R and B reach near-full
        # intensity. A linked stretch would crush the dim red channel.
        h = w = 100
        mosaic = np.zeros((h, w), dtype=np.uint16)
        row = np.linspace(0, 1, w, dtype=np.float32)
        grad = np.tile(row, (h, 1))
        red = (100 + grad * 150).astype(np.uint16)        # ~100..250
        blue = (1000 + grad * 1500).astype(np.uint16)     # ~1000..2500
        green = (500 + grad * 750).astype(np.uint16)
        mosaic[0::2, 0::2] = red[0::2, 0::2]      # R
        mosaic[0::2, 1::2] = green[0::2, 1::2]    # G
        mosaic[1::2, 0::2] = green[1::2, 0::2]    # G
        mosaic[1::2, 1::2] = blue[1::2, 1::2]     # B

        path = tmp_path / "osc.fit"
        hdu = fits.PrimaryHDU(data=mosaic)
        hdu.header["BAYERPAT"] = "RGGB"
        hdu.writeto(path, overwrite=True)

        jpg = generate_thumbnail(path, tmp_path / ".cache")
        arr = np.asarray(Image.open(jpg))  # H x W x 3, uint8
        r_max = int(arr[..., 0].max())
        b_max = int(arr[..., 2].max())
        # Both channels independently stretched to near full range despite the
        # ~10x raw brightness difference.
        assert r_max > 200
        assert b_max > 200

    def test_cached_on_second_call(self, tmp_path):
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        jpg1 = generate_thumbnail(src, cache)
        mtime1 = jpg1.stat().st_mtime
        jpg2 = generate_thumbnail(src, cache)
        assert jpg1 == jpg2
        assert jpg2.stat().st_mtime == mtime1  # not regenerated

    def test_regenerates_if_source_newer(self, tmp_path):
        import time
        src = make_mono_fits(tmp_path / "mono.fit")
        cache = tmp_path / ".cache"
        jpg = generate_thumbnail(src, cache)
        old_mtime = jpg.stat().st_mtime
        time.sleep(0.1)
        make_mono_fits(src)  # overwrite → newer mtime
        jpg2 = generate_thumbnail(src, cache)
        assert jpg2.stat().st_mtime > old_mtime
