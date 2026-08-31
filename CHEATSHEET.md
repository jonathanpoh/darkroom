# darkroom — Command Cheatsheet

Quick reference for the `darkroom` CLI. For the full design brief see `CLAUDE.md`;
for what's currently waiting on you, see `BLOCKERS.md`.

```
SD card ──ingest──▶ NAS archive ──wbpp──▶ ~/WBPP/SESSION_N (symlinks)
                        ▲                        │
                        │                   PixInsight WBPP
                        │                        │
                        └────────finish──────────┘   (stacks back to archive + mark processed)

ASIAir logs ──logs import──▶ NAS 00_Logs ──backfill-times──▶ scan-guiding ──▶ guiding stats

catalog = the source of truth (astro_catalog.db).  triage = one-off archive cleanup.
```

Run locally with `uv run darkroom <cmd>` (or just `darkroom` if installed globally).

---

## Shared config resolution

Most path flags resolve in this order: **CLI flag → env var → `darkroom.toml` → default**.
Set the env vars once in your shell and you can omit the flags everywhere.

| Flag | Env var | `darkroom.toml` key | Default |
|---|---|---|---|
| `--catalog` | `DARKROOM_CATALOG` | `catalog_path` | `~/.config/darkroom/astro_catalog.db` |
| `--archive` | `DARKROOM_ARCHIVE` | `archive_path` | — (required) |
| `--wbpp` | `DARKROOM_WBPP` | `wbpp_path` | `./WBPP` |
| `--asiair` | — | — | — (required for ingest) |

`darkroom.toml` accepts flat keys or a `[darkroom]` section.

```bash
export DARKROOM_ARCHIVE="/Volumes/Astrophotography"
export DARKROOM_CATALOG="$HOME/.config/darkroom/astro_catalog.db"
export DARKROOM_WBPP="$HOME/WBPP"
```

> **Target names** are forgiving on input — `m81`, `M81`, `M 81` all resolve to the
> canonical `M 81`; `SH2-103`/`Sh 2-103` → `Sh2-103`. Spacing and case are normalised
> for you, so type whatever's convenient.

---

## The everyday pipeline

### 1. `darkroom ingest` — archive a fresh ASIAir session

Copies FITS off the SD-card copy into the canonical NAS layout and registers the
new sessions + calibration sets in the catalog. **Never deletes source files.**

Designed to be non-interactive (CCC postflight has no TTY): generate a manifest,
review it, then commit.

It has three verbs: **scan** → **review** → **commit**.

```bash
# 1. Scan and print the manifest to stdout, write nothing (a dry run)
darkroom ingest scan --asiair /Volumes/ASIAIR/Autorun

# 2. Scan and write the manifest to a file for later review
darkroom ingest scan --asiair /Volumes/ASIAIR/Autorun --manifest run.yaml

# 3. Confirm/correct the parsed values (target, filter, OTA+camera) interactively
darkroom ingest review run.yaml
darkroom ingest review run.yaml --flagged-only   # only entries missing a filter

# 4. Commit the reviewed manifest (copies files + writes catalog)
darkroom ingest commit run.yaml

#    …or scan + commit in one shot (no separate manifest file)
darkroom ingest commit --asiair /Volumes/ASIAIR/Autorun
```

| Verb | Use |
|---|---|
| `scan --asiair PATH [--manifest FILE]` | Scan the source; print the manifest (or write it to FILE with `--manifest`). No `--manifest` = dry run. |
| `review FILE` | Walk FILE and confirm/correct target, filter and OTA+camera per entry. Needs a TTY. |
| `review --flagged-only` | Visit only the entries missing a filter (the old behaviour). |
| `commit [FILE]` | Execute FILE; with no FILE, scan `--asiair` + commit directly. |
| `--archive`, `--catalog` | (on `scan`/`commit`) Override resolved paths. `--catalog` is the **offline fallback only** — a configured `catalog_url`/`DARKROOM_CATALOG_URL` wins for all three verbs. |

> The manifest is always **YAML**. `--manifest run` (no extension) writes `run.yaml`;
> a `.json` name still gets YAML content and prints a warning.

