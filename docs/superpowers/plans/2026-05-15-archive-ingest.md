# archive_ingest.py Implementation Plan

> **Historical (2026-05-15).** Implemented. Module now lives at `darkroom/ingest.py`; CLI is `darkroom ingest`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `darkroom/scanner.py` and `archive_ingest.py` to scan an ASIAir Autorun/Plan folder, generate a reviewed YAML manifest, and copy files to a staging directory while registering sessions in `astro_catalog.db`.

**Architecture:** A pure-read scanner (`darkroom/scanner.py`) reads FITS headers and filenames to return structured `Session` and `CalibrationGroup` dataclasses. `archive_ingest.py` wraps this with a CLI that compares against the catalog, builds a YAML manifest with dedup logic, handles interactive filter prompts or `needs_review` flagging, and in `--commit` mode copies files and upserts to the catalog via imported `fits_cataloger` functions.

**Tech Stack:** Python 3.11+, astropy, pyyaml, fits-cataloger (path dep to `../darkroom-catalog`), sqlite3 (stdlib), tomllib (stdlib 3.11+), unittest.mock (stdlib)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Add fits-cataloger path dep + pyyaml |
| `darkroom/parse.py` | Modify | Delegate `ota_from_focallen()` to `fits_cataloger.parse_ota()` |
| `darkroom/scanner.py` | Create | `Session`, `CalibrationGroup`, `ScanResult` dataclasses + `scan_source()` |
| `archive_ingest.py` | Implement | CLI, config, path helpers, manifest builder, all command modes |
| `tests/__init__.py` | Create | Empty, marks tests as a package |
| `tests/test_parse.py` | Create | Tests for `darkroom/parse.py` |
| `tests/test_scanner.py` | Create | Tests for `darkroom/scanner.py` |
| `tests/test_ingest.py` | Create | Tests for pure functions in `archive_ingest.py` |

---

## Task 1: Add fits-cataloger and pyyaml dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependencies via uv**

```bash
cd /Users/jpoh/Projects/darkroom-ingest
uv add pyyaml
uv add --editable /Users/jpoh/Projects/darkroom-catalog
```

- [ ] **Step 2: Verify pyproject.toml was updated correctly**

`pyproject.toml` should now include:
```toml
dependencies = [
    "astropy>=5.0",
    "fits-cataloger",
    "pyyaml>=6.0",
]

[tool.uv.sources]
fits-cataloger = { path = "../darkroom-catalog", editable = true }
```

- [ ] **Step 3: Verify imports work**

```bash
uv run python -c "from fits_cataloger import FITSHeaderExtractor, compute_imaging_night, parse_ota, make_session_id, init_db, upsert_session, upsert_calibration_set; print('OK')"
```

Expected output: `OK`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add fits-cataloger path dep and pyyaml"
```

---

## Task 2: Update parse.py and write tests

**Files:**
- Modify: `darkroom/parse.py`
- Create: `tests/__init__.py`
- Create: `tests/test_parse.py`

- [ ] **Step 1: Create tests package**

```bash
touch /Users/jpoh/Projects/darkroom-ingest/tests/__init__.py
```

- [ ] **Step 2: Write failing tests for `ota_from_focallen` tolerance window**

Create `tests/test_parse.py`:

```python
import pytest
from darkroom.parse import (
    ota_from_focallen,
    parse_filter,
    parse_exposure,
    parse_datetime,
    flat_morning_date,
)
from datetime import datetime, date


def test_ota_exact():
    assert ota_from_focallen(400) == "FRA400"
    assert ota_from_focallen(180) == "FMA180"


def test_ota_tolerance():
    # ASIAir reports measured focal length, not nominal
    assert ota_from_focallen(402) == "FRA400"
    assert ota_from_focallen(185) == "FMA180"
    assert ota_from_focallen(170) == "FMA180"
    assert ota_from_focallen(190) == "FMA180"
    assert ota_from_focallen(390) == "FRA400"
    assert ota_from_focallen(410) == "FRA400"


def test_ota_unknown():
    assert ota_from_focallen(250) == "Unknown"
    assert ota_from_focallen(None) == "Unknown"


def test_parse_filter_with_filter():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001"
    assert parse_filter(stem) == "L-Pro"


def test_parse_filter_normalises_lextreme():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_LExtreme_0001"
    assert parse_filter(stem) == "L-Extreme"


def test_parse_filter_no_filter():
    stem = "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001"
    assert parse_filter(stem) is None


def test_parse_exposure():
    assert parse_exposure("Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001") == "180.0s"
    assert parse_exposure("Flat_130.0ms_Bin1_585MC_gain200_20260221-093939_-20.0C_0001") == "130.0ms"


def test_parse_datetime():
    stem = "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001"
    dt = parse_datetime(stem)
    assert dt == datetime(2026, 2, 19, 22, 0, 0)


def test_flat_morning_date_post_midnight():
    # Session ends at 04:00 local → flats taken same morning
    end_dt = datetime(2026, 2, 20, 4, 0, 0)
    assert flat_morning_date(end_dt) == date(2026, 2, 20)


def test_flat_morning_date_evening():
    # Session ends at 22:00 → flats taken next morning
    end_dt = datetime(2026, 2, 19, 22, 0, 0)
    assert flat_morning_date(end_dt) == date(2026, 2, 20)
```

- [ ] **Step 3: Run tests to confirm they fail on `test_ota_tolerance`**

```bash
uv run pytest tests/test_parse.py -v
```

Expected: `test_ota_tolerance` FAILS (current implementation uses exact match).

- [ ] **Step 4: Update `darkroom/parse.py` to delegate `ota_from_focallen`**

In `darkroom/parse.py`, replace the `FOCALLEN_TO_OTA` dict and `ota_from_focallen` function:

```python
from fits_cataloger import parse_ota as _fits_parse_ota


def ota_from_focallen(focal_length: int | float | None) -> str:
    """Infer OTA name from focal length header value (delegates to fits_cataloger)."""
    return _fits_parse_ota(focal_length)
```

Remove the `FOCALLEN_TO_OTA` dict — it is no longer used.

- [ ] **Step 5: Run tests to confirm all pass**

```bash
uv run pytest tests/test_parse.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add darkroom/parse.py tests/__init__.py tests/test_parse.py
git commit -m "feat: delegate ota_from_focallen to fits_cataloger tolerance window, add parse tests"
```

---

## Task 3: scanner.py — dataclasses and light frame scanning

**Files:**
- Create: `darkroom/scanner.py`
- Create: `tests/test_scanner.py`

- [ ] **Step 1: Write failing tests for light frame scanning**

Create `tests/test_scanner.py`:

```python
import pytest
from unittest.mock import patch
import tempfile
from pathlib import Path
from darkroom.scanner import scan_source, Session, CalibrationGroup, ScanResult


