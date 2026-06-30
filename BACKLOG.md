# darkroom — Backlog

Captured 2026-06-30 from a whole-codebase review + a web-UI readiness assessment.
Line numbers are accurate as of commit `5c8936d`; re-grep before editing if the
tree has moved on. Severity: **P1** = correctness, act first · **P2** = minor /
docs · **R** = refactor · **W** = web-UI prep.

---

## P1 — Correctness bugs

### B1. `darkroom finish` marks zero sessions under the filter-subdir layout — ✅ FIXED
> `finish.py` now resolves session IDs by matching each Lights symlink's
> resolved directory against `archive_root / lights_path` (layout-agnostic),
> dropping the positional `.parent` walking. Regression test:
> `tests/test_wbpp_finish.py::test_resolve_session_ids_filter_subdir_layout`.

- **Where:** `darkroom/finish.py:49-70` (`_collect_session_folders`), `:73-89` (`_resolve_session_ids`)
- **Problem:** The archive moved to `…/<session>/Lights/<filter>/<file>.fit` (commit
  "split sessions by filter"; `ingest.session_dest_rel` writes `…/Lights/<filter>`,
  and catalog `lights_path` includes the filter component). `_collect_session_folders`
  still assumes the old `…/<session>/Lights/<file>.fit` shape and does
  `resolved.parent.parent` to find the session folder — with the extra `<filter>`
  level this now points at `Lights`, so the triple becomes
  `("M 81", "<datefolder>", "Lights")`. `_resolve_session_ids` then builds
  `rel = "M 81/<datefolder>/Lights/Lights"` and queries `target="<datefolder>"` —
  neither matches the stored row (`target="M 81"`,
  `lights_path="01_Deep Sky Objects/M 81/<datefolder>/Lights/L-Pro"`). The built
  `rel` also omits the `01_Deep Sky Objects` prefix and the filter subdir.
- **Symptom:** `finish` copies the stacks but prints "no catalog sessions matched
  symlinks — nothing to mark" and marks nothing processed. Silent.
- **Fix:** Resolve session IDs by matching each symlink's resolved absolute path
  against `archive_root / lights_path` directly (the catalog already stores the
  full relative `lights_path` including prefix + filter), rather than re-deriving
  folder triples by positional `.parent` walking. That makes it layout-agnostic.
- **Tests:** `tests/test_wbpp_finish.py` only covers the small helpers
  (`_find_processing_date`, `_build_dest`, `_copy_flat`, `_list_session_dirs`,
  `_confirm_and_delete`). Add a regression test that builds a fake archive +
  catalog row in the new layout, runs `_resolve_session_ids`, and asserts the
  session is found and marked.

### B2. Flat-dark "+1 morning" matches are silently dropped — ✅ FIXED
> `prep.py:_build_night` now filters flat-dark files by each matched row's own
> `capture_date` instead of the flat's date, so the `flat_date+1` fallback works.
> Regression test:
> `tests/test_wbpp_finish.py::test_build_night_symlinks_flat_darks_dated_next_morning`.

- **Where:** `darkroom/prep.py:183-195`, `darkroom/wbpp.py:48-61` (`discover_flat_darks`), `darkroom/catalog.py:112-129` (`find_flat_darks`)
- **Problem:** `find_flat_darks` correctly accepts flat darks captured on
  `flat_date` **or** `flat_date+1`. But `_build_night` passes the *flat's* date
  into `discover_flat_darks(..., capture_date=flat_date)` for every matched row,
  and `discover_flat_darks` filters files by exact `dt.date() == capture_date`.
  A FlatDark set captured the morning after lives in the shared
  `FlatDarks/<camera>/` folder with filenames dated `flat_date+1`, so the
  exact-match filter returns 0 files. The `+1` fallback is effectively dead.
- **Fix:** In `prep.py`, filter by the matched row's own date —
  `discover_flat_darks(output / fd_row["folder_path"], capture_date=Date.fromisoformat(fd_row["capture_date"]))`
  — not `flat_date`.
- **Tests:** Add a case where flat darks are dated one day after the flats and
  assert they get symlinked.

### B3. `darkroom triage scan` scans the wrong DSO root
- **Where:** `darkroom/triage/scanner.py:274` (`dso = archive_root / "04_Deep Sky Objects"`)
- **Problem:** Canonical DSO root was renamed to `01_Deep Sky Objects` (commit
  4799a2b; `ingest.py:50`, `finish.py:46`, `cataloger.py:516` all use `01_`).
  Triage still looks in `04_`, so all DSO-side scanners
  (`calibration_in_target`, `processed_dir`, `legacy_session`, `fits_headers`)
  silently find nothing.
- **Fix:** First **confirm the physical NAS layout** (is it `01_` now, or does
  the legacy archive triage targets still have `04_`?). Then either change the
  constant to `01_` or detect both (`for name in ("01_Deep Sky Objects",
  "04_Deep Sky Objects"): if (archive_root / name).exists(): …`). Triage is
  scaffolding, but right now it's a no-op on the current archive.

