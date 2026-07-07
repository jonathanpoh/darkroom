# darkroom — Development Brief

## Superpowers skills (opt-in on Opus 4.8)

When running **Opus 4.8**, do NOT auto-invoke `test-driven-development`,
`systematic-debugging`, or `verification-before-completion` — 4.8 already does
root-cause investigation, test-first discipline, and verify-before-claiming well
enough on its own, and the gates add friction to exploratory work in this repo.
Invoke them only when I explicitly ask, or when the change is risky enough to
warrant the rigor (e.g. catalog/ingest logic where a silent regression corrupts
the archive). On **Sonnet 4.6** (or any non-4.8 model), treat them as normally
auto-triggering. Orchestration skills (brainstorming, writing/executing-plans,
worktrees, subagent-driven-development) are unaffected.

## What This Project Is

The full darkroom suite — unified into a single `darkroom` CLI as of May 2026.
Previously two repos (`darkroom-catalog` read-only, `darkroom-ingest` write
pipeline); now one package with subcommands:

| Subcommand | Purpose |
|---|---|
| `darkroom catalog scan-lights <path>` | Recursively catalog all light sessions on the NAS |
| `darkroom catalog scan-calibration <path>` | Catalog calibration frames |
| `darkroom catalog mark <id> <state> [--date/--path/--notes]` | Set processed_state (unprocessed/in_progress/processed/skipped) for one session |
| `darkroom catalog scan-processed --archive <path> [--apply]` | Reconcile processed_state from on-disk output artifacts (dry-run by default) |
| `darkroom catalog list [--target X]` | Browse the catalog |
| `darkroom catalog migrate-archive --archive <path> [--dry-run]` | Migrate archive from old filter-in-folder layout to `Lights/<filter>/` |
| `darkroom ingest scan --asiair <path> [--manifest F]` / `ingest review F` / `ingest commit [F]` | Archive an ASIAir session (scan → review → commit) |
| `darkroom wbpp --target X [--date Y \| --session ID] [--flat-window DAYS]` | Build SESSION_N symlink dirs for PixInsight |
| `darkroom finish --target X [--date Y]` | Copy WBPP stacks back to archive and mark sessions processed |
| `darkroom triage scan --archive <path>` | Scan archive for issues, populate triage.db |
| `darkroom triage serve --archive <path>` | Review/fix flagged items in a web UI (port 8002) |

Shared flags and config resolution (CLI → env → `darkroom.toml`, see `darkroom/config.py`):

| Flag | Env var | toml key | Default |
|---|---|---|---|
| `--catalog` | `DARKROOM_CATALOG` | `catalog_path` | `~/.config/darkroom/astro_catalog.db` |
| `--archive` | `DARKROOM_ARCHIVE` | `archive_path` | — (required) |
| `--wbpp` | `DARKROOM_WBPP` | `wbpp_path` | `./WBPP` |
| `--asiair` | — | — | — (required for ingest) |

The toml accepts flat keys or a `[darkroom]` section.

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

Ported from `asiair-ingestion/scripts/create_wbpp_input.py`. Use these everywhere:

- `parse_filter(stem)` — filter from filename (None if absent)
- `parse_exposure(stem)` — exposure string (e.g. `'180.0s'`)
- `parse_datetime(stem)` — capture datetime
- `flat_morning_date(end_dt)` — date flats were taken (same morning if session ended
  before noon; next morning otherwise)
- `ota_from_focallen(focal_length)` — OTA name from FOCALLEN header value
- `fits_files(directory)` — sorted FITS list, thumbnails excluded

## `darkroom ingest` (was `archive_ingest.py`)

### Verbs (subcommands, not mode flags)
- `ingest scan --asiair <path> [--manifest <yaml>]`: scan + emit manifest.
  No `--manifest` prints to stdout (a dry run); `--manifest FILE` writes it.
- `ingest review <yaml>`: interactively resolve `needs_review` (missing-filter) items.
- `ingest commit [<yaml>]`: execute a manifest. With no FILE, scans `--asiair` and
  commits in one step.
- Shared: `--archive <path>` (env `DARKROOM_ARCHIVE`), `--catalog <path>` on
  `scan`/`commit`.

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

### Manifest YAML structure

```yaml
meta:
  asiair: /Volumes/ASIAIR/Autorun/
  archive: ~/02_Astrophotography/02_Archive
  catalog: ~/.config/darkroom/astro_catalog.db
  generated: 2026-02-19T21:00:00
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

calibration:
  - frame_type: Flat
    ...
```

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
| Flats | OTA + Camera + Filter + nearest date within ±N days (N = `--flat-window`, default 3) |
| Flat darks | Flat exposure + flat date (or flat_date + 1 fallback) |