# Reusable metadata template for a single light frame
def light_meta(filename_stem: str, date_obs: str = "2026-02-19T22:00:00") -> dict:
    return {
        "filename_stem": filename_stem,
        "file_path": "",
        "date_obs": date_obs,
        "exposure": 180.0,
        "camera": "ZWO ASI585MC Pro",
        "gain": 200,
        "temperature": -20.0,
        "object": "M 81",
        "filter_header": None,
        "imagetyp": "Light Frame",
        "focallen": 400,
        "ra_deg": 148.888,
        "dec_deg": 69.065,
    }


def test_scan_source_single_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        f1 = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001.fit"
        f2 = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220300_-20.0C_L-Pro_0002.fit"
        f1.touch()
        f2.touch()

        def mock_extract(path):
            return {**light_meta(path.stem), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert isinstance(result, ScanResult)
    assert len(result.sessions) == 1
    s = result.sessions[0]
    assert s.target == "M 81"
    assert s.obs_date == "2026-02-19"
    assert s.filter == "L-Pro"
    assert s.ota == "FRA400"
    assert s.camera == "ZWO ASI585MC Pro"
    assert s.gain == 200
    assert s.temperature_c == -20.0
    assert s.exposure_sec == 180.0
    assert s.ra_deg == pytest.approx(148.888)
    assert len(s.files) == 2


def test_scan_source_two_nights_same_target():
    # Frames on two different imaging nights produce two sessions
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 101"
        light_dir.mkdir(parents=True)
        f1 = light_dir / "Light_M 101_180.0s_Bin1_585MC_gain200_20260222-220000_-20.0C_L-Pro_0001.fit"
        f2 = light_dir / "Light_M 101_180.0s_Bin1_585MC_gain200_20260225-220000_-20.0C_L-Pro_0001.fit"
        f1.touch()
        f2.touch()

        def mock_extract(path):
            date_obs = "2026-02-22T22:00:00" if "20260222" in path.name else "2026-02-25T22:00:00"
            meta = {**light_meta(path.stem, date_obs), "file_path": str(path), "object": "M 101"}
            return meta

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.sessions) == 2
    dates = {s.obs_date for s in result.sessions}
    assert dates == {"2026-02-22", "2026-02-25"}


def test_scan_source_no_filter_in_filename():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 51"
        light_dir.mkdir(parents=True)
        f = light_dir / "Light_M 51_300.0s_Bin1_585MC_gain200_20260228-220000_-20.0C_0001.fit"
        f.touch()

        def mock_extract(path):
            return {**light_meta(path.stem, "2026-02-28T22:00:00"), "file_path": str(path), "object": "M 51", "exposure": 300.0}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.sessions) == 1
    assert result.sessions[0].filter is None


def test_scan_source_thumbnails_excluded():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        light_dir = source / "Light" / "M 81"
        light_dir.mkdir(parents=True)
        fit = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001.fit"
        thn = light_dir / "Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220000_-20.0C_L-Pro_0001_thn.jpg"
        fit.touch()
        thn.touch()

        def mock_extract(path):
            return {**light_meta(path.stem), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.sessions[0].files) == 1
    assert result.sessions[0].files[0].suffix == ".fit"


def test_scan_source_empty_source():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = scan_source(Path(tmpdir))
    assert result.sessions == []
    assert result.calibration == []
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_scanner.py -v
```

Expected: ImportError or ModuleNotFoundError — `darkroom/scanner.py` does not exist yet.

- [ ] **Step 3: Create `darkroom/scanner.py` with dataclasses and light scanning**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fits_cataloger import FITSHeaderExtractor, compute_imaging_night, parse_ota
from darkroom.parse import fits_files, parse_filter


@dataclass
class Session:
    target: str
    obs_date: str          # YYYY-MM-DD local imaging night
    ota: str
    camera: str
    filter: str | None     # None when not detected in filenames
    gain: int
    temperature_c: float
    exposure_sec: float
    ra_deg: float | None
    dec_deg: float | None
    files: list[Path] = field(default_factory=list)


@dataclass
class CalibrationGroup:
    frame_type: str        # Flat | Dark | FlatDark | Bias
    camera: str
    ota: str
    filter: str | None     # only for Flat and FlatDark
    gain: int
    exposure_sec: float
    temperature_c: float   # rounded to nearest integer
    capture_date: str      # YYYY-MM-DD from DATE-OBS header
    files: list[Path] = field(default_factory=list)


@dataclass
class ScanResult:
    sessions: list[Session] = field(default_factory=list)
    calibration: list[CalibrationGroup] = field(default_factory=list)


def scan_source(source: Path) -> ScanResult:
    """Scan an Autorun or Plan folder and return all sessions and calibration groups."""
    return ScanResult(
        sessions=_scan_lights(source / "Light"),
        calibration=_scan_calibration(source),
    )


def _scan_lights(light_root: Path) -> list[Session]:
    if not light_root.is_dir():
        return []

    sessions: list[Session] = []
    for target_dir in sorted(light_root.iterdir()):
        if not target_dir.is_dir() or target_dir.name.startswith("."):
            continue

        pairs: list[tuple[dict, Path]] = []
        for path in fits_files(target_dir):
            meta = FITSHeaderExtractor.extract_metadata(path)
            if meta:
                pairs.append((meta, path))

        if not pairs:
            continue

        # Group by imaging night (local Lisbon civil date)
        nights: dict[str, list[tuple[dict, Path]]] = {}
        for meta, path in pairs:
            night = compute_imaging_night(meta.get("date_obs", ""))
            if night is None:
                continue
            nights.setdefault(night, []).append((meta, path))

        for night, frames in sorted(nights.items()):
            first_meta = frames[0][0]

            # Filter: first filename that carries one wins
            filter_: str | None = None
            for meta, _ in frames:
                filter_ = parse_filter(meta["filename_stem"])
                if filter_ is not None:
                    break

            sessions.append(Session(
                target=target_dir.name,
                obs_date=night,
                ota=parse_ota(first_meta.get("focallen")),
                camera=first_meta["camera"],
                filter=filter_,
                gain=first_meta["gain"],
                temperature_c=first_meta["temperature"],
                exposure_sec=first_meta["exposure"],
                ra_deg=first_meta.get("ra_deg"),
                dec_deg=first_meta.get("dec_deg"),
                files=[path for _, path in frames],
            ))

    return sessions


def _scan_calibration(source: Path) -> list[CalibrationGroup]:
    # Darks with exposure_sec below this threshold are flat darks
    FLAT_DARK_THRESHOLD_SEC = 10.0

    groups: dict[tuple, CalibrationGroup] = {}

    for folder_name in ("Flat", "Dark", "Bias"):
        folder = source / folder_name
        if not folder.is_dir():
            continue

        for path in fits_files(folder):
            meta = FITSHeaderExtractor.extract_metadata(path)
            if not meta:
                continue

            # Frame type from source folder name; reclassify short darks as flat darks
            frame_type = folder_name
            if frame_type == "Dark" and meta["exposure"] < FLAT_DARK_THRESHOLD_SEC:
                frame_type = "FlatDark"

            # DATE-OBS → YYYY-MM-DD
            capture_date = ""
            date_obs = meta.get("date_obs", "")
            if date_obs:
                try:
                    from astropy.time import Time
                    capture_date = Time(date_obs, format="isot").datetime.strftime("%Y-%m-%d")
                except Exception:
                    pass

            # Filter only meaningful for Flat and FlatDark
            filter_: str | None = None
            if frame_type in ("Flat", "FlatDark"):
                filter_ = parse_filter(path.stem)
                if filter_ is None:
                    filter_ = meta.get("filter_header")

            temp_rounded = round(meta["temperature"])
            key = (frame_type, meta["camera"], meta["gain"], meta["exposure"], temp_rounded, capture_date)

            if key not in groups:
                groups[key] = CalibrationGroup(
                    frame_type=frame_type,
                    camera=meta["camera"],
                    ota=parse_ota(meta.get("focallen")),
                    filter=filter_,
                    gain=meta["gain"],
                    exposure_sec=meta["exposure"],
                    temperature_c=float(temp_rounded),
                    capture_date=capture_date,
                    files=[],
                )
            groups[key].files.append(path)

    return list(groups.values())
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_scanner.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add darkroom/scanner.py tests/test_scanner.py
git commit -m "feat: add scanner with Session/CalibrationGroup dataclasses and light frame detection"
```