### B4. `check_ra_dec` crashes the whole triage scan on sexagesimal RA/DEC
- **Where:** `darkroom/triage/checks.py:63` (`SkyCoord(ra=float(ra), dec=float(dec), unit="deg")`)
- **Problem:** `float(ra)` is *outside* the `try` block. The cataloger's
  `_parse_coords` (`cataloger.py:30-45`) deliberately handles both float-degrees
  and sexagesimal strings ("09 55 33") because older rigs write the latter — and
  triage exists to clean up that messy legacy archive. On such a header,
  `float()` raises `ValueError` that propagates through `check_ra_dec` →
  `scan_fits_headers` → `scan_archive`, aborting the entire scan.
- **Fix:** Reuse `_parse_coords` (ideally after it's moved to a shared module —
  see **R6/W5**) to parse RA/DEC, and skip the frame (return `None`) when it
  can't be parsed.

### B5. `wbpp` symlinks both the master AND the raw subs
- **Where:** `darkroom/prep.py:137-143` (darks), `:152-159` (bias)
- **Problem:** The loops iterate every row from `find_darks`/`find_bias`
  (masters ordered first) and symlink all of them. When a master `.xisf` *and*
  raw subs both match, both land in `Darks/`/`Bias/`, contradicting the commit
  intent ("prefer masterDark/masterBias .xisf over raw subs") and handing WBPP a
  mixed master+raw set.
- **Fix:** Decide intended behaviour. If "prefer" = "use master instead of
  raws", `break` after the first row that produced symlinks when it's a master
  (rows are already `ORDER BY is_master DESC`). **Verify first** how the 585 /
  Canon calibration is actually stored before changing — this is design-ambiguous.

---

## P2 — Minor / docs

### B6. Stale `04_Deep Sky Objects` in help/docstrings
- `darkroom/finish.py:250` (subparser description says `04_`, code writes `01_`),
  `darkroom/cataloger.py:1027`, `:1084-1085` (legacy epilog/help).
- Update to `01_`. Also reconcile `CLAUDE.md`, which mixes `04_` and `01_`.

### B7. `triage` CSV export uses naive quoting
- **Where:** `darkroom/triage/server.py:278-284` (`export.csv`)
- Hand-rolled `"`-wrapping breaks if a path contains a quote. Use the stdlib
  `csv` module. Low priority (localhost single-user tool).

---

## R — Refactors

### R1. Consolidate the two calibration-scan implementations
- `cataloger.CalibrationCataloger.scan` (`cataloger.py:715-838`) and
  `scanner._scan_calibration` (`scanner.py:128-188`) independently re-implement
  frame-type inference, the flat-dark threshold, temp rounding, filter-from-
  filename, and group keying. `_FLAT_DARK_THRESHOLD_SEC` is defined **three
  times**: `cataloger.py:647`, `scanner.py:130`, `suggest.py:25`.
- Extract one shared grouping helper + one threshold constant so the two ingest
  paths can't drift.

### R2. Delete the legacy `cataloger.finish_command`
- `cataloger.py:497-542` (`finish_command`) + its argparse wiring
  (`cataloger.py:1070-1095`, dispatch at `:1109-1110`). The live command is
  `finish.py:cmd_finish`; the cataloger one is reachable only via
  `python -m darkroom.cataloger finish` and builds paths differently
  (`_normalize_target` vs `_target_slug`). ~100 lines of confusable dead surface.
  Remove once nothing references it.

### R3. Unify the two `set_id` builders
- `cataloger.py:796-800` (in `CalibrationCataloger.scan`) vs
  `ingest.make_cal_set_id` (`ingest.py:191-202`). They format gain differently
  (`_format_gain` → `ISO1600`/`200g` vs literal `{gain}g`). If a Bias written by
  `ingest commit` and the same set re-scanned by `catalog scan-calibration` must
  collide on `set_id`, these can diverge for DSLR ISO gains and create duplicate
  rows. Pick one builder and share it.

### R4. Share `_target_slug`
- Defined identically in `prep.py:56` and `finish.py:16`. The wbpp↔finish
  handoff depends on them staying identical — co-locate (e.g. in `config.py` or
  a small `names.py`) to remove silent-drift risk.

### R5. Dedup FITS-file collection
- `cataloger.find_lights_folders` / `scan_all_command` (`cataloger.py:905-909`)
  hand-roll `.fit/.fits` filtering with non-recursive `iterdir`, while `scanner`
  uses `parse.fits_files` (recursive option). Route both through
  `parse.fits_files`.

### R6. Extract name/coord helpers out of `cataloger.py` into a lightweight module
- `_normalize_target`, `_normalize_camera`, `_format_gain`, `_parse_coords`,
  `_round_exposure` live in `cataloger.py`, which top-level imports
  `astropy.io.fits`, `SkyCoord`, `Time`, `astroquery`. Anything importing these
  helpers (`catalog.py` imports `_normalize_target`; `checks.py` wants
  `_parse_coords`) drags in astropy. Move them to `parse.py` or a new
  `darkroom/names.py`. **Prerequisite for W5** (web read layer must not import
  astropy).