#### ⚠️ Fix the manifest with `review`, never in a text editor

`session_id`, `lights_rel_path` and every file's `dst` are **derived** from
target + obs_date + OTA + camera + filter — and **nothing re-derives them at
commit**. Editing an identity field by hand desyncs the catalog row from the
folder layout, silently and with no warning:

```yaml
# hand-edited: filter changed, needs_review cleared, nothing else touched
filter: L-Extreme                                          # ← what you changed
session_id: M81_20260621_FRA400_ZWOASI585MCPro_UnknownFilter   # ← still stale
lights_rel_path: .../Lights/NoFilter                           # ← still stale
```

That commits happily and leaves the catalog claiming `L-Extreme` while every
path on disk says `NoFilter` — the exact split-brain state the U2 rename ledger
had to clean up. `ingest review` recomputes all three (plus the per-file copy
plan and the new/existing/topup verdict), which is the whole reason it exists.

**Safe to hand-edit:** `notes`, and a file's `copy` flag.
**Never by hand:** `target`, `filter`, `ota`, `camera`, `obs_date`, `panel`,
`session_id`, `set_id`, `lights_rel_path`, `folder_rel_path`, any `dst`.

#### CCC postflight

The postflight has no TTY, so it can only get as far as the manifest — `review`
refuses without a terminal (exit 1) and `commit` refuses any entry still missing
a filter. Set every path and the catalog **explicitly in the script**: CCC may
run it with a different `HOME` than yours, so `~/.config/darkroom/darkroom.toml`
won't necessarily be found.

With the catalog on the server, export `DARKROOM_CATALOG_URL` +
`DARKROOM_API_TOKEN` and **don't** pass `--catalog` — that flag is only the
offline fallback, and pointing it at the dormant local
`~/.config/darkroom/astro_catalog.db` would make every already-archived session
look new.

```bash
#!/bin/zsh
# CCC postflight — scan only; never commits unattended.
set -euo pipefail

OUT=/Users/jpoh/ingest                       # absolute: CCC's $HOME may not be yours
mkdir -p "$OUT"
exec >> "$OUT/ccc.log" 2>&1
echo "=== $(date '+%F %T') postflight ==="

STAGING=/Users/jpoh/staging/Autorun          # CCC's destination, not the SD card
ARCHIVE="/Volumes/Photography 4TB/Astrophotography"

export DARKROOM_CATALOG_URL=https://darkroom.jpoh.net
export DARKROOM_API_TOKEN=...                # same value as darkroom.toml api_token
                                             # keep this script chmod 600

/Users/jpoh/Projects/darkroom/.venv/bin/darkroom ingest scan \
    --asiair  "$STAGING" \
    --archive "$ARCHIVE" \
    --manifest "$OUT/$(date +%F-%H%M).yaml" < /dev/null
```

`scan` resolves its new/existing/topup check through the same backend `commit`
writes to, so with the URL set it asks the server. If the server is unreachable
it still writes the manifest (a scan is read-only and useful offline) but warns
loudly and records `meta.status_verified: false`; `commit` repeats that warning,
since the postflight's warning may only have reached a log nobody read. An
unverified "new" is cheap — already-copied files are skipped by the dst-exists
check, and the upsert preserves `processed_state` and `notes`.

Then, at a real terminal, whenever you get to it:

```bash
darkroom ingest review  ~/ingest/2026-07-29-0830.yaml
darkroom ingest commit  ~/ingest/2026-07-29-0830.yaml
```

Point `--asiair` at CCC's **destination on the Mac**, not `/Volumes/ASIAIR/`:
the manifest records absolute `src` paths, so ejecting the card before you
commit would break every copy.

Sessions are grouped by **imaging night** (local noon-to-noon), so a run that crosses
midnight stays one session, dated to the night it began.

#### Mosaic panels

The ASIAir writes one folder per panel (`M 8_1-1` … `M 8_4-2`). Ingest splits
that into the base target `M 8` plus a `panel` of `1-1`, so the object stays
one target and each panel-night gets its own session row:

```
M8_20260813_Canon50mm_ZWOASI585MCPro_AstronomikL2_P1-1
01_Deep Sky Objects/M 8/2026-08-13_Canon50mm_ZWOASI585MCPro/Lights/AstronomikL2/P1-1/
```

`review` shows a `Panel` line when one is set and offers **Change mosaic
panel** (blank it to make the session ordinary). `panel` is an identity field,
so editing it recomputes the session_id and the destination like any other.

> `wbpp` is **not** panel-aware yet — it would stack non-overlapping panels
> together, which cannot register. Prep mosaics by hand until that lands.

---

### 2. `darkroom wbpp` — build a WBPP symlink session for PixInsight

Reads the archive + catalog and creates `~/WBPP/<TargetSlug>/SESSION_N/` dirs full of
symlinks (Lights / Darks / Flats / FlatDarks), plus an empty `Output/` to point WBPP at.
One `SESSION_N` per imaging night.

```bash
# Browse what's in the catalog first
darkroom wbpp --list
darkroom wbpp --list --target "M 81"

# Prep every night for a target
darkroom wbpp --target "M 81"

# Prep a single night
darkroom wbpp --target "M 81" --date 2026-02-19

# Prep one exact session by catalog ID
darkroom wbpp --session M81_20260219_FRA400_ZWOASI585MCPro_L-Pro

# Widen/narrow flat matching (default ±3 days)
darkroom wbpp --target "M 81" --flat-window 5

# Rebuild from scratch (clears existing SESSION_N dirs first)
darkroom wbpp --target "M 81" --overwrite
```

| Flag | Use |
|---|---|
| `--list` | List catalog sessions instead of building (optionally `--target`). |
| `--target NAME` | Target to prep (canonicalised; quote names with spaces). |
| `--date YYYY-MM-DD` | Restrict `--target` to one night. |
| `--session ID` | Build one exact session by catalog ID. |
| `--flat-window DAYS` | Match flats within ±DAYS of the session (default 3). |
| `--overwrite` | Clear existing `SESSION_N` dirs before regenerating. Prompts if real (non-symlink) files are present. |
| `--archive`, `--catalog`, `--wbpp` | Override resolved paths. |

**Matching rules:** darks by Camera+Gain+Exposure · flats by OTA+Camera+Filter within
±`--flat-window` days (nearest wins) · flat-darks by flat exposure ±10% on the flat's
date (or +1). If multiple flat sets match, you're prompted (or the closest is auto-picked
when there's no TTY).

After it runs: in PixInsight, add each `SESSION_N/` dir to WBPP and set the output dir to
the printed `Output/` path.

**WBPP grouping keywords.** The directory layout only separates frames if WBPP is
configured to group on it. `SESSION` is the normal one. For a **mosaic**, add
`PANEL` as well — WBPP supports nested keywords and will list frames as
`SESSION 1 : PANEL 1-1`. Dropping `SESSION` merges flats across nights *and*
merges the mosaic panels into one stack, silently and with no error, so the
keyword set matters as much as the tree.

> Note: calibration is deliberately **not** panel-split — one
> `Flats/FILTER_<name>/` set serves every panel of that night, and WBPP matches
> it to all of them. Don't add `PANEL_` dirs under `Flats/`, `Darks/` or
> `FlatDarks/`.

---

### 3. `darkroom finish` — push stacks back and mark processed

After WBPP/PixInsight, copies `Output/master/` + `Output/processed/` into
`<archive>/01_Deep Sky Objects/<target>/_Processed/<date>/` and marks every session under
that WBPP target as processed in the catalog. Then offers to clean up the working dirs.

```bash
# Auto-derives the date from the WBPP output's mtimes
darkroom finish --target "M 81"

# Pin the processed-date folder name explicitly
darkroom finish --target "M 81" --date 2026-05-15

# See what would be copied/deleted without doing it
darkroom finish --target "M 81" --dry-run
```