---

## Task 4: scanner.py — calibration frame scanning tests

**Files:**
- Modify: `tests/test_scanner.py`

- [ ] **Step 1: Add calibration tests to `tests/test_scanner.py`**

Append to the end of `tests/test_scanner.py`:

```python
def dark_meta(filename_stem: str, exposure: float, date_obs: str = "2026-02-20T09:20:00") -> dict:
    return {
        "filename_stem": filename_stem,
        "file_path": "",
        "date_obs": date_obs,
        "exposure": exposure,
        "camera": "ZWO ASI585MC Pro",
        "gain": 200,
        "temperature": -20.0,
        "object": "",
        "filter_header": None,
        "imagetyp": "Dark Frame",
        "focallen": 400,
        "ra_deg": None,
        "dec_deg": None,
    }


def test_scan_calibration_dark_classified():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f = dark_dir / "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001.fit"
        f.touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   return_value={**dark_meta(f.stem, 180.0), "file_path": str(f)}):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert result.calibration[0].frame_type == "Dark"
    assert result.calibration[0].exposure_sec == 180.0
    assert result.calibration[0].capture_date == "2026-02-20"


def test_scan_calibration_flatdark_reclassified():
    # Short darks (< 10s) in the Dark/ folder are reclassified as FlatDark
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f = dark_dir / "Dark_1.35s_Bin1_585MC_gain200_20260220-093000_-20.0C_0001.fit"
        f.touch()

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata",
                   return_value={**dark_meta(f.stem, 1.35), "file_path": str(f)}):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert result.calibration[0].frame_type == "FlatDark"


def test_scan_calibration_flat_with_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        flat_dir = source / "Flat"
        flat_dir.mkdir(parents=True)
        f = flat_dir / "Flat_1.35s_Bin1_585MC_gain200_20260220-090000_-20.0C_L-Pro_0001.fit"
        f.touch()

        meta = {
            **dark_meta(f.stem, 1.35, "2026-02-20T09:00:00"),
            "file_path": str(f),
            "imagetyp": "Flat Frame",
        }
        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", return_value=meta):
            result = scan_source(source)

    assert len(result.calibration) == 1
    cal = result.calibration[0]
    assert cal.frame_type == "Flat"
    assert cal.filter == "L-Pro"
    assert cal.exposure_sec == 1.35


def test_scan_calibration_groups_same_params():
    # Two files with identical params land in one CalibrationGroup
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        dark_dir = source / "Dark"
        dark_dir.mkdir(parents=True)
        f1 = dark_dir / "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001.fit"
        f2 = dark_dir / "Dark_180.0s_Bin1_585MC_gain200_20260220-093000_-20.0C_0002.fit"
        f1.touch()
        f2.touch()

        def mock_extract(path):
            return {**dark_meta(path.stem, 180.0), "file_path": str(path)}

        with patch("darkroom.scanner.FITSHeaderExtractor.extract_metadata", side_effect=mock_extract):
            result = scan_source(source)

    assert len(result.calibration) == 1
    assert len(result.calibration[0].files) == 2
```

- [ ] **Step 2: Run all scanner tests**

```bash
uv run pytest tests/test_scanner.py -v
```

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scanner.py
git commit -m "test: add calibration frame scanning tests"
```

---

## Task 5: archive_ingest.py — arg parsing, config loading, path helpers

**Files:**
- Implement: `archive_ingest.py`
- Create: `tests/test_ingest.py`

- [ ] **Step 1: Write failing tests for path helpers**

Create `tests/test_ingest.py`:

```python
import os
import tempfile
import tomllib
from pathlib import Path
import pytest


# Import functions that will exist after implementation
from archive_ingest import (
    camera_slug,
    session_dest_rel,
    cal_dest_rel,
    load_config,
    resolve_path,
)


def test_camera_slug():
    assert camera_slug("ZWO ASI585MC Pro") == "ZWOASI585MCPro"
    assert camera_slug("Canon6D") == "Canon6D"


def test_session_dest_rel():
    result = session_dest_rel("M 81", "2026-02-19", "FRA400", "ZWO ASI585MC Pro", "L-Pro")
    assert result == Path("04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights")


def test_session_dest_rel_no_filter():
    result = session_dest_rel("M 51", "2026-02-28", "FRA400", "ZWO ASI585MC Pro", None)
    assert result == Path("04_Deep Sky Objects/M 51/2026-02-28_FRA400_ZWOASI585MCPro_NoFilter/Lights")


