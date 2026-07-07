# darkroom

Personal astrophotography pipeline — catalog, archive, prep for stacking, and
finish. One CLI, one SQLite database, one venv.

```
ASIAir SD ──[CCC]──▶ Mac staging ──[darkroom ingest]──▶ NAS archive
                                                            │
                                                            ▼
                                                       astro_catalog.db
                                                            │
                              [darkroom wbpp]   ◀───────────┘
                                     │
                                     ▼
                              ~/WBPP/<target>/SESSION_N/  (symlinks)
                                     │
                              [PixInsight WBPP]
                                     │
                                     ▼
                              [darkroom finish] ──▶ NAS _Processed/<date>/
                                     │
                                     └─▶ mark sessions processed in catalog
```

## Install

```bash
git clone <repo> darkroom
cd darkroom
uv sync --extra dev
cp darkroom.toml.example darkroom.toml   # then edit paths
```

The `darkroom` console script is registered via `[project.scripts]`. Run it as
`uv run darkroom …` (or activate the venv).

## Configuration

Every subcommand needs a catalog path; ingest/finish also need an archive root.
Resolution order: CLI flag → env var → `darkroom.toml`.

| Setting | CLI flag | Env var | toml key |
|---|---|---|---|
| Catalog DB | `--catalog` | `DARKROOM_CATALOG` | `catalog_path` |
| Archive root | `--archive` | `DARKROOM_ARCHIVE` | `archive_path` |
| WBPP work dir | `--wbpp` | `DARKROOM_WBPP` | `wbpp_path` |

See [`darkroom.toml.example`](darkroom.toml.example).

## Subcommands

### `darkroom catalog`

```bash
darkroom catalog scan-lights "/Volumes/Astrophotography/01_Deep Sky Objects"
darkroom catalog scan-calibration "/Volumes/Astrophotography/00_Calibration"
darkroom catalog list [--target "M 81"]
darkroom catalog mark <session_id> <state> [--date Y] [--path P] [--notes T]
darkroom catalog scan-processed --archive <path> [--apply]
```

Processing status is a structured enum `processed_state`, one of:
`unprocessed`, `in_progress` (stacked and/or editing, no final export yet),
`processed`, `skipped` (deliberately set aside). `catalog mark` sets it by hand
(`<state>` is validated); `--date`/`--path`/`--notes` attach a processing date,
output path, and free-text note. `darkroom finish` sets it automatically.

`catalog scan-processed` reconciles the catalog to what's actually on disk:
it walks the archive for output artifacts and proposes a `processed_state` per
session — an export (`.tif/.jpg/.png/.psd`) → `processed`, a `.xisf`/WBPP master
→ `in_progress`, only subs → `unprocessed`. Where PixInsight WBPP logs exist it
uses their frame lists for **exact** night→edit attribution; otherwise it falls
back to a date-bound heuristic (an edit dated on/after a night covers it; newer
nights stay unprocessed). It is a **dry run by default** (prints proposed
changes, tagged `[log …]` vs `[date-bound …]`); `--apply` writes them,
monotonically (only upgrades, never downgrades or touches `skipped`). Read-only
on the archive.

Catalog write rules:
- Camera names are normalized (whitespace stripped) at the upsert layer —
  `"ZWO ASI585MC Pro"` → `ZWOASI585MCPro`.
- `exposure_sec` is rounded to 4 decimals (avoids FITS float noise).
- `processed_state` (+ `processed_path`/`processed_date`/`notes`) is preserved on
  re-scan (upsert does not overwrite it).
- `filter` is `NULL` when absent/unknown (`NoFilter`/`UnknownFilter` are
  deliberate values, not "empty").
- Synology `@eaDir/` metadata folders are skipped automatically.
- `Dark` frames with exposure < 10s are reclassified as `FlatDark` (ASIAir
  writes them into the same folder).

Browse the catalog in the web UI served by `darkroom.webapi`
(`uvicorn --factory darkroom.webapi.app:create_app_from_env`).

### `darkroom ingest`

```bash
# Three-step (recommended): scan → review → commit
darkroom ingest scan --asiair ~/staging/Autorun --manifest /tmp/ingest.yaml
darkroom ingest review /tmp/ingest.yaml      # only if filter detection failed
darkroom ingest commit /tmp/ingest.yaml

# Or one-shot:
darkroom ingest commit --asiair ~/staging/Autorun
```

