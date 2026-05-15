# archive_ingest.py — Design Spec

**Date:** 2026-05-15
**Status:** Approved, pending implementation

## Overview

`archive_ingest.py` copies a completed ASIAir session from the local Autorun/Plan
folder into the canonical archive directory structure. It generates a YAML manifest
for review before any files are written, supports non-interactive (CCC postflight)
operation, and registers new sessions in `astro_catalog.db`.

## Source Structure

CCC copies the ASIAir SD card to:
`~/02_Astrophotography/01_ ASIAir/ASIAIR/`

Relevant subdirectories (flat — all dates mixed, no subfolders except Light):

```
Autorun/          (or Plan/)
  Light/
    <Target>/     ← one subfolder per target name, e.g. "M 81"
      *.fit       ← all dates mixed in one flat directory
  Flat/           ← all dates mixed
  Dark/           ← science darks + flat darks mixed
  Bias/           ← all dates mixed
```

`Plan/` uses the same layout but typically only contains `Light/`. Both are valid
`--source` inputs and handled identically.

New sessions accumulate alongside already-ingested data. The tool uses the catalog
and output path to determine what is new.

## Architecture

Three files:

- **`darkroom/scanner.py`** — pure read: scans source path, returns `Session` and
  `CalibrationGroup` objects. No catalog access, no file writes.
- **`darkroom/parse.py`** — existing, minor update: `ota_from_focallen()` delegates to
  `fits_cataloger.parse_ota()` to gain the focal-length tolerance window.
- **`archive_ingest.py`** — CLI entrypoint: calls scanner, compares against catalog,
  builds manifest, and in commit mode copies files and writes to catalog.

`darkroom-catalog` (`fits-cataloger`) is added as a path dependency. The following
are imported from `fits_cataloger`:
- `FITSHeaderExtractor` — reads FITS headers
- `compute_imaging_night()` — UTC DATE-OBS → local Lisbon imaging night date
- `_infer_frame_type()` — classifies Dark vs FlatDark vs Flat vs Bias
- `parse_ota()` — focal-length tolerance window (170–190 → FMA180, 390–410 → FRA400)
- `make_session_id()` — canonical session primary key
- `init_db()`, `upsert_session()`, `upsert_calibration_set()` — catalog writes


Both projects share `astro_catalog.db`. This is intentional — they are the write and
read sides of a single future unified CLI.

## CLI Flags

```
archive_ingest.py --source <path> [--output <path>] [--catalog <path>]
                  [--dry-run | --manifest <file> | --review <file> | --commit [<file>]]
```

| Flag | Description |
|---|---|
| `--source <path>` | Autorun or Plan folder to scan |
| `--output <path>` | Destination root (env: `DARKROOM_OUTPUT`, then `darkroom.toml`) |
| `--catalog <path>` | astro_catalog.db path (env: `DARKROOM_CATALOG`, then `darkroom.toml`) |
| `--dry-run` | Print manifest to stdout, create nothing |
| `--manifest <file>` | Write manifest YAML to file, create nothing |
| `--review <file>` | Interactively resolve `needs_review` items in a manifest, rewrite file |
| `--commit [<file>]` | Execute manifest file, or scan+commit in one step if no file given |

Config resolution order: CLI flag → env var → `darkroom.toml` (project dir or
`~/.config/darkroom/`). Keys: `output_path`, `catalog_path`.

## Scanner (`darkroom/scanner.py`)

`scan_source(source: Path) -> ScanResult`

**Light frames:**
- Reads every `.fit` in `Light/<Target>/` (thumbnails excluded via `_thn` filter)
- Extracts metadata per file via `FITSHeaderExtractor.extract_metadata()`
- Groups by target, then by imaging night using `compute_imaging_night()`
- Returns one `Session` per (target × night), carrying: `target`, `obs_date`, `ota`,
  `camera`, `filter`, `gain`, `temperature_c`, `exposure_sec`, `ra_deg`, `dec_deg`,
  `files: list[Path]`

**Calibration frames:**
- Scans `Flat/`, `Dark/`, `Bias/` flat directories
- Frame type: inferred via `_infer_frame_type()` + folder name
- Dark/FlatDark split: `exposure_sec < 10.0` → FlatDark (same threshold as
  `fits_cataloger._FLAT_DARK_THRESHOLD_SEC`)
- Groups by `(frame_type, camera, gain, exposure, round(temp), capture_date)`
- Returns one `CalibrationGroup` per group, carrying all metadata + `files: list[Path]`

## Filter Detection and `needs_review`

Filter is read from the filename stem via `parse_filter()`. If absent:

**Interactive mode** (TTY present, or `--review`): user is prompted with a numbered
list before the manifest is written:
```
1) L-Pro
2) L-Extreme
3) AstronomikL2
4) BaaderNeodymium
5) OmegonHelievo
6) Enter manually
[Enter] NoFilter
```
The chosen filter is baked into the manifest.

**Non-interactive mode** (no TTY, e.g. CCC postflight): `filter` is set to `"NoFilter"`
and `needs_review: true` is written into the manifest entry. `--commit` hard-refuses
to proceed if any `needs_review: true` item remains. Use `--review <file>` to resolve.

## Destination Path Mapping

`<CameraSlug>` = `INSTRUME` header value with spaces stripped
(e.g. `"ZWO ASI585MC Pro"` → `"ZWOASI585MCPro"`).

| Frame type | Destination (relative to `--output`) |
|---|---|
| Light | `04_Deep Sky Objects/<Target>/<YYYY-MM-DD>_<OTA>_<CameraSlug>_<Filter>/Lights/` |
| Flat | `00_Calibration/Flats/<OTA>_<CameraSlug>_<Filter>/<YYYY-MM-DD>/` |
| Dark | `00_Calibration/Darks/<CameraSlug>/` |
| FlatDark | `00_Calibration/FlatDarks/<CameraSlug>/` |
| Bias | `00_Calibration/Bias/<CameraSlug>/Raw/` |

Paths stored in the catalog (`lights_path`, `folder_path`) are **relative** to the
output root, so they remain valid after syncing to NAS.

## Deduplication

**Sessions:**
- Query catalog by `session_id`
- Not found → `status: new`, all files `copy: true`
- Found, same `frame_count` → `status: existing`, file list omitted, skip on commit
- Found, different `frame_count` → `status: topup`, only filenames not present at
  destination get `copy: true`; catalog `frame_count` and `total_integration_sec`
  updated on commit

**Calibration:**
- Each file is checked for existence at its destination path
- Present → `copy: false`; absent → `copy: true`
- The calibration set is always upserted to catalog on commit to pick up frame count
  changes

## Manifest YAML Format

```yaml
meta:
  source: /path/to/Autorun
  output: /path/to/staging
  catalog: /path/to/astro_catalog.db
  generated: 2026-05-15T14:32:00

sessions:
  - session_id: M81_20260219_FRA400_ZWOASI585MCPro_L-Pro
    target: M 81
    obs_date: 2026-02-19
    ota: FRA400
    camera: ZWO ASI585MC Pro
    filter: L-Pro
    gain: 200
    temperature_c: -20.0
    exposure_sec: 180.0
    frame_count: 132
    needs_review: false
    status: new          # new | existing | topup
    lights_rel_path: 04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights
    files:
      - src: /abs/path/to/source/Light/M 81/Light_M 81_180.0s_..._0001.fit
        dst: 04_Deep Sky Objects/.../Lights/Light_M 81_180.0s_..._0001.fit
        copy: true

calibration:
  - set_id: Flat_ZWOASI585MCPro_1.35s_200g_-20C_2026-02-20
    frame_type: Flat
    camera: ZWO ASI585MC Pro
    ota: FRA400
    filter: L-Pro
    needs_review: false
    gain: 200
    exposure_sec: 1.35
    temperature_c: -20.0
    capture_date: 2026-02-20
    folder_rel_path: 00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20
    files:
      - src: /abs/path/to/source/Flat/Flat_1.35s_..._0001.fit
        dst: 00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20/Flat_1.35s_..._0001.fit
        copy: true
```

## Commit Behaviour

1. Refuse if any `needs_review: true` item exists in the manifest
2. For each file where `copy: true`: copy source → `<output>/<dst>`, creating
   directories as needed. Never delete or overwrite destination files.
3. Upsert all sessions and calibration sets to catalog via `fits_cataloger` functions
4. Print a summary: files copied, files skipped, catalog entries written

## Workflow

**Interactive (manual run):**
```bash
# Scan, prompt for missing filters, print to stdout
archive_ingest.py --source ~/02_Astrophotography/.../Autorun --dry-run

# Scan, prompt for missing filters, write manifest
archive_ingest.py --source ~/02_Astrophotography/.../Autorun --manifest session.yaml

# Review and commit
archive_ingest.py --commit session.yaml
```

**Non-interactive (CCC postflight):**
```bash
# Step 1 — automated, no TTY
archive_ingest.py --source <path> --manifest session.yaml

# Step 2 — user resolves any needs_review items
archive_ingest.py --review session.yaml

# Step 3 — execute
archive_ingest.py --commit session.yaml
```