def test_cal_dest_rel_flat():
    result = cal_dest_rel("Flat", "ZWO ASI585MC Pro", "FRA400", "L-Pro", "2026-02-20")
    assert result == Path("00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20")


def test_cal_dest_rel_flat_no_filter():
    result = cal_dest_rel("Flat", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-20")
    assert result == Path("00_Calibration/Flats/FRA400_ZWOASI585MCPro_NoFilter/2026-02-20")


def test_cal_dest_rel_dark():
    result = cal_dest_rel("Dark", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-20")
    assert result == Path("00_Calibration/Darks/ZWOASI585MCPro")


def test_cal_dest_rel_flatdark():
    result = cal_dest_rel("FlatDark", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-21")
    assert result == Path("00_Calibration/FlatDarks/ZWOASI585MCPro")


def test_cal_dest_rel_bias():
    result = cal_dest_rel("Bias", "ZWO ASI585MC Pro", "FRA400", None, "2026-02-21")
    assert result == Path("00_Calibration/Bias/ZWOASI585MCPro/Raw")


def test_load_config_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_config() == {}


def test_load_config_reads_project_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "darkroom.toml").write_text(
        '[darkroom]\noutput_path = "/staging"\ncatalog_path = "/catalog.db"\n'
    )
    config = load_config()
    assert config["darkroom"]["output_path"] == "/staging"


def test_resolve_path_from_cli():
    result = resolve_path("/from/cli", "DARKROOM_OUTPUT", {}, "output_path", "output")
    assert result == Path("/from/cli")


def test_resolve_path_from_env(monkeypatch):
    monkeypatch.setenv("DARKROOM_OUTPUT", "/from/env")
    result = resolve_path(None, "DARKROOM_OUTPUT", {}, "output_path", "output")
    assert result == Path("/from/env")


def test_resolve_path_from_config(monkeypatch):
    monkeypatch.delenv("DARKROOM_OUTPUT", raising=False)
    config = {"darkroom": {"output_path": "/from/config"}}
    result = resolve_path(None, "DARKROOM_OUTPUT", config, "output_path", "output")
    assert result == Path("/from/config")


def test_resolve_path_missing_exits(monkeypatch):
    monkeypatch.delenv("DARKROOM_OUTPUT", raising=False)
    with pytest.raises(SystemExit):
        resolve_path(None, "DARKROOM_OUTPUT", {}, "output_path", "output")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_ingest.py -v
```

Expected: ImportError — `archive_ingest` not yet implemented.

- [ ] **Step 3: Implement `archive_ingest.py` skeleton with these functions**

Replace the stub `archive_ingest.py` with:

```python
#!/usr/bin/env python3
"""archive_ingest.py — Copy a completed ASIAir session into canonical archive structure."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fits_cataloger import (
    init_db,
    make_session_id,
    upsert_calibration_set,
    upsert_session,
)

from darkroom.scanner import CalibrationGroup, Session, ScanResult, scan_source


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load darkroom.toml from project dir or ~/.config/darkroom/."""
    for p in [
        Path("darkroom.toml"),
        Path.home() / ".config" / "darkroom" / "darkroom.toml",
    ]:
        if p.exists():
            with open(p, "rb") as f:
                return tomllib.load(f)
    return {}


def resolve_path(
    cli_val: str | None,
    env_key: str,
    config: dict,
    config_key: str,
    label: str,
) -> Path:
    """Resolve a path from CLI → env var → config, exit with error if missing."""
    val = cli_val or os.environ.get(env_key) or config.get("darkroom", {}).get(config_key)
    if not val:
        print(
            f"Error: {label} path required. Use --{label}, {env_key} env var, "
            f"or set {config_key} in darkroom.toml",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(val)


# ---------------------------------------------------------------------------
# Destination path helpers
# ---------------------------------------------------------------------------

def camera_slug(camera: str) -> str:
    """Strip spaces from camera name for use in folder names."""
    return re.sub(r"\s+", "", camera)


def session_dest_rel(
    target: str, obs_date: str, ota: str, camera: str, filter_: str | None
) -> Path:
    """Return relative destination path for a session's Lights/ folder."""
    f = filter_ or "NoFilter"
    folder = f"{obs_date}_{ota}_{camera_slug(camera)}_{f}"
    return Path("04_Deep Sky Objects") / target / folder / "Lights"


def cal_dest_rel(
    frame_type: str, camera: str, ota: str, filter_: str | None, capture_date: str
) -> Path:
    """Return relative destination path for a calibration group's folder."""
    slug = camera_slug(camera)
    if frame_type == "Flat":
        f = filter_ or "NoFilter"
        return Path("00_Calibration") / "Flats" / f"{ota}_{slug}_{f}" / capture_date
    if frame_type == "Dark":
        return Path("00_Calibration") / "Darks" / slug
    if frame_type == "FlatDark":
        return Path("00_Calibration") / "FlatDarks" / slug
    if frame_type == "Bias":
        return Path("00_Calibration") / "Bias" / slug / "Raw"
    raise ValueError(f"Unknown frame type: {frame_type}")


# ---------------------------------------------------------------------------
# Placeholders for later tasks
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    config = load_config()
    print("archive_ingest: not fully implemented yet")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive ASIAir session to canonical folder structure."
    )
    parser.add_argument("--source", required=False, metavar="PATH")
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--catalog", metavar="PATH")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--manifest", metavar="FILE")
    mode.add_argument("--review", metavar="FILE")
    mode.add_argument("--commit", nargs="?", const=True, metavar="FILE")
    return parser


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_ingest.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add archive_ingest.py tests/test_ingest.py
git commit -m "feat: archive_ingest skeleton — config loading and destination path helpers"
```

---

## Task 6: archive_ingest.py — filter prompt and needs_review logic

**Files:**
- Modify: `archive_ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Add tests for filter resolution**

Append to `tests/test_ingest.py`:

```python
from archive_ingest import resolve_filter, KNOWN_FILTERS


def test_resolve_filter_known():
    # When filter is already detected, return it unchanged
    assert resolve_filter("L-Pro", interactive=False) == ("L-Pro", False)
    assert resolve_filter("L-Extreme", interactive=False) == ("L-Extreme", False)


def test_resolve_filter_non_interactive_unknown():
    # No TTY: return NoFilter with needs_review=True
    result = resolve_filter(None, interactive=False)
    assert result == ("NoFilter", True)


def test_resolve_filter_interactive_chooses_from_list(monkeypatch):
    # Simulate user entering "1" to choose L-Pro
    monkeypatch.setattr("builtins.input", lambda _: "1")
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == KNOWN_FILTERS[0]
    assert needs_review is False


def test_resolve_filter_interactive_empty_input_gives_nofilter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == "NoFilter"
    assert needs_review is False


def test_resolve_filter_interactive_manual_entry(monkeypatch):
    inputs = iter([str(len(KNOWN_FILTERS) + 1), "AstronomikL2"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    filter_, needs_review = resolve_filter(None, interactive=True, context="M 51 on 2026-02-28")
    assert filter_ == "AstronomikL2"
    assert needs_review is False
```

- [ ] **Step 2: Run to confirm these tests fail**

```bash
uv run pytest tests/test_ingest.py::test_resolve_filter_known -v
```

Expected: ImportError — `resolve_filter` not defined yet.

- [ ] **Step 3: Add `KNOWN_FILTERS` and `resolve_filter` to `archive_ingest.py`**

Add after the `cal_dest_rel` function:

```python
# ---------------------------------------------------------------------------
# Filter prompt
# ---------------------------------------------------------------------------

KNOWN_FILTERS = ["L-Pro", "L-Extreme", "AstronomikL2", "BaaderNeodymium", "OmegonHelievo"]


def resolve_filter(
    detected: str | None,
    interactive: bool,
    context: str = "",
) -> tuple[str, bool]:
    """Return (filter_str, needs_review).

    If filter is already detected, returns it directly. If missing and interactive,
    prompts the user. If missing and non-interactive, returns ('NoFilter', True).
    """
    if detected is not None:
        return detected, False

    if not interactive:
        return "NoFilter", True

    if context:
        print(f"\nNo filter detected for: {context}")
    else:
        print("\nNo filter detected.")

    for i, f in enumerate(KNOWN_FILTERS, 1):
        print(f"  {i}) {f}")
    print(f"  {len(KNOWN_FILTERS) + 1}) Enter manually")
    print("  [Enter] NoFilter")

    while True:
        try:
            raw = input("> ").strip()
            if not raw:
                return "NoFilter", False
            n = int(raw)
            if 1 <= n <= len(KNOWN_FILTERS):
                return KNOWN_FILTERS[n - 1], False
            if n == len(KNOWN_FILTERS) + 1:
                manual = input("Filter name: ").strip()
                return (manual or "NoFilter"), False
        except ValueError:
            print("Please enter a number.")
        except EOFError:
            return "NoFilter", False
```

- [ ] **Step 4: Run all ingest tests**

```bash
uv run pytest tests/test_ingest.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add archive_ingest.py tests/test_ingest.py
git commit -m "feat: add filter prompt with known filter list and needs_review flag"
```

---

## Task 7: archive_ingest.py — manifest generation for sessions

**Files:**
- Modify: `archive_ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Add tests for session manifest building**

Append to `tests/test_ingest.py`:

```python
from pathlib import Path
from archive_ingest import build_session_entry, existing_catalog_sessions, make_cal_set_id
from darkroom.scanner import Session


def _make_session(filter_="L-Pro", n_files=3) -> Session:
    with tempfile.TemporaryDirectory() as tmpdir:
        files = []
        for i in range(n_files):
            f = Path(tmpdir) / f"Light_M 81_180.0s_Bin1_585MC_gain200_20260219-220{i:02d}00_-20.0C_L-Pro_{i+1:04d}.fit"
            f.touch()
            files.append(f)
        return Session(
            target="M 81", obs_date="2026-02-19", ota="FRA400",
            camera="ZWO ASI585MC Pro", filter=filter_, gain=200,
            temperature_c=-20.0, exposure_sec=180.0, ra_deg=148.888,
            dec_deg=69.065, files=files,
        )


import tempfile


def test_build_session_entry_new():
    session = _make_session()
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=False)

    assert entry["session_id"] == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    assert entry["status"] == "new"
    assert entry["needs_review"] is False
    assert entry["filter"] == "L-Pro"
    assert entry["frame_count"] == 3
    assert len(entry["files"]) == 3
    assert all(f["copy"] is True for f in entry["files"])
    assert entry["lights_rel_path"] == "04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights"