### Inputs
- `--target "M 81"` + `--date 2026-02-19` (looks up session in catalog)
- `--session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro` (direct session ID)
- `--wbpp <path>`: where to create SESSION_N dirs (env: `DARKROOM_WBPP`, default: `./WBPP`)
- `--archive <path>`: NAS/local archive root (env: `DARKROOM_ARCHIVE`)

### Output structure
```
<wbpp>/<TargetSlug>/
  SESSION_N/        ← symlinks into archive
    Lights/
    Darks/
    Flats/
    FlatDarks/
  Output/           ← set this as WBPP output dir in PixInsight
    processed/      ← pre-created
```

## Running

```bash
cd /Users/jpoh/Projects/darkroom
uv sync --extra dev
uv run darkroom --help
uv run darkroom catalog list
uv run pytest                            # run the suite
```

## Package Layout

```
darkroom/
  cli.py            ← entry point (argparse dispatch)
  config.py         ← shared CLI/env/toml path resolution; resolve_catalog() defaults to ~/.config/darkroom/astro_catalog.db
  cataloger.py      ← FITS header extraction, scan-all/calibration logic, DB schema, upsert/mark fns
                       (also a LEGACY finish_command — NOT the live finish; see note below)
  catalog.py        ← read-only query helpers (find_darks, find_flats, find_flat_darks, query_sessions)
  catalog_cli.py    ← subparser tree for `darkroom catalog ...`
  parse.py          ← filename parsing (parse_filter, parse_exposure, parse_datetime, fits_files)
  scanner.py        ← scan_source — produces Session/CalibrationGroup dataclasses from ASIAir source folder
  ingest.py         ← `darkroom ingest`
  prep.py           ← `darkroom wbpp`
  finish.py         ← `darkroom finish` (THE live finish — edit here)
  wbpp.py           ← symlink helpers used by prep/finish
  triage/           ← `darkroom triage` — archive-cleanup web UI
    scanner.py      ← walk archive, flag issues (checks.py: OBJECT, RA/DEC)
    suggest.py      ← propose corrected paths/values
    actions.py      ← move/rename/copy_corrected/trash/revert
    db.py           ← triage_items table (separate triage.db, NOT the catalog)
    server.py       ← FastAPI app; cli.py registers `triage scan|serve`
  templates/triage/ ← Jinja2 templates for the triage UI
```

> **Two `finish` implementations — don't edit the wrong one.** `darkroom finish`
> dispatches (via `cli.py` → `finish.add_subparser`) to **`finish.py:cmd_finish`** —
> this is the live command: it copies WBPP `master/`+`processed/` to the archive and
> marks each resolved session processed (folder name via `_target_slug`).
> `cataloger.py` *also* has a separate `finish_command` with its own argparse,
> reachable only via `python -m darkroom.cataloger finish` — it is legacy and NOT what
> users run (it builds the archive path via `_normalize_target` instead). Changing
> `cataloger.py:finish_command` has **no effect** on `darkroom finish`. Default to
> `finish.py` when touching finish behaviour.

## `darkroom triage` (transient cleanup tool)

A web UI for cleaning up the **existing** NAS archive — not part of the steady-state
ingest pipeline. `triage scan` walks the archive, flags problems (placeholder FITS
`OBJECT`, RA/DEC mismatches, mis-filed calibration, legacy session naming), and
proposes corrections; `triage serve` (port 8002) lets you review each item and
apply move/rename/copy-corrected/trash, or revert a prior action.

State lives in a **separate `triage.db`** (default `<archive>/triage.db`), distinct
from `astro_catalog.db` — triage does not write to the catalog, so the "catalog is
the single source of truth" rule still holds. This tool is expected to be removed
once the archive backlog is cleaned up; treat it as scaffolding, not core.

## Relationship to `asiair-ingestion`

`asiair-ingestion` is a **data repository** for the Feb 2026 imaging run. Its
`scripts/create_wbpp_input.py` was the original prototype — now superseded by this
package. Treat it as a historical reference only.

## Catalog integration

`darkroom ingest commit` calls `upsert_session`/`upsert_calibration_set` from
`darkroom.cataloger` directly — no shell-out, no manual SQL. `darkroom finish`
calls `mark_processed` for every session_id resolved from the WBPP target's
SESSION_N symlinks. The catalog is the single source of truth.