| Flag | Use |
|---|---|
| `--target NAME` | Required. The WBPP target to finish. |
| `--date YYYY-MM-DD` | Name the `_Processed/<date>/` output folder (default: derived from WBPP mtimes). **Not** a night selector — finish always processes the whole WBPP target. |
| `--dry-run` | Print copies/deletes, change nothing. |
| `--archive`, `--catalog`, `--wbpp` | Override resolved paths. |

---

## Catalog management — `darkroom catalog ...`

The catalog is the source of truth. `ingest` writes to it automatically; these commands
are for backfilling, browsing, and manual edits.

> Note: `--catalog` goes **after** the subcommand, like everywhere else:
> `darkroom catalog list --catalog /path/to.db`.

```bash
# Backfill the catalog by scanning the NAS (idempotent upserts)
darkroom catalog scan-lights "/Volumes/Astrophotography/01_Deep Sky Objects"
darkroom catalog scan-calibration "/Volumes/Astrophotography/00_Calibration"

# Browse
darkroom catalog list
darkroom catalog list --target "M 81"

# Manually set one session's processed_state (enum: unprocessed | in_progress | processed | skipped)
darkroom catalog mark M81_20260219_FRA400_ZWOASI585MCPro_L-Pro processed --date 2026-05-15
darkroom catalog mark <id> skipped --notes "bad tracking"

# Reconcile processed_state from what's on disk (dry run; add --apply to write)
darkroom catalog scan-processed --archive "/Volumes/Astrophotography"
darkroom catalog scan-processed --archive "/Volumes/Astrophotography" --apply
```

`processed_state` is a structured enum: `unprocessed` → `in_progress` (stacked
and/or mid-edit, no final export) → `processed`, plus `skipped` (deliberately
set aside). `finish` sets it automatically; `mark` and `scan-processed` set it
manually / from disk.

| Subcommand | When to use |
|---|---|
| `scan-lights <root_path>` | (Re)catalog all light sessions under a folder. Safe to re-run. |
| `scan-calibration <calibration_path>` | (Re)catalog calibration frames (darks/flats/flat-darks/bias). |
| `list [--target NAME]` | Browse sessions, with integration time and processed state. |
| `mark <session_id> <state> [--date/--path/--notes]` | Set one session's `processed_state` by hand (`<state>` validated). |
| `scan-processed --archive PATH [--apply]` | Reconcile `processed_state` from archive artifacts (dry run without `--apply`; only upgrades, never touches `skipped`). Uses PixInsight WBPP logs for **exact** night→edit attribution where they exist (`[log …]`), else a date-bound heuristic (`[date-bound …]`). |
| `rescan-archive --archive PATH [--apply] [--yes]` | Diff the archive against the catalog and queue each divergence as a proposal in the web UI's `/rescan` page. Does not write session rows directly. |
| `apply-renames --archive PATH [--apply]` | Execute the folder moves owed by web-UI identity edits (the server has no NAS mount, so it can only record them). |
| `backfill-times [--archive PATH] [--apply]` | Fill `start_utc`/`end_utc` from DATE-OBS/EXPTIME. Only touches rows where they are **NULL**. |
| `scan-guiding [--logs DIR] [--settle-exclude SEC] [--apply]` | Match archived PHD2 logs to sessions **by time** and store per-session guiding stats. Needs `backfill-times` first. |
| `backfill-sites [--archive PATH] [--apply]` | Fill `site_lat`/`site_lon` from FITS SITELAT/SITELONG. |
| `sites …` | Manage observing sites (used for SQM and home-equivalent hours). |

### Archive ↔ catalog reconciliation

```bash
# What does the disk say that the catalog doesn't? (dry run)
darkroom catalog rescan-archive --archive "$DARKROOM_ARCHIVE"
#   → review the proposals at /rescan in the web UI, then:
darkroom catalog rescan-archive --archive "$DARKROOM_ARCHIVE" --apply

# Drain the rename ledger after editing identity fields in the web UI
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE"          # dry run
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE" --apply
```

Proposal kinds: `update` (frame count/integration changed — e.g. you culled bad
subs), `rename` (an identity field diverged), `create` (folder with no catalog
row), `delete` (row with no folder). They are queued rather than applied
because a `delete` against an unmounted NAS looks exactly like a genuinely
removed session.

