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