def test_build_session_entry_existing_same_count():
    session = _make_session()
    output = Path("/staging")
    catalog = {"M81_20260219_FRA400_ZWOASI585MCPro_L-Pro": 3}
    entry = build_session_entry(session, output, catalog_sessions=catalog, interactive=False)

    assert entry["status"] == "existing"
    assert entry["files"] == []


def test_build_session_entry_no_filter_non_interactive():
    session = _make_session(filter_=None)
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=False)

    assert entry["needs_review"] is True
    assert entry["filter"] is None
    assert "UnknownFilter" in entry["session_id"]


def test_build_session_entry_no_filter_interactive(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "1")  # choose L-Pro
    session = _make_session(filter_=None)
    output = Path("/staging")
    entry = build_session_entry(session, output, catalog_sessions={}, interactive=True)

    assert entry["needs_review"] is False
    assert entry["filter"] == "L-Pro"
    assert entry["session_id"].endswith("_L-Pro")


def test_existing_catalog_sessions_empty_when_no_db(tmp_path):
    result = existing_catalog_sessions(tmp_path / "nonexistent.db")
    assert result == {}


def test_make_cal_set_id():
    result = make_cal_set_id("Flat", "ZWO ASI585MC Pro", 200, 1.35, -20.0, "2026-02-20")
    assert result == "Flat_ZWOASI585MCPro_1.35s_200g_-20C_2026-02-20"
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
uv run pytest tests/test_ingest.py::test_build_session_entry_new -v
```

Expected: ImportError.

- [ ] **Step 3: Add session manifest functions to `archive_ingest.py`**

Add after `resolve_filter`:

```python
# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def existing_catalog_sessions(catalog_path: Path) -> dict[str, int]:
    """Return {session_id: frame_count} for all sessions in the catalog."""
    if not catalog_path.exists():
        return {}
    with sqlite3.connect(catalog_path) as conn:
        rows = conn.execute("SELECT session_id, frame_count FROM sessions").fetchall()
    return {r[0]: r[1] for r in rows}


def make_cal_set_id(
    frame_type: str,
    camera: str,
    gain: int,
    exposure_sec: float,
    temperature_c: float,
    capture_date: str,
) -> str:
    """Build a calibration set primary key matching fits_cataloger's convention."""
    slug = camera_slug(camera)
    temp_str = f"{int(temperature_c)}C"
    return f"{frame_type}_{slug}_{exposure_sec:.3g}s_{gain}g_{temp_str}_{capture_date}"


# ---------------------------------------------------------------------------
# Manifest entry builders
# ---------------------------------------------------------------------------

