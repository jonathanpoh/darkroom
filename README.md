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
darkroom catalog scan-lights "/Volumes/Astrophotography/04_Deep Sky Objects"
darkroom catalog scan-calibration "/Volumes/Astrophotography/00_Calibration"
darkroom catalog list [--target "M 81"]
darkroom catalog mark <session_id> "2026-05-15"
```

Catalog write rules:
- Camera names are normalized (whitespace stripped) at the upsert layer —
  `"ZWO ASI585MC Pro"` → `ZWOASI585MCPro`.
- `exposure_sec` is rounded to 4 decimals (avoids FITS float noise).
- `processed_status` is preserved on re-scan (upsert does not overwrite it).
- Synology `@eaDir/` metadata folders are skipped automatically.
- `Dark` frames with exposure < 10s are reclassified as `FlatDark` (ASIAir
  writes them into the same folder).

Browse the DB with Datasette:

```bash
uv run datasette serve astro_catalog.db
```

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

Copies stacks to `<output>/04_Deep Sky Objects/<target>/_Processed/<date>/`,
then walks each `SESSION_N/`'s light symlinks back to the catalog to mark every
contributing `session_id` as processed with the `_Processed/<date>` path. Then
prompts to delete `SESSION_N/`, `calibrated/`, `debayered/`, `master/`,
`processed/` working dirs.

## Package layout

```
darkroom/
  cli.py            entry point — argparse dispatch
  config.py         shared --flag / env / toml resolution
  cataloger.py      FITS extraction, scan-all/scan-calibration logic, DB schema, upsert/mark
  catalog.py        read-only query helpers (find_darks, find_flats, …)
  catalog_cli.py    `darkroom catalog …` subparser tree
  parse.py          filename parsing (parse_filter, parse_exposure, parse_datetime, …)
  scanner.py        scan_source — produces Session/CalibrationGroup dataclasses
  ingest.py         `darkroom ingest`
  prep.py           `darkroom wbpp`
  finish.py         `darkroom finish`
  wbpp.py           symlink helpers used by prep/finish
```

## Tests

```bash
uv run pytest
```

168 tests covering scanners, parsers, DB upsert/queries, ingest manifest
builders, WBPP symlink discovery, and finish-side date/copy/cleanup helpers.
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