**The rename ledger exists because the server has no NAS mount.** Editing an
identity field in the web UI recomputes `session_id` and `lights_path`
immediately, but the folder move is recorded in `pending_renames` for the Mac
to execute. Until you run `apply-renames`, catalog and archive disagree — by
design, not by accident.

### Guiding stats — `logs import` → `backfill-times` → `scan-guiding`

```bash
# 1. Get the logs off the SD card (it gets rotated and cleared)
darkroom logs import --source /Volumes/ASIAIR/log            # dry run
darkroom logs import --source /Volumes/ASIAIR/log --apply

# 2. Give every session a UTC wall-clock span (one-off for legacy rows;
#    ingest populates it for free on new ones)
darkroom catalog backfill-times --apply

# 3. Intersect the guide logs with those spans
darkroom catalog scan-guiding --apply
```

Run them in that order — `scan-guiding` needs `start_utc`/`end_utc` to exist.

- **Guide logs are matched by TIME ONLY, never by target name.** The names in
  the logs are whatever was typed at acquisition and are sometimes wrong; the
  catalog is the corrected truth. A whole date range matching nothing means the
  ASIAir's clock/timezone was off — `scan-guiding` reports that both ways
  (unmatched logs *and* unmatched sessions) and never auto-corrects it.
- **`coverage` is the honest guard.** Guided seconds ÷ session wall span;
  under 0.8 means a partial log and the UI says so.
- **`--settle-exclude` (default 15s)** is how much post-dither settling is
  discarded. Leave it alone — the stored numbers are only comparable across
  sessions at one setting.
- **Re-run `scan-guiding` after changing any session's span.** `backfill-times`
  only fills NULLs and won't revisit a row, so a guiding row derived from an
  old span stays stale silently.
- **No guide log is not an error.** A session shot without the guidescope
  simply has no `session_guiding` row; `scan-guiding` will list it as unmatched
  and that is correct.

### `darkroom catalog migrate-archive` — one-off layout migration

Migrates an old filter-in-folder archive layout to the current `Lights/<filter>/` layout.
You almost certainly won't need this again; keep `--dry-run` until you trust the moves.

```bash
darkroom catalog migrate-archive --archive "/Volumes/Astrophotography" --dry-run
darkroom catalog migrate-archive --archive "/Volumes/Astrophotography"
```

---

## Browsing — web UI

The catalog is browsed via the `darkroom.webapi` web UI (deployed on the LXC;
run locally with `uvicorn --factory darkroom.webapi.app:create_app_from_env`).
For ad-hoc SQL, open the DB directly: `sqlite3 ~/.config/darkroom/astro_catalog.db`.

---

## Archive cleanup — `darkroom triage ...`

Transient tool for cleaning up the **existing** archive backlog (placeholder FITS
`OBJECT` headers, RA/DEC mismatches, mis-filed calibration, legacy naming). Writes to a
separate `triage.db` — it does **not** touch the catalog. Scaffolding; expected to go away
once the backlog is clean.

```bash
# 1. Walk the archive and flag issues into triage.db
darkroom triage scan --archive "/Volumes/Astrophotography"

# 2. Review/fix flagged items in the web UI (default http://127.0.0.1:8002)
darkroom triage serve --archive "/Volumes/Astrophotography"
darkroom triage serve --archive "/Volumes/Astrophotography" --port 8010
```

| Flag | Use |
|---|---|
| `--archive PATH` | Archive root to scan/serve (env: `DARKROOM_ARCHIVE`, like the other commands). |
| `--db PATH` | **triage.db**, not the catalog (default `<archive>/triage.db`). |
| `--port` / `--host` | (serve) bind address; default `127.0.0.1:8002`. |

---

## Typical end-to-end run

