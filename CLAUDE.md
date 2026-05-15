# darkroom — Development Brief

## What This Project Is

The full darkroom suite — unified into a single `darkroom` CLI as of May 2026.
Previously two repos (`darkroom-catalog` read-only, `darkroom-ingest` write
pipeline); now one package with subcommands:

| Subcommand | Purpose |
|---|---|
| `darkroom catalog scan-lights <path>` | Recursively catalog all light sessions on the NAS |
| `darkroom catalog scan-calibration <path>` | Catalog calibration frames |
| `darkroom catalog mark <id> <status>` | Update processed_status for one session |
| `darkroom catalog list [--target X]` | Browse the catalog |
| `darkroom ingest --source <path> [--dry-run \| --manifest F \| --review F \| --commit [F]]` | Archive an ASIAir session |
| `darkroom wbpp --target X [--date Y \| --session ID]` | Build SESSION_N symlink dirs for PixInsight |
| `darkroom finish --target X [--date Y]` | Copy WBPP stacks back to NAS and mark sessions processed |

Shared flags: `--catalog`/`--db` (env `DARKROOM_CATALOG`), `--output` (env
`DARKROOM_OUTPUT`), `--wbpp` (default `./WBPP`). All resolve via CLI → env → `darkroom.toml`
(see `darkroom/config.py`). The toml accepts flat keys or a `[darkroom]` section.

## Pipeline Context

```
ASIAir SD card
      │
      ▼
[CCC copies to Mac]  ← Carbon Copy Cloner, triggered on SD mount (no TTY)
      │
      ▼
darkroom ingest ──→ NAS: 04_Deep Sky Objects/<Target>/<Session>/Lights/
      │                  00_Calibration/Flats/<OTA_Camera_Filter>/<Date>/
      │                  00_Calibration/Darks/<Camera>/
      │
      └──→ astro_catalog.db — register new sessions + calibration sets
      │
      ▼
darkroom wbpp ──→ ~/WBPP/<Target>/SESSION_N/  (symlinks, temporary)
      │            Lights/FILTER_<name>/
      │            Darks/
      │            Flats/FILTER_<name>/
      │            FlatDarks/
      ▼
  PixInsight WBPP → master/*.xisf + processed/*.xisf
      │
      ▼
darkroom finish ──→ NAS: 04_Deep Sky Objects/<Target>/_Processed/<date>/
                    └─→ marks every session_id under that WBPP target as processed
```

## Key Constraints

- **CCC postflight = no TTY**: `darkroom ingest` must be fully non-interactive.
  Use a YAML manifest approach: generate manifest first, user reviews, then `--commit`.
- **Never delete source files**: SD card originals stay until user manually clears them.
- **Filter from filename, not header**: ASIAir does not write FILTER to FITS headers.
  Use `darkroom/parse.py:parse_filter()` everywhere.
- **OTA from FOCALLEN header**: `180 → FMA180`, `400 → FRA400`. See `parse.py:ota_from_focallen()`.
- **Session date = start date**: local calendar date the session began (before midnight),
  not the date it ended (sessions routinely run past midnight).

## NAS Archive Structure (canonical)

Root: `/volume1/Astrophotography/` on Synology NAS.
Mounted on Mac via SMB (confirm mount path — likely `/Volumes/Astrophotography/`).

### Light frames

```
04_Deep Sky Objects/
  <Target with spaces, e.g. "M 81">/
    YYYY-MM-DD_{OTA}_{Camera}_{Filter}/
      Lights/
        *.fit
```

### Calibration frames (go to 00_Calibration, NOT in session folders)

```
00_Calibration/
  Darks/
    <Camera>/            ← masters flat in folder, e.g. masterDark_180s_gain200_-20C.xisf
  FlatDarks/
    <Camera>/            ← Canon6D only; ZWOASI585MCPro doesn't need flat darks
  Bias/
    <Camera>/
      Masters/           ← master .xisf files
      Raw/               ← raw frames
  Flats/
    {OTA}_{Camera}_{Filter}/
      YYYY-MM-DD/        ← raw flat frames, one date subfolder per session
```

## Canonical Naming Convention

| Component | Form | Examples |
|---|---|---|
| OTA | Abbrev + model | `FMA180`, `FRA400` |
| Camera | No spaces, brand + model | `ZWOASI585MCPro`, `Canon6D` |
| Filter | Hyphenated where product does | `L-Pro`, `L-Extreme`, `NoFilter` |
| Gain (ZWO) | lowercase | `gain200`, `gain252` |
| ISO (Canon) | uppercase | `ISO800`, `ISO1600` |
| Temperature | Sign + number + C | `-20C`, `15C` |
| Exposure | Number + s | `180s`, `2s` |
| Date | ISO 8601 | `2026-02-19` |
| Separators | Underscore between, hyphen within | `FRA400_ZWOASI585MCPro_L-Pro` |

## Shared Utilities (`darkroom/parse.py`)

Ported from `asiair-ingestion/scripts/create_wbpp_input.py`. Use these everywhere:

- `parse_filter(stem)` — filter from filename (None if absent)
- `parse_exposure(stem)` — exposure string (e.g. `'180.0s'`)
- `parse_datetime(stem)` — capture datetime
- `flat_morning_date(end_dt)` — date flats were taken (same morning if session ended
  before noon; next morning otherwise)
