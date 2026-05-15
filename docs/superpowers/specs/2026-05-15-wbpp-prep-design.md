# wbpp_prep.py — Design Spec

**Date:** 2026-05-15
**Status:** Approved, pending implementation

## Overview

`wbpp_prep.py` reads the catalog to locate a session's light frames and matching
calibration frames, then creates a local WBPP directory of symlinks ready for
PixInsight's Weighted Batch Pre-Processing (WBPP) tool. It never copies files,
never writes to the catalog, and never modifies the archive.

## CLI

```
wbpp_prep.py --list [--target "M 81"]
wbpp_prep.py --target "M 81" [--date 2026-02-19]
wbpp_prep.py --session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro
             [--output <path>] [--catalog <path>] [--wbpp <path>]
```

| Flag | Description |
|---|---|
| `--list` | List sessions from catalog, grouped by target and date |
| `--target <name>` | Target name as stored in catalog (e.g. `"M 81"`) |
| `--date <YYYY-MM-DD>` | Restrict `--target` to one imaging night |
| `--session <id>` | Select a single session by catalog ID |
| `--output <path>` | Archive root — lights and calibration paths resolve against this |
| `--catalog <path>` | Path to `astro_catalog.db` |
| `--wbpp <path>` | Root for WBPP output dirs (default: `./WBPP`) |

Config resolution order for `--output` and `--catalog`: CLI flag → env var
(`DARKROOM_OUTPUT`, `DARKROOM_CATALOG`) → `darkroom.toml`. Same mechanism as
`archive_ingest.py`.

## Architecture

Three files:

- **`darkroom/catalog.py`** — all SQL queries against `astro_catalog.db`:
  session lookup, calibration set matching, flat proximity search. Pure reads,
  no writes.
- **`darkroom/wbpp.py`** — symlink creation, `SESSION_N` directory management,
  file discovery within archive folders.
- **`wbpp_prep.py`** — CLI entrypoint, orchestration, interactive prompts for
  ambiguous flat matches.

## Session Resolution

Sessions are always resolved from the catalog. The filesystem is never scanned
for session discovery.

| Invocation | Behaviour |
|---|---|
| `--list` | All sessions, grouped by target then `obs_date`. Shows session ID, filter, frame count, total integration. `--target` narrows to one target. |
| `--target` | All sessions for target, grouped by `obs_date`. Each unique date → one `SESSION_N`. |
| `--target --date` | Sessions for target on that date only → one `SESSION_N`. |
| `--session` | Single session by ID → one `SESSION_N`. |

Multiple sessions on the same night (different filters) are combined into one
`SESSION_N` with separate `Lights/FILTER_<name>/` subdirectories.

## WBPP Output Structure

Output root: `<--wbpp>/<TargetSlug>/` where `TargetSlug` strips spaces
(`"M 81"` → `"M81"`).

Sessions are **always** created as `SESSION_N` directories. `N` continues from
the highest existing `SESSION_*` number in the target dir (or starts at 1).

```
./WBPP/
  M81/
    SESSION_1/
      Lights/
        FILTER_L-Pro/       ← symlinks → output_root/lights_path/*.fit
        FILTER_L-Extreme/   ← (if multiple filters same night)
      Darks/                ← symlinks → matched dark files
      Flats/
        FILTER_L-Pro/       ← symlinks → output_root/flat_folder_path/*.fit
      FlatDarks/            ← symlinks → flat dark files (filtered by date)
    SESSION_2/
      ...
```

All symlinks are absolute (resolved path of the source file). Existing symlinks
are skipped without error.

## Calibration Matching

All matching is done via `calibration_sets` catalog queries. Files are then
discovered by scanning `output_root / folder_path`.

### Darks

Query: `frame_type='Dark'`, `camera`, `gain`, `exposure_sec` matching the
session. Temperature is not a matching criterion (WBPP synthetic dark scaling
handles temperature variation). All files in the matched sets' `folder_path`
that have a matching exposure in their filename (via `parse_exposure()`) are
symlinked into `SESSION_N/Darks/`. Multiple sessions on the same night share
the same Darks dir (deduped by symlink existence check).

### Flats

Query: `frame_type='Flat'`, `camera`, `ota`, `filter`, `capture_date` within
±1 day of session `obs_date`. Since Flat `folder_path` values include the
capture date as a subdirectory (e.g.
`00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Pro/2026-02-20`), all `*.fit`
files in the resolved folder are the correct files.

**Resolution:**
- 0 matches → prompt:
  ```
  No flats found for L-Pro within ±1 day of 2026-02-19.
  [Enter] Proceed without flats
  ```
- 1 match → use silently
- 2 matches (both adjacent days have flats) → prompt with options sorted by
  date proximity, default to closest:
  ```
  Multiple flat sets found for L-Pro near 2026-02-19:
    1) 2026-02-19 (20 frames) ← closest
    2) 2026-02-20 (20 frames)
  [1]>
  ```

### FlatDarks

Once flats are resolved, query: `frame_type='FlatDark'`, `camera`,
`exposure_sec` ≈ flat exposure (within 10%), `capture_date` = flat
`capture_date` or flat `capture_date + 1 day`. Files are filtered within
`folder_path` by `parse_datetime()` date matching `cal_set.capture_date`.

If no FlatDarks found, silently skip — WBPP handles their absence.

## File Discovery

| Frame type | Source | Filter |
|---|---|---|
| Lights | `output_root / session.lights_path` | all `*.fit` |
| Flats | `output_root / cal_set.folder_path` | all `*.fit` (folder already date-specific) |
| Darks | `output_root / cal_set.folder_path` | `parse_exposure(stem)` matches session exposure |
| FlatDarks | `output_root / cal_set.folder_path` | `parse_datetime(stem).date()` == `cal_set.capture_date` |

## Output

After creating each session:

```
SESSION_1  (M 81 · 2026-02-19 · L-Pro · 132 lights)
  Lights/FILTER_L-Pro/    132 symlinks
  Darks/                   30 symlinks
  Flats/FILTER_L-Pro/      20 symlinks
  FlatDarks/               20 symlinks

In PixInsight: WBPP → Add Directory → select SESSION_1/
```

Missing calibration warnings appear inline:
```
  Flats/FILTER_L-Pro/       0 symlinks  [no flats found — skipped]
```

## Workflow

```bash
# See what's in the catalog
wbpp_prep.py --list
wbpp_prep.py --list --target "M 81"

# Prep all M 81 nights
wbpp_prep.py --target "M 81" --output ./staging --catalog ../darkroom-catalog/astro_catalog.db

# Prep one specific night
wbpp_prep.py --target "M 81" --date 2026-02-19 --output ./staging --catalog ../darkroom-catalog/astro_catalog.db

# Prep a specific session by ID
wbpp_prep.py --session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro --output ./staging --catalog ../darkroom-catalog/astro_catalog.db
```