- Designed to run non-interactively after Carbon Copy Cloner finishes (no TTY).
- Detects session boundaries, filters, OTA from FOCALLEN; writes to the
  canonical archive layout; refuses to commit if any `needs_review` items
  remain. Re-running on the same source detects already-archived sessions and
  becomes a no-op (or a top-up if new frames are present).
- Writes to the catalog atomically with the file copy.

### `darkroom wbpp`

```bash
darkroom wbpp --list                                  # browse what's in the catalog
darkroom wbpp --target "M 81"                         # all nights, all filters
darkroom wbpp --target "M 81" --date 2026-02-19       # one night
darkroom wbpp --session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro
darkroom wbpp --target "M 81" --overwrite             # clear ./WBPP/M81/ first
```

For each imaging night the session covers, builds:

```
./WBPP/<target_slug>/SESSION_N/
  Lights/FILTER_<name>/   ← symlinks to NAS light frames
  Darks/                  ← science darks matched by camera/gain/exposure
  Flats/FILTER_<name>/    ← flats nearest to obs_date (±1 day)
  FlatDarks/              ← flat darks matching flat exposure/date (or date+1)
```

Then in PixInsight: WBPP → Add Directory → select the `SESSION_N/` folder.

### `darkroom finish`

After WBPP produces `master/*.xisf` (and you've optionally hand-finished into
`processed/`):

```bash
darkroom finish --target "M 81"                      # auto: max mtime across master/+processed/
darkroom finish --target "M 81" --date 2026-05-15    # override
darkroom finish --target "M 81" --dry-run            # preview
```

Copies stacks to `<output>/01_Deep Sky Objects/<target>/_Processed/<date>/`,
then walks each `SESSION_N/`'s light symlinks back to the catalog to set every
contributing session's `processed_state = processed` (recording the
`_Processed/<date>` path and date). Then prompts to delete `SESSION_N/`,
`calibrated/`, `debayered/`, `master/`, `processed/` working dirs.

## Package layout

```
darkroom/
  cli.py            entry point — argparse dispatch
  config.py         shared --flag / env / toml resolution
  cataloger.py      FITS extraction, scan-all/scan-calibration logic, DB schema, upsert/mark
  catalog.py        read-only query helpers (find_darks, find_flats, …)
  catalog_db.py     write/query API for the future web UI (open_db, query/count/update)
  catalog_cli.py    `darkroom catalog …` subparser tree
  names.py          stdlib-only name/coord helpers (make_session_id, normalize, …)
  parse.py          filename parsing (parse_filter, parse_exposure, parse_datetime, …)
  scanner.py        scan_source — produces Session/CalibrationGroup dataclasses
  ingest.py         `darkroom ingest`
  prep.py           `darkroom wbpp`
  picker.py         interactive session picker for `darkroom wbpp`
  finish.py         `darkroom finish`
  procscan.py       `darkroom catalog scan-processed` — reconcile processed_state from disk
  wbpplog.py        parse PixInsight WBPP logs for exact session→edit attribution
  wbpp.py           symlink helpers used by prep/finish
```

## Tests

```bash
uv run pytest
```

459 tests covering scanners, parsers, DB upsert/queries/migrations, the
catalog write API, ingest manifest builders, WBPP symlink discovery, the
interactive picker, processed-state reconciliation, and finish-side helpers.
The new CLI dispatcher itself (`darkroom/cli.py`) is not yet covered by tests
— if you change parsing, smoke-test with `uv run darkroom <subcmd> --help`.

## Canonical naming

| Component | Form | Examples |
|---|---|---|
| OTA | abbrev + model | `FMA180`, `FRA400` |
| Camera | brand + model, no spaces | `ZWOASI585MCPro`, `Canon6D` |
| Filter | hyphenated where the product is | `L-Pro`, `L-Extreme`, `NoFilter` |
| Gain (ZWO) | lowercase | `gain200` |
| ISO (Canon) | uppercase | `ISO800` |
| Temperature | sign + number + `C` | `-20C`, `15C` |
| Exposure | number + `s` | `180s`, `2s` |
| Date | ISO 8601, local night-start date | `2026-02-19` |
| Separators | `_` between components, `-` within | `FRA400_ZWOASI585MCPro_L-Pro` |
| Session ID | `{Target}_{YYYYMMDD}_{OTA}_{Camera}_{Filter}` | `M81_20260219_FRA400_ZWOASI585MCPro_L-Pro` |
