# darkroom — Development Brief

## What This Project Is

The full darkroom suite — unified into a single `darkroom` CLI as of May 2026.
Previously two repos (`darkroom-catalog` read-only, `darkroom-ingest` write
pipeline); now one package. Run `darkroom --help` for the subcommand tree, and
see `darkroom/config.py` for shared path resolution (CLI → env → `darkroom.toml`,
which accepts flat keys or a `[darkroom]` section).

## Pipeline Context

```
ASIAir SD card
      │
      ▼
[CCC copies to Mac]  ← Carbon Copy Cloner, triggered on SD mount (no TTY)
      │
      ▼
darkroom ingest ──→ NAS: 01_Deep Sky Objects/<Target>/<Session>/Lights/
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
      │          ~/WBPP/<Target>/Output/  (created empty, set as WBPP output dir)
      │            processed/             (pre-created)
      ▼
  PixInsight WBPP → Output/master/*.xisf + Output/processed/*.xisf
      │
      ▼
darkroom finish ──→ NAS: 01_Deep Sky Objects/<Target>/_Processed/<date>/
                    └─→ marks every session_id under that WBPP target as processed
```

## Key Constraints

- **CCC postflight = no TTY**: the postflight runs `ingest scan --manifest` only.
  `ingest review` is the interactive step (refuses without a TTY) and `ingest
  commit` is run deliberately afterwards; `commit` itself never prompts.
- **Manifest identity fields are derived, and commit does not re-derive them**:
  `session_id`, `set_id`, `lights_rel_path`, `folder_rel_path` and every file
  `dst` come from target/obs_date/OTA/camera/filter. Editing one of those inputs
  without recomputing the rest leaves the catalog row disagreeing with the
  folder layout — silently. `darkroom/ingest_review.py:recompute_entry` is the
  only correct way to apply such an edit; never hand-edit a manifest.
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
01_Deep Sky Objects/
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

Ported from `asiair-ingestion/scripts/create_wbpp_input.py`. Use these helpers
everywhere — never re-implement filename parsing, filter/exposure extraction, or
the flat-morning date rule inline.

## `darkroom ingest` (was `archive_ingest.py`)

### Workflow
1. Scan source for FITS files; extract metadata from filenames + headers.
2. Group light frames into sessions by **imaging night** (local noon-to-noon):
   each frame's night is the local calendar date the night began, so a run
   spanning midnight stays one session. Sessions are keyed by (target, night).
   See `cataloger.py:compute_imaging_night`.
3. Separate frame types: Light, Dark, Flat, FlatDark.
4. Compute canonical destination paths for each group.
5. Write YAML manifest listing every source→destination move.
6. In `--dry-run` or first pass: print/save manifest, stop.
7. In `--commit` pass: execute copies, then register in `astro_catalog.db`.

## `darkroom wbpp` (was `wbpp_prep.py`)

Generalised from `asiair-ingestion/scripts/create_wbpp_input.py`. Key differences:

- Source is the **NAS archive**, not a local `Autorun/` folder.
- Sessions identified by catalog ID or `--target` + `--date`.
- Flat matching uses **date proximity** (±3 days default, `--flat-window DAYS`), not
  exact date — because archived flats may have been taken on a different occasion
  than the session.
- Produces WBPP session dirs in `~/WBPP/<TargetSlug>/SESSION_N/` with symlinks.

### Matching rules (inherit from prototype, adjust as needed)

| Frame type | Match key |
|---|---|
| Science darks | Camera + Gain + Exposure (all dates usable) |
| Flats | OTA + Camera + Filter, within ±N days (N = `--flat-window`, default 3), ranked by the **flat-morning rule** — see below |
| Flat darks | Flat exposure + flat date (or flat_date + 1 fallback) |

#### The flat-morning rule (directional, not proximity)

Flats are shot the morning **after** a session, so a flat set's relationship to
a night is directional: offset `+1` (following morning, the normal workflow) and
`0` (that evening — happens when filters are changed mid-session) are both *this
run*; anything else is a different occasion. `catalog.flat_sort_key` ranks in-run
sets first (`+1` ahead of `0`), then falls back to proximity, preferring the
later date on a tie.

Do not rank flats by absolute date distance. That makes `-1` and `+1` a tie
broken by backend row order, which routinely handed a session the *previous*
night's flats — a different sky, often a very different flat exposure
(0.31s vs 0.05s was observed on real NGC 281 data). `parse.py`'s flat-morning
helpers and `catalog.find_flat_darks` already encoded the directional `0..+1`
window; `find_flats` was the outlier.

## `darkroom finish`

> **`finish.py` is the finish implementation.** `darkroom finish` dispatches
> (via `cli.py` → `finish.add_subparser`) to **`finish.py:cmd_finish`**: it
> copies WBPP `master/`+`processed/` to the archive and marks each resolved
> session processed (folder name via `names.target_slug`). The legacy
> `cataloger.py:finish_command` (reachable only via `python -m darkroom.cataloger
> finish`, and which built archive paths differently via `_normalize_target`)
> has been removed — `finish.py` is the only finish surface now.

## Relationship to `asiair-ingestion`

`asiair-ingestion` is a **data repository** for the Feb 2026 imaging run. Its
`scripts/create_wbpp_input.py` was the original prototype — now superseded by this
package. Treat it as a historical reference only.

## Catalog integration

`darkroom ingest commit` calls `upsert_session`/`upsert_calibration_set` from
`darkroom.cataloger` directly — no shell-out, no manual SQL. `darkroom finish`
calls `set_processed_state` (via the resolved backend) for every session_id
resolved from the WBPP target's SESSION_N symlinks. The catalog is the single
source of truth.