---

## W — Web-UI prep (display + edit the catalog)

> Architecture note: model the catalog UI on the **triage subpackage** — it's a
> working `FastAPI + Jinja2 + db.py + server.py + templates/` reference. Add a
> new subcommand (e.g. `catalog ui` / `catalog serve-ui`) distinct from the
> existing datasette `serve`. Read-only display can ship on today's schema;
> the items below are needed for a UI that **edits/works with** the catalog.
>
> Migration safety: `init_db` already does additive migrations (`focal_length`,
> `is_master` via `PRAGMA table_info` checks at `cataloger.py:298-304`) — follow
> that pattern, never drop columns on a live DB, and back up `astro_catalog.db`
> first.

### W1. Replace overloaded `processed_status` free-text with structured status
- **Today:** `processed_status` stores a date *or* a path *or* a note
  ("skipped — bad tracking"). A UI can't render it as a state, filter
  processed/unprocessed reliably, or sort by processing date.
- **Do:** Add `processed_state` (enum: `unprocessed`/`processed`/`skipped`),
  `processed_path`, `processed_date`; keep `notes` for free text. Backfill from
  the existing free-text where parseable. Update writers: `finish.py`
  (`_mark_sessions_processed` / `mark_processed`), `cataloger.mark_processed*`,
  `catalog_cli` `mark`.

### W2. Normalize empty-value conventions (`""` vs `NULL`)
- `filter` is `""` from scan-all (`cataloger.py:613` `... or ""`) but `None` /
  `"NoFilter"` from ingest. `processed_status` is `""` on insert. A UI's
  GROUP BY / filter logic must special-case both. Pick one (recommend `NULL` for
  "absent", `"NoFilter"` only for deliberate bare-filter shots) and migrate.

### W3. Stable surrogate key + identity-edit story
- `session_id` is a composite natural key (`target_date_ota_camera_filter`). If
  the UI lets a user fix a mis-parsed target/filter, the PK changes →
  `upsert_session` creates a *new* row and **orphans `processed_status`/`notes`**
  on the old one (upsert only preserves them on matching `session_id`). Editing
  identity fields is silently destructive today.
- **Do:** Add `id INTEGER PRIMARY KEY`, demote `session_id` to a `UNIQUE` mutable
  column, and have edits update in place. Or, if keeping the natural key, give
  the UI an explicit rename-migration path that carries status/notes forward.

### W4. Catalog write/query API module (`darkroom/catalog/db.py` or similar)
- No API to update a session beyond full-row `upsert_session` + `mark_processed`.
  A UI editing notes/target would embed raw SQL.
- **Do:** Mirror `triage/db.py`: `open_db` (with WAL — see W6),
  `update_session_fields(db, key, **fields)`, a generic
  `query_sessions(... filters ..., limit, offset)` supporting
  camera/ota/filter/date-range/processed-state, and `count_sessions(...)`.
  Current `query_sessions` only filters target/obs_date/session_id and
  `query_all_sessions` has no pagination (full-table) — fine at current scale
  (dozens–hundreds of rows) but add `LIMIT/OFFSET` before the UI grows.

### W5. Decouple the read layer from astropy
- See **R6**. The web backend's read path should not pay astropy import cost /
  dependency surface. After R6, `catalog.py` and the new `catalog/db.py` import
  only the lightweight name helpers.

### W6. Enable WAL mode in `init_db`
- No `PRAGMA journal_mode=WAL` today. A browser reading while `ingest commit` /
  `finish` writes will hit `database is locked`. Add
  `conn.execute("PRAGMA journal_mode=WAL")` in `init_db` (`cataloger.py:252`).
  One line, big concurrency win.

### W7. Indexes + timestamps
- Only the PK is indexed. Add indexes on `target`, `obs_date`, `processed_state`
  (post-W1). Add `created_at` / `updated_at` (`DEFAULT (datetime('now'))`, as
  triage's tables have) to `sessions` and `calibration_sets` so the UI can show
  "recently added" and sort by ingest time.

### W8. (Optional) Persisted session↔calibration linkage
- There's no recorded link between a session and the calibration sets used —
  matching is recomputed at query time (`find_darks/find_flats/...`). A UI
  showing "calibration used for this stack" must recompute. Acceptable; decide
  whether the UI needs a persisted `finish`-time linkage table.

---

## Suggested order for a future session
1. **B1 + B2** (finish + flat-darks) — silent data-pipeline failures, with tests.
2. **R6 + W5/W6/W7** schema+helper groundwork (move name helpers, WAL, indexes,
   timestamps) — unblocks the web work and B4.
3. **B4** (reuse `_parse_coords`), **B3** (confirm `01_` vs `04_`), **B5** (after
   verifying intended master/raw behaviour).
4. **W1/W2/W3/W4** the real web-UI data-model + API prep.
5. **R1–R5, B6, B7** cleanup as capacity allows.
</content>
</invoke>