```bash
# A. New imaging night landed on disk
darkroom ingest scan --asiair /Volumes/ASIAIR/Autorun --manifest run.yaml
#    review run.yaml, then:
darkroom ingest commit run.yaml

# B. Stage it for PixInsight
darkroom wbpp --target "M 81" --date 2026-02-19
#    → open the SESSION_N dirs in WBPP, set the Output/ dir, run WBPP + processing

# C. File the results and mark done
darkroom finish --target "M 81"

# Check the books
darkroom catalog list --target "M 81"

# D. Periodically: archive the ASIAir logs and refresh guiding stats
darkroom logs import --source /Volumes/ASIAIR/log --apply
darkroom catalog backfill-times --apply
darkroom catalog scan-guiding --apply
```

---

## Gotchas

- **Source is sacred.** `ingest` never deletes SD-card originals — clear them yourself.
- **Filter comes from the filename**, not the FITS header (ASIAir doesn't write FILTER).
- **No TTY in CCC postflight** — the postflight can only run `ingest scan --manifest`;
  `review` needs a terminal and `commit` is a deliberate step you run yourself.
- **Never fix a manifest in a text editor** — identity fields are derived and are
  *not* recomputed at commit. Use `darkroom ingest review`. See the ⚠️ above.
- **Quote targets with spaces:** `--target "M 81"`. Spacing/case are normalised either way.
- **Flat matching defaults to ±3 days** — bump `--flat-window` if archived flats are older.
- **Flats are ranked by the flat-morning rule, not raw proximity** — the set shot
  the morning after a session (`+1 day`) beats one from the previous night, even
  though both are 1 day away. `wbpp` shows each candidate's offset so you can
  override; a different night's flats mean a different sky and often a very
  different flat exposure.
- **`ModuleNotFoundError` only when a prompt appears** → the bare `darkroom` on
  PATH is a `uv tool` install whose dependencies are stale. Fix:
  `uv tool install --force --editable .` from the repo. See below.
- **Everything that writes is a dry run by default.** `--apply` is the verb that
  writes, on `logs import`, `scan-processed`, `rescan-archive`, `apply-renames`,
  `backfill-times`, `backfill-sites` and `scan-guiding`. `ingest` is the
  exception: its write step is `commit`.
- **`apply-renames` is owed after every web-UI identity edit** — the server
  can't reach the NAS, so the folder move waits for you on the Mac.
- **`backfill-times` only fills NULLs.** Change a session's span and you must
  clear it before the backfill will revisit, then re-run `scan-guiding` or the
  guiding row stays stale.
- **A camera lens can impersonate a telescope.** OTA is inferred from
  `FOCALLEN` alone, so a Canon 100-400 zoom shot at 180mm reads as `FMA180` and
  at 400mm as `FRA400` — confidently wrong rather than `Unknown`, so nothing
  flags it. Correct the OTA in `ingest review` when using a zoom.
- **`uv run pytest` on a fresh checkout or worktree** fails with
  `Failed to spawn: pytest` until you run `uv sync --extra dev` once — pytest
  is in the `dev` extra, not the base dependencies.

## Bare `darkroom` vs `uv run darkroom`

If `darkroom` is on your PATH via `uv tool install --editable .`, it runs your
current source but keeps its **own** venv, with dependencies pinned at install
time. Add a dependency to `pyproject.toml` and the two diverge silently.

Because optional deps are imported lazily (deliberately — `questionary` is
imported inside the prompt functions so `picker`/`ingest_review` stay importable
without a TTY), the break is invisible until a prompt actually renders:

| | bare `darkroom`, stale tool venv |
|---|---|
| `ingest scan` / `commit` | fine — never prompts |
| `ingest review` | `ModuleNotFoundError: questionary` at the first menu |
| `wbpp` picker (bare `wbpp`) | same |
| `wbpp --target X --date Y` | fine — skips the picker |

```bash
which -a darkroom                                      # which one am I running?
~/.local/share/uv/tools/darkroom/bin/python -c "import questionary"   # is it stale?
uv tool install --force --editable .                   # re-resolve (run in the repo)
```

Re-run that after **every** dependency change. Scripts should call the venv
binary by absolute path (`/path/to/darkroom/.venv/bin/darkroom`) instead of
relying on PATH — that's why the CCC postflight never hits this.