def build_session_entry(
    session: Session,
    output: Path,
    catalog_sessions: dict[str, int],
    interactive: bool,
) -> dict:
    """Build one sessions[] manifest entry for the given Session."""
    filter_, needs_review = resolve_filter(
        session.filter,
        interactive=interactive,
        context=f"{session.target} on {session.obs_date}",
    )

    # Pass None for filter when unknown so make_session_id uses "UnknownFilter"
    session_id = make_session_id(
        session.target,
        session.obs_date,
        session.ota,
        session.camera,
        None if needs_review else filter_,
    )
    dest_rel = session_dest_rel(
        session.target, session.obs_date, session.ota, session.camera,
        None if needs_review else filter_,
    )
    dest_abs = output / dest_rel

    existing = catalog_sessions.get(session_id)
    if existing is None:
        status = "new"
        file_entries = [
            {"src": str(f), "dst": str(dest_rel / f.name), "copy": True}
            for f in sorted(session.files)
        ]
    elif existing == len(session.files):
        status = "existing"
        file_entries = []
    else:
        status = "topup"
        existing_names = (
            {p.name for p in dest_abs.iterdir() if p.is_file()}
            if dest_abs.exists()
            else set()
        )
        file_entries = [
            {"src": str(f), "dst": str(dest_rel / f.name), "copy": True}
            for f in sorted(session.files)
            if f.name not in existing_names
        ]

    return {
        "session_id": session_id,
        "target": session.target,
        "obs_date": session.obs_date,
        "ota": session.ota,
        "camera": session.camera,
        "filter": None if needs_review else filter_,
        "gain": session.gain,
        "temperature_c": session.temperature_c,
        "exposure_sec": session.exposure_sec,
        "frame_count": len(session.files),
        "ra_deg": session.ra_deg,
        "dec_deg": session.dec_deg,
        "needs_review": needs_review,
        "status": status,
        "lights_rel_path": str(dest_rel),
        "files": file_entries,
    }
```

- [ ] **Step 4: Run all ingest tests**

```bash
uv run pytest tests/test_ingest.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add archive_ingest.py tests/test_ingest.py
git commit -m "feat: add session manifest entry builder with dedup and needs_review logic"
```

---

## Task 8: archive_ingest.py — manifest generation for calibration

**Files:**
- Modify: `archive_ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Add tests for calibration manifest building**

Append to `tests/test_ingest.py`:

```python
from archive_ingest import build_cal_entry
from darkroom.scanner import CalibrationGroup


def _make_cal_group(frame_type="Flat", filter_="L-Pro", n_files=2) -> CalibrationGroup:
    with tempfile.TemporaryDirectory() as tmpdir:
        files = []
        for i in range(n_files):
            f = Path(tmpdir) / f"Flat_1.35s_Bin1_585MC_gain200_20260220-09{i:02d}00_-20.0C_L-Pro_{i+1:04d}.fit"
            f.touch()
            files.append(f)
        return CalibrationGroup(
            frame_type=frame_type, camera="ZWO ASI585MC Pro", ota="FRA400",
            filter=filter_, gain=200, exposure_sec=1.35, temperature_c=-20.0,
            capture_date="2026-02-20", files=files,
        )


def test_build_cal_entry_flat_all_new(tmp_path):
    group = _make_cal_group()
    entry = build_cal_entry(group, output=tmp_path, interactive=False)

    assert entry["set_id"] == "Flat_ZWOASI585MCPro_1.35s_200g_-20C_2026-02-20"
    assert entry["frame_type"] == "Flat"
    assert entry["filter"] == "L-Pro"
    assert entry["needs_review"] is False
    assert entry["folder_rel_path"] == "00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20"
    assert len(entry["files"]) == 2
    assert all(f["copy"] is True for f in entry["files"])


def test_build_cal_entry_files_already_at_dest(tmp_path):
    group = _make_cal_group(n_files=1)
    dest_dir = tmp_path / "00_Calibration" / "Flats" / "FRA400_ZWOASI585MCPro_L-Pro" / "2026-02-20"
    dest_dir.mkdir(parents=True)
    # Pre-create the file at destination
    (dest_dir / group.files[0].name).touch()

    entry = build_cal_entry(group, output=tmp_path, interactive=False)

    assert len(entry["files"]) == 1
    assert entry["files"][0]["copy"] is False


def test_build_cal_entry_dark_no_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        group = CalibrationGroup(
            frame_type="Dark", camera="ZWO ASI585MC Pro", ota="FRA400",
            filter=None, gain=200, exposure_sec=180.0, temperature_c=-20.0,
            capture_date="2026-02-20",
            files=[Path(tmpdir) / "Dark_180.0s_Bin1_585MC_gain200_20260220-092000_-20.0C_0001.fit"],
        )
        group.files[0].touch()
        entry = build_cal_entry(group, output=Path(tmpdir) / "out", interactive=False)

    assert entry["needs_review"] is False
    assert entry["folder_rel_path"] == "00_Calibration/Darks/ZWOASI585MCPro"
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
uv run pytest tests/test_ingest.py::test_build_cal_entry_flat_all_new -v
```

Expected: ImportError.

- [ ] **Step 3: Add `build_cal_entry` to `archive_ingest.py`**

Add after `build_session_entry`:

```python
def build_cal_entry(
    group: CalibrationGroup,
    output: Path,
    interactive: bool,
) -> dict:
    """Build one calibration[] manifest entry for the given CalibrationGroup."""
    # Filter resolution only matters for Flat/FlatDark
    if group.frame_type in ("Flat", "FlatDark"):
        filter_, needs_review = resolve_filter(
            group.filter,
            interactive=interactive,
            context=f"{group.frame_type} on {group.capture_date}",
        )
    else:
        filter_ = group.filter
        needs_review = False

    set_id = make_cal_set_id(
        group.frame_type, group.camera, group.gain,
        group.exposure_sec, group.temperature_c, group.capture_date,
    )
    dest_rel = cal_dest_rel(
        group.frame_type, group.camera, group.ota, filter_, group.capture_date
    )
    dest_abs = output / dest_rel

    file_entries = []
    for f in sorted(group.files):
        dest_file = dest_abs / f.name
        file_entries.append({
            "src": str(f),
            "dst": str(dest_rel / f.name),
            "copy": not dest_file.exists(),
        })

    return {
        "set_id": set_id,
        "frame_type": group.frame_type,
        "camera": group.camera,
        "ota": group.ota,
        "filter": None if needs_review else filter_,
        "gain": group.gain,
        "exposure_sec": group.exposure_sec,
        "temperature_c": group.temperature_c,
        "capture_date": group.capture_date,
        "frame_count": len(group.files),
        "needs_review": needs_review,
        "folder_rel_path": str(dest_rel),
        "files": file_entries,
    }
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add archive_ingest.py tests/test_ingest.py
git commit -m "feat: add calibration manifest entry builder"
```

