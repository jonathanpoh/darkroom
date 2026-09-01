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
darkroom ingest ──→ Archive (SSD): 01_Deep Sky Objects/<Target>/<Session>/Lights/
      │                            00_Calibration/Flats/<OTA_Camera_Filter>/<Date>/
      │                            00_Calibration/Darks/<Camera>/
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
darkroom finish ──→ Archive (SSD): 01_Deep Sky Objects/<Target>/_Processed/<date>/
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
- **OTA from FOCALLEN header + session date**: `180 → FMA180`, `400 → FRA400`,
  `100/135/200/300/400 → Canon<focal>mm`, `45-60 → Canon50mm`. See
  `parse.py:parse_ota()`. Focal length alone is ambiguous — the Canon EF 100-400
  zoom sits at the same focal lengths as the scopes — so `parse_ota` also takes
  `obs_date` and falls through to the lens when the session predates the scope
  (`OTA_ACQUIRED`: FMA180 Jan 2023, FRA400 Jan 2025). Each zoom stop is its own
  OTA name, because flat matching keys on OTA and a 100mm flat must never match
  a 400mm light.
- **Session date = start date**: local calendar date the session began (before midnight),
  not the date it ended (sessions routinely run past midnight).

## Archive Structure (canonical)

Root: `/Volumes/Photography 4TB/Astrophotography/` — a **Thunderbolt 4 attached
SSD** on the Mac, configured as `archive_path` in `darkroom.toml` (or
`--archive` / `DARKROOM_ARCHIVE`). Every darkroom command reads and writes
the archive here, as fast local storage.

**darkroom never touches the NAS.** A separate backup task replicates the
SSD onto the Synology; that copy is a backup, not a second archive, and no
command reads from or writes to it. Two consequences for code review:

- Do not justify I/O refactors on network round-trip cost ("one stat per
  frame over SMB"). Directory listings and per-file stats are cheap here;
  a scan's cost is opening FITS headers, not walking folders.
- The webapi host (an LXC) has **no** mount of the archive at all. Anything
  that has to move files on disk (U2's `pending_renames` ledger, `darkroom
  catalog apply-renames`) runs on the Mac, never on the server.

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

- Source is the **archive** (the SSD), not a local `Autorun/` folder.
- Sessions identified by catalog ID or `--target` + `--date`.
- Flat matching uses **date proximity** (±3 days default, `--flat-window DAYS`), not
  exact date — because archived flats may have been taken on a different occasion
  than the session.
- Produces WBPP session dirs in `~/WBPP/<TargetSlug>/SESSION_N/` with symlinks.

### Matching rules (inherit from prototype, adjust as needed)

| Frame type | Match key |
|---|---|
| Science darks | Camera + Gain + Exposure + Temperature within ±N °C (N = `--dark-temp-tolerance`, default 3; all dates usable). Single nearest master is symlinked; NULL-temp sets pass as lowest-priority fallback |
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

## `darkroom logs` + guide-log stats (F4)

ASIAir logs live on the SD card, which gets rotated and cleared, so they are
archived before anything reads them:

```
darkroom logs import [--source DIR] [--apply]   # -> <archive>/00_Logs/ASIAir/
```

Copies `Autorun_Log_*.txt` and `PHD2_GuideLog_*.txt`, skipping `*_CHN.txt`
(Chinese translations of identical content) and names already archived at the
same size. Dry run by default; never touches the source. Autorun logs are
archived but not parsed — only the PHD2 guide logs are.

Two catalog passes then turn those into per-session guiding quality:

```
darkroom catalog backfill-times [--archive P] [--apply]
darkroom catalog scan-guiding   [--logs DIR] [--settle-exclude SEC] [--apply]
```

- `backfill-times` fills `sessions.start_utc`/`end_utc` — the session's UTC
  wall-clock span — from `DATE-OBS`/`EXPTIME`. Only frames whose imaging night
  (`cataloger.compute_imaging_night`) equals the session's `obs_date` count: a
  `lights_path` folder can hold several nights, and legacy layouts have two
  session rows sharing one folder. Ingest populates the span for free
  (`scanner.py` already groups by night), so this is a one-off for old rows.
- `scan-guiding` parses every guide log, intersects its guiding segments with
  each session's span, and writes one `session_guiding` row per session
  (`guidescan.py` → `catalog_client.upsert_session_guiding`). `--settle-exclude`
  (default 15s) is how much post-dither settling is discarded; leave it alone
  unless you are deliberately probing the sensitivity, since the stored numbers
  are only comparable across sessions at one setting.

### Guide logs are matched by TIME ONLY, never by target name

The logs carry target names. **Do not use them.** They are messy (`NGC7000` vs
`NGC 7000`, 147 `FOV` framing blocks) and, worse, sometimes simply wrong: the
name recorded at acquisition is whatever was typed then, while filenames, FITS
headers and the catalog were corrected afterwards. The catalog is the corrected
truth; a log is trusted only for *when* it was guiding. Time-span intersection
is the entire matching strategy.

Two consequences worth knowing:

- **Window filtering is what makes the numbers mean anything.** Whole-log RMS is
  garbage — framing, plate-solving and slews sit inside "guiding" segments
  (IC 5070 2026-07-10: 84.39" whole-log vs **0.97"** windowed).
- **Coverage is the honest guard.** `coverage` = guided seconds ÷ session wall
  span. Anything under 0.8 means a partial log, and the UI says so rather than
  letting it look authoritative.
- **`rms_total >= 2 * p95` is spike-dominated**, and the UI appends a dim ▲ to
  the RMS (value and colour band unchanged, `ui.py:_is_spike_dominated`): the
  total is carried by a few wrecked subs, not a bad night (NGC 6888 2026-07-20,
  rms 19.18" / p95 2.11"), whereas a uniformly bad night sits near 1.2× and
  keeps reading bad.

A whole date range matching nothing means the ASIAir clock/timezone wasn't
`guidelog.LOCAL_TZ`. `scan-guiding` **reports** that (unmatched logs and
unmatched sessions, both ways) and never auto-corrects it.

Settle failures are normal on this rig (`Settling failed` outnumbers `Settling
complete` 10,706 : 1,350) — they are excluded statistically, never surfaced as
an alarm.

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