- `ota_from_focallen(focal_length)` — OTA name from FOCALLEN header value
- `fits_files(directory)` — sorted FITS list, thumbnails excluded

## `darkroom ingest` (was `archive_ingest.py`)

### Inputs
- `--source <path>`: ASIAir output folder (an `Autorun/` directory or equivalent)
- `--nas <path>`: NAS root (default: read from config or env var)
- `--dry-run`: print what would happen, create no files
- `--manifest <yaml>`: path to pre-generated manifest to commit
- `--commit`: execute a previously generated manifest

### Workflow
1. Scan source for FITS files; extract metadata from filenames + headers.
2. Detect session boundaries: gap > 4h between frames = new session.
3. Separate frame types: Light, Dark, Flat, FlatDark.
4. Compute canonical destination paths for each group.
5. Write YAML manifest listing every source→destination move.
6. In `--dry-run` or first pass: print/save manifest, stop.
7. In `--commit` pass: execute copies, then register in `astro_catalog.db`.

### Manifest YAML structure (draft)

```yaml
sessions:
  - session_id: M81_20260219_FRA400_ZWOASI585MCPro_L-Pro
    target: M 81
    obs_date: 2026-02-19
    ota: FRA400
    camera: ZWOASI585MCPro
    filter: L-Pro
    gain: 200
    exposure_sec: 180.0
    frame_count: 132
    source: /Volumes/ASIAIR/Autorun/Light/M 81/
    destination: /Volumes/Astrophotography/04_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro_L-Pro/Lights/

calibration:
  flats:
    - source: /Volumes/ASIAIR/Autorun/Flat/
      destination: /Volumes/Astrophotography/00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20/
      frame_count: 30
  darks:
    - source: /Volumes/ASIAIR/Autorun/Dark/
      destination: /Volumes/Astrophotography/00_Calibration/Darks/ZWOASI585MCPro/
      frame_count: 30
```

## `darkroom wbpp` (was `wbpp_prep.py`)

Generalised from `asiair-ingestion/scripts/create_wbpp_input.py`. Key differences:

- Source is the **NAS archive**, not a local `Autorun/` folder.
- Sessions identified by catalog ID or `--target` + `--date`.
- Flat matching uses **date proximity** (±3 days default), not exact date — because
  archived flats may have been taken on a different occasion than the session.
- Produces WBPP session dirs in `~/WBPP/<TargetSlug>/SESSION_N/` with symlinks.

### Matching rules (inherit from prototype, adjust as needed)

| Frame type | Match key |
|---|---|
| Science darks | Camera + Gain + Exposure (all dates usable) |
| Flats | OTA + Camera + Filter + nearest date within ±N days |
| Flat darks | Flat exposure + flat date (or flat_date + 1 fallback) |

### Inputs
- `--target "M 81"` + `--date 2026-02-19` (looks up session in catalog)
- `--session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro` (direct session ID)
- `--wbpp-root <path>`: where to create SESSION_N dirs (default: `~/WBPP/`)
- `--nas <path>`: NAS root

## Running

```bash
cd /Users/jpoh/Projects/darkroom-ingest
uv sync --extra dev
uv run darkroom --help
uv run darkroom catalog list
uv run datasette serve <catalog_db>      # browse in browser
uv run pytest                            # 168 tests
```

## Package Layout

```
darkroom/
  cli.py            ← entry point (argparse dispatch)
  config.py         ← shared CLI/env/toml path resolution
  cataloger.py      ← FITS header extraction, scan-all/calibration logic, DB schema, upsert/mark fns (was: fits_cataloger.py from old darkroom-catalog repo)
  catalog.py        ← read-only query helpers (find_darks, find_flats, find_flat_darks, query_sessions)
  catalog_cli.py    ← subparser tree for `darkroom catalog ...`
  parse.py          ← filename parsing (parse_filter, parse_exposure, parse_datetime, fits_files)
  scanner.py        ← scan_source — produces Session/CalibrationGroup dataclasses from ASIAir source folder
  ingest.py         ← `darkroom ingest` (was archive_ingest.py)
  prep.py           ← `darkroom wbpp` (was wbpp_prep.py)
  finish.py         ← `darkroom finish` (was wbpp_finish.py)
  wbpp.py           ← symlink helpers used by prep/finish
```

## Relationship to `asiair-ingestion`

`asiair-ingestion` is a **data repository** for the Feb 2026 imaging run, with a
working prototype of `wbpp_prep.py` at `scripts/create_wbpp_input.py`. That script
is hardcoded to the local `Autorun/` folder structure of that specific dataset.

This project generalises those scripts to work from any NAS-archived session.
When implementing, use `asiair-ingestion/scripts/create_wbpp_input.py` as the
reference implementation — the logic is proven, just needs to be decoupled from the
hardcoded paths and connected to the catalog.

## Catalog integration

`darkroom ingest --commit` calls `upsert_session`/`upsert_calibration_set` from
`darkroom.cataloger` directly — no shell-out, no manual SQL. `darkroom finish`
calls `mark_processed` for every session_id resolved from the WBPP target's
SESSION_N symlinks. The catalog is the single source of truth.