---

## Task 9: archive_ingest.py — `--dry-run` and `--manifest` modes

**Files:**
- Modify: `archive_ingest.py`

- [ ] **Step 1: Add `build_manifest` and `cmd_scan` to `archive_ingest.py`**

Add after `build_cal_entry`:

```python
# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------

def build_manifest(
    scan: ScanResult,
    source: Path,
    output: Path,
    catalog: Path,
    interactive: bool,
) -> dict:
    """Build the full manifest dict from a ScanResult."""
    catalog_sessions = existing_catalog_sessions(catalog)

    session_entries = [
        build_session_entry(s, output, catalog_sessions, interactive)
        for s in scan.sessions
    ]
    cal_entries = [
        build_cal_entry(g, output, interactive)
        for g in scan.calibration
    ]

    return {
        "meta": {
            "source": str(source),
            "output": str(output),
            "catalog": str(catalog),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "sessions": session_entries,
        "calibration": cal_entries,
    }


def cmd_scan(args: argparse.Namespace, config: dict, *, write_file: bool) -> None:
    """Handle --dry-run and --manifest modes."""
    source = Path(args.source)
    output = resolve_path(args.output, "DARKROOM_OUTPUT", config, "output_path", "output")
    catalog = resolve_path(args.catalog, "DARKROOM_CATALOG", config, "catalog_path", "catalog")
    interactive = sys.stdin.isatty()

    if not source.exists():
        print(f"Error: source path does not exist: {source}", file=sys.stderr)
        sys.exit(1)

    scan = scan_source(source)
    manifest = build_manifest(scan, source, output, catalog, interactive)

    yaml_str = yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True)

    if write_file:
        dest = Path(args.manifest)
        dest.write_text(yaml_str)
        needs_review = sum(
            1 for e in manifest["sessions"] + manifest["calibration"]
            if e.get("needs_review")
        )
        print(f"Manifest written to {dest}")
        if needs_review:
            print(f"  {needs_review} item(s) need filter review — run: archive_ingest.py --review {dest}")
    else:
        print(yaml_str)
```

- [ ] **Step 2: Wire `--dry-run` and `--manifest` into `main()`**

Replace the `main()` function:

```python
def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    config = load_config()

    if args.dry_run:
        if not args.source:
            parser.error("--dry-run requires --source")
        cmd_scan(args, config, write_file=False)
    elif args.manifest:
        if not args.source:
            parser.error("--manifest requires --source")
        cmd_scan(args, config, write_file=True)
    elif args.review:
        cmd_review(args, config)
    elif args.commit is not None:
        cmd_commit(args, config)
    else:
        parser.print_help()
```

Also update `_build_parser()` to mark `--source` as not required (it's conditionally required):

The `--source` argument was already `required=False` in the skeleton from Task 5. No change needed.

- [ ] **Step 3: Add stub implementations for `cmd_review` and `cmd_commit` so `main()` doesn't crash**

Add after `cmd_scan`:

```python
def cmd_review(args: argparse.Namespace, config: dict) -> None:
    raise NotImplementedError("--review not yet implemented")


def cmd_commit(args: argparse.Namespace, config: dict) -> None:
    raise NotImplementedError("--commit not yet implemented")
```

- [ ] **Step 4: Smoke-test dry-run against real Autorun data**

```bash
uv run python archive_ingest.py \
  --source "/Users/jpoh/02_Astrophotography/01_ ASIAir/ASIAIR/Autorun" \
  --output /tmp/darkroom-test \
  --catalog /Users/jpoh/Projects/darkroom-catalog/astro_catalog.db \
  --dry-run 2>&1 | head -80
```

Expected: YAML manifest printed to stdout showing sessions and calibration groups.

- [ ] **Step 5: Commit**

```bash
git add archive_ingest.py
git commit -m "feat: implement --dry-run and --manifest modes"
```

---

## Task 10: archive_ingest.py — `--review` mode

**Files:**
- Modify: `archive_ingest.py`

- [ ] **Step 1: Replace `cmd_review` stub with implementation**

Replace the `cmd_review` stub:

```python
def cmd_review(args: argparse.Namespace, config: dict) -> None:
    """Interactively resolve needs_review items in a saved manifest file."""
    manifest_path = Path(args.review)
    if not manifest_path.exists():
        print(f"Error: manifest file not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = yaml.safe_load(manifest_path.read_text())
    changed = False

    for entry in manifest.get("sessions", []) + manifest.get("calibration", []):
        if not entry.get("needs_review"):
            continue

        is_session = "lights_rel_path" in entry
        context = (
            f"{entry['target']} on {entry['obs_date']}"
            if is_session
            else f"{entry['frame_type']} on {entry['capture_date']}"
        )
        filter_, _ = resolve_filter(None, interactive=True, context=context)
        entry["filter"] = filter_
        entry["needs_review"] = False

        if is_session:
            # Recalculate session_id, lights_rel_path, and all file dst paths
            new_session_id = make_session_id(
                entry["target"], entry["obs_date"],
                entry["ota"], entry["camera"], filter_,
            )
            new_dest_rel = session_dest_rel(
                entry["target"], entry["obs_date"],
                entry["ota"], entry["camera"], filter_,
            )
            entry["session_id"] = new_session_id
            entry["lights_rel_path"] = str(new_dest_rel)
            for f in entry.get("files", []):
                f["dst"] = str(new_dest_rel / Path(f["dst"]).name)
        else:
            # Recalculate set_id, folder_rel_path, and all file dst paths
            new_set_id = make_cal_set_id(
                entry["frame_type"], entry["camera"], entry["gain"],
                entry["exposure_sec"], entry["temperature_c"], entry["capture_date"],
            )
            new_dest_rel = cal_dest_rel(
                entry["frame_type"], entry["camera"], entry["ota"],
                filter_, entry["capture_date"],
            )
            entry["set_id"] = new_set_id
            entry["folder_rel_path"] = str(new_dest_rel)
            for f in entry.get("files", []):
                f["dst"] = str(new_dest_rel / Path(f["dst"]).name)

        changed = True

    if changed:
        manifest_path.write_text(
            yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )
        print(f"Updated: {manifest_path}")
    else:
        print("No items needed review.")
```

- [ ] **Step 2: Smoke-test `--review` with a manifest containing a needs_review item**

First generate a manifest with a filterless session (use a source that has one):

```bash
uv run python archive_ingest.py \
  --source "/Users/jpoh/02_Astrophotography/01_ ASIAir/ASIAIR/Autorun" \
  --output /tmp/darkroom-test \
  --catalog /Users/jpoh/Projects/darkroom-catalog/astro_catalog.db \
  --manifest /tmp/test_manifest.yaml
```

If any `needs_review: true` items appear, run:

```bash
uv run python archive_ingest.py --review /tmp/test_manifest.yaml
```

Expected: prompt appears for each flagged item, manifest is rewritten with resolved filters.

- [ ] **Step 3: Commit**

```bash
git add archive_ingest.py
git commit -m "feat: implement --review mode to resolve needs_review manifest items"
```

---

## Task 11: archive_ingest.py — `--commit` mode

**Files:**
- Modify: `archive_ingest.py`

- [ ] **Step 1: Replace `cmd_commit` stub with implementation**

Replace the `cmd_commit` stub:

```python
def cmd_commit(args: argparse.Namespace, config: dict) -> None:
    """Execute a manifest: copy files and register in catalog."""
    if args.commit is True:
        # No manifest file given — scan and commit in one step
        if not args.source:
            print("Error: --commit without a file requires --source", file=sys.stderr)
            sys.exit(1)
        source = Path(args.source)
        output = resolve_path(args.output, "DARKROOM_OUTPUT", config, "output_path", "output")
        catalog = resolve_path(args.catalog, "DARKROOM_CATALOG", config, "catalog_path", "catalog")
        interactive = sys.stdin.isatty()
        scan = scan_source(source)
        manifest = build_manifest(scan, source, output, catalog, interactive)
    else:
        manifest_path = Path(args.commit)
        if not manifest_path.exists():
            print(f"Error: manifest file not found: {manifest_path}", file=sys.stderr)
            sys.exit(1)
        manifest = yaml.safe_load(manifest_path.read_text())
        output = Path(manifest["meta"]["output"])
        catalog = Path(manifest["meta"]["catalog"])

    # Hard-refuse if any needs_review items remain
    flagged = [
        e.get("session_id") or e.get("set_id")
        for e in manifest.get("sessions", []) + manifest.get("calibration", [])
        if e.get("needs_review")
    ]
    if flagged:
        print("Error: manifest has unresolved needs_review items:", file=sys.stderr)
        for item in flagged:
            print(f"  - {item}", file=sys.stderr)
        print("Run: archive_ingest.py --review <manifest>", file=sys.stderr)
        sys.exit(1)

    init_db(catalog)
    files_copied = 0
    files_skipped = 0

    # Copy files
    for entry in manifest.get("sessions", []) + manifest.get("calibration", []):
        if entry.get("status") == "existing":
            continue
        for f in entry.get("files", []):
            if not f.get("copy"):
                files_skipped += 1
                continue
            src = Path(f["src"])
            dst = output / f["dst"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                files_skipped += 1
                continue
            shutil.copy2(src, dst)
            files_copied += 1

    # Upsert catalog entries
    catalog_entries = 0
    for entry in manifest.get("sessions", []):
        if entry.get("status") == "existing":
            continue
        upsert_session(catalog, {
            "session_id": entry["session_id"],
            "target": entry["target"],
            "obs_date": entry["obs_date"],
            "ota": entry["ota"],
            "camera": entry["camera"],
            "filter": entry.get("filter"),
            "gain": entry["gain"],
            "temperature_c": entry["temperature_c"],
            "exposure_sec": entry["exposure_sec"],
            "frame_count": entry["frame_count"],
            "total_integration_sec": int(entry["frame_count"] * entry["exposure_sec"]),
            "ra_deg": entry.get("ra_deg"),
            "dec_deg": entry.get("dec_deg"),
            "lights_path": entry["lights_rel_path"],
            "processed_status": "",
            "notes": "",
        })
        catalog_entries += 1

    for entry in manifest.get("calibration", []):
        upsert_calibration_set(catalog, {
            "set_id": entry["set_id"],
            "frame_type": entry["frame_type"],
            "camera": entry["camera"],
            "ota": entry["ota"],
            "filter": entry.get("filter"),
            "gain": entry["gain"],
            "exposure_sec": entry["exposure_sec"],
            "temperature_c": entry["temperature_c"],
            "frame_count": entry["frame_count"],
            "capture_date": entry["capture_date"],
            "folder_path": entry["folder_rel_path"],
        })
        catalog_entries += 1

    print(f"Done: {files_copied} files copied, {files_skipped} skipped, {catalog_entries} catalog entries written")
```

- [ ] **Step 2: Run the full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add archive_ingest.py
git commit -m "feat: implement --commit mode — copy files and register in catalog"
```

---

## Task 12: End-to-end dry-run smoke test

**Files:** None modified — verification only.

- [ ] **Step 1: Run dry-run against real Autorun data and check output**

```bash
uv run python archive_ingest.py \
  --source "/Users/jpoh/02_Astrophotography/01_ ASIAir/ASIAIR/Autorun" \
  --output /tmp/darkroom-staging \
  --catalog /Users/jpoh/Projects/darkroom-catalog/astro_catalog.db \
  --dry-run 2>&1 | tee /tmp/dry_run_output.yaml
```

Verify manually:
- `sessions:` block lists one entry per target × imaging night
- `calibration:` block groups darks, flats, flat darks, bias correctly
- Short darks (< 10s) appear as `frame_type: FlatDark`
- Sessions without a filter in their filenames appear as `needs_review: true`
- `lights_rel_path` matches the canonical `04_Deep Sky Objects/<Target>/...` format

- [ ] **Step 2: Write manifest to file and test `--review` if any needs_review items**

```bash
uv run python archive_ingest.py \
  --source "/Users/jpoh/02_Astrophotography/01_ ASIAir/ASIAIR/Autorun" \
  --output /tmp/darkroom-staging \
  --catalog /Users/jpoh/Projects/darkroom-catalog/astro_catalog.db \
  --manifest /tmp/test_session.yaml

grep needs_review /tmp/test_session.yaml
```

If any `needs_review: true` lines appear:

```bash
uv run python archive_ingest.py --review /tmp/test_session.yaml
grep needs_review /tmp/test_session.yaml
```

Expected: all `needs_review: false` after review.

- [ ] **Step 3: Final test run**

```bash
uv run pytest tests/ -v
```

Expected: all PASS.

- [ ] **Step 4: Final commit**

```bash
git add -A
git status  # verify nothing unexpected
git commit -m "feat: archive_ingest.py — complete implementation"
```
