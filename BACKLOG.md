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

### B3. `darkroom triage scan` scans the wrong DSO root — ✅ FIXED
> Confirmed with the user: `01_Deep Sky Objects` is the actual current name on
> both the work SSD and the NAS — no dual-support needed. Changed the constant
> at `darkroom/triage/scanner.py:274`. Added the first-ever test coverage for
> `scan_archive` itself (previously zero, which is why this went unnoticed).
> Tests: `tests/triage/test_scanner.py::TestScanArchive`.

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

### B4. `check_ra_dec` crashes the whole triage scan on sexagesimal RA/DEC — ✅ FIXED
> Reused `darkroom.names._parse_coords` (the shared helper from R6) for both
> the `SkyCoord` construction and the returned mismatch dict's `frame_ra`/
> `frame_dec` fields (the same `float()` bug was duplicated in both places).
> Returns `None` early when parsing fails instead of raising.
> Tests: `tests/triage/test_checks.py::TestCheckRaDec` (`test_sexagesimal_coords_do_not_crash`,
> `test_sexagesimal_mismatch_returns_degree_values`).

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

### B5. `wbpp` symlinks both the master AND the raw subs — ✅ FIXED
> Confirmed intent from commit `5c8936d`'s message ("prefer... falling back to
> raw subs"). Fixed by partitioning each matched row list into master-vs-raw
> and using only the masters when any exist — not "break after the first
> master row", which would have silently dropped legitimate additional master
> rows at different capture temperatures (`find_darks`/`find_bias` don't filter
> on temperature). Applied identically to both the Darks and Bias loops in
> `darkroom/prep.py:_build_night`.
> Tests: `tests/test_wbpp_finish.py` (`test_build_night_prefers_master_dark_over_raw_subs`,
> `test_build_night_prefers_master_bias_over_raw_subs`).

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

### B6. Stale `04_Deep Sky Objects` in help/docstrings — ✅ FIXED
> Renamed all 15 remaining `04_` occurrences across `darkroom/catalog_cli.py`,
> `darkroom/cataloger.py`, `darkroom/finish.py`, `CLAUDE.md`, `CHEATSHEET.md`,
> `README.md`. Deliberately left untouched: `docs/superpowers/plans/*.md` /
> `docs/superpowers/specs/*.md` (historical records from when `04_` was
> current), test fixture literals (arbitrary placeholder strings, behaviorally
> inert), and `darkroom/cataloger.py:120`'s docstring (deliberately documents
> that `_target_from_path`'s matching logic supports either prefix).

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

### R6. Extract name/coord helpers out of `cataloger.py` into a lightweight module — ✅ FIXED
> Moved `_normalize_target`, `_normalize_camera`, `_format_gain`, `_parse_coords`,
> `_round_exposure` into `darkroom/names.py` — stdlib-only at module load; the
> astropy import for `_parse_coords`'s sexagesimal fallback is lazy (inside the
> function, not at module scope). `cataloger.py` and all other callers
> (`ingest.py`, `scanner.py`, `triage/suggest.py`) now import from there.
> Tests: `tests/test_names.py`.

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

### W1. Replace overloaded `processed_status` free-text with structured status — ✅ DONE
> Added `processed_state` (enum `unprocessed`/`processed`/`skipped`, `NOT NULL
> DEFAULT 'unprocessed'`), `processed_path`, `processed_date` to `sessions`. The
> legacy `processed_status` column is **kept, not dropped** (migration safety),
> but no live writer touches it anymore. One-time backfill parses the old
> free-text (bare date → processed+date; `_Processed/<date>` path → processed +
> path + extracted date; `skip…` → skipped, text moved to `notes` iff empty;
> other non-blank → processed + best-effort path/date; blank → unprocessed).
> New writer `cataloger.set_processed_state()`; `finish.py` and
> `mark_processed_by_target` now write structured columns; `catalog mark` CLI is
> now `mark <id> <state> [--date/--path/--notes]` (argparse `choices`);
> `picker.is_processed` reads `processed_state == 'processed'`. Backfill runs
> exactly once (folded into the W3 rebuild gate) so it can never clobber a later
> `set_processed_state`. Tests: `tests/test_cataloger.py::TestSchemaMigration`,
> `::TestSetProcessedState`, `::TestMarkProcessedCommandCLI`.
- **Today:** `processed_status` stores a date *or* a path *or* a note
  ("skipped — bad tracking"). A UI can't render it as a state, filter
  processed/unprocessed reliably, or sort by processing date.
- **Do:** Add `processed_state` (enum: `unprocessed`/`processed`/`skipped`),
  `processed_path`, `processed_date`; keep `notes` for free text. Backfill from
  the existing free-text where parseable. Update writers: `finish.py`
  (`_mark_sessions_processed` / `mark_processed`), `cataloger.mark_processed*`,
  `catalog_cli` `mark`.

### W2. Normalize empty-value conventions (`""` vs `NULL`) — ✅ DONE
> `NULL` is now the sole "absent/unknown filter" sentinel; `NoFilter`/
> `UnknownFilter` remain deliberate signal values. scan-all's `... or ""` filter
> fallback changed to `... or None`; `init_db` migrates existing `filter = ''`
> rows to `NULL`. `processed_status = ''`-on-insert removed from both live insert
> paths (`cataloger` scan-all + `ingest`) — the `processed_state` default covers
> it. `catalog.find_flats` already treated `filter IS NULL` as absent; unchanged.
> Verified on a populated DB: `''` → `NULL`, real filters (`Ha`, `L-Pro`) intact.
- `filter` is `""` from scan-all (`cataloger.py:613` `... or ""`) but `None` /
  `"NoFilter"` from ingest. `processed_status` is `""` on insert. A UI's
  GROUP BY / filter logic must special-case both. Pick one (recommend `NULL` for
  "absent", `"NoFilter"` only for deliberate bare-filter shots) and migrate.

### W3. Stable surrogate key + identity-edit story — ✅ DONE
> `sessions` now has `id INTEGER PRIMARY KEY`; `session_id` demoted to
> `TEXT NOT NULL UNIQUE` (so `upsert_session`'s `ON CONFLICT(session_id)` still
> works). Migrated via a one-time, idempotent table rebuild (guarded on `id`
> being absent): `CREATE sessions_new` → `INSERT…SELECT` an explicit
> non-generated column list (the `total_integration_hours` VIRTUAL column is
> re-derived, never copied) → `DROP`/`RENAME` → recreate indexes. Fresh-DB and
> migrated-DB schemas verified identical. The in-place identity-edit mechanism
> (recompute `session_id`, carry status/notes forward, no orphan) lives in
> **W4**'s `update_session_fields`. Tests: `TestSchemaMigration` (12 cases incl.
> idempotency + fresh/migrated convergence).
- `session_id` is a composite natural key (`target_date_ota_camera_filter`). If
  the UI lets a user fix a mis-parsed target/filter, the PK changes →
  `upsert_session` creates a *new* row and **orphans `processed_status`/`notes`**
  on the old one (upsert only preserves them on matching `session_id`). Editing
  identity fields is silently destructive today.
- **Do:** Add `id INTEGER PRIMARY KEY`, demote `session_id` to a `UNIQUE` mutable
  column, and have edits update in place. Or, if keeping the natural key, give
  the UI an explicit rename-migration path that carries status/notes forward.

### W4. Catalog write/query API module (`darkroom/catalog/db.py` or similar) — ✅ DONE
> New `darkroom/catalog_db.py` (named `catalog_db` to avoid clashing with the
> existing `catalog.py` module). `open_db(path)` → Row-factory conn + WAL,
> lazily calling `init_db` only when the file is missing. `query_sessions(conn,
> *, target/obs_date/session_id/camera/ota/filter/date_from/date_to/
> processed_state, limit, offset)` and `count_sessions(...)` share one
> `_build_where` helper. `update_session_fields(conn, session_id, **fields)`
> whitelists editable columns, validates `processed_state`, and — the W3
> anti-orphan payoff — when an identity component changes it recomputes
> `session_id` and folds it into a single `UPDATE … WHERE id = ?`, carrying
> status/notes/created_at forward on the same row; a rename that collides with
> another row's `session_id` raises before writing. `make_session_id` moved to
> `darkroom/names.py` so the module stays **astropy-free at import** (W5
> constraint; verified by a subprocess `sys.modules` test). Tests:
> `tests/test_catalog_db.py` (33), `tests/test_names.py` (make_session_id).
- No API to update a session beyond full-row `upsert_session` + `mark_processed`.
  A UI editing notes/target would embed raw SQL.
- **Do:** Mirror `triage/db.py`: `open_db` (with WAL — see W6),
  `update_session_fields(db, key, **fields)`, a generic
  `query_sessions(... filters ..., limit, offset)` supporting
  camera/ota/filter/date-range/processed-state, and `count_sessions(...)`.
  Current `query_sessions` only filters target/obs_date/session_id and
  `query_all_sessions` has no pagination (full-table) — fine at current scale
  (dozens–hundreds of rows) but add `LIMIT/OFFSET` before the UI grows.

### W5. Decouple the read layer from astropy — ✅ FIXED
> `catalog.py:6` now imports `_normalize_target` from `darkroom.names` instead of
> `darkroom.cataloger`. Regression test (subprocess-isolated, since sibling test
> files import astropy-heavy `cataloger.py` first and would otherwise pollute an
> in-process `sys.modules` check):
> `tests/test_catalog.py::test_importing_catalog_does_not_pull_in_astropy`.

- See **R6**. The web backend's read path should not pay astropy import cost /
  dependency surface. After R6, `catalog.py` and the new `catalog/db.py` import
  only the lightweight name helpers.

### W6. Enable WAL mode in `init_db` — ✅ FIXED
> `init_db` now runs `conn.execute("PRAGMA journal_mode=WAL")` immediately after
> connecting, before `executescript`. Test:
> `tests/test_cataloger.py::TestSQLiteCatalog::test_init_db_enables_wal`.

- No `PRAGMA journal_mode=WAL` today. A browser reading while `ingest commit` /
  `finish` writes will hit `database is locked`. Add
  `conn.execute("PRAGMA journal_mode=WAL")` in `init_db` (`cataloger.py:252`).
  One line, big concurrency win.

### W7. Indexes + timestamps — ✅ FIXED (target/obs_date indexes + created_at/updated_at)
> Added `idx_sessions_target` / `idx_sessions_obs_date`. Added `created_at` /
> `updated_at` `TEXT` columns to `sessions` and `calibration_sets` — set
> explicitly in Python inside `upsert_session` / `upsert_calibration_set`, **not**
> a SQL `DEFAULT`: SQLite refuses a non-constant `ALTER TABLE ADD COLUMN` default
> on a table that already has rows (verified empirically against the populated-DB
> migration path), so `DEFAULT (datetime('now'))` as originally suggested below
> would crash on the real `astro_catalog.db`. `created_at` is preserved across
> re-scans (excluded from `ON CONFLICT DO UPDATE`); `updated_at` refreshes on
> every write. Migration backfills existing `NULL` rows once. The
> `processed_state` index from the original ask is deferred — that column
> doesn't exist until **W1** lands.
> Tests: `tests/test_cataloger.py::TestSQLiteCatalog` (`test_init_db_creates_indexes`,
> `test_init_db_adds_timestamp_columns`, `test_init_db_backfills_timestamps_on_existing_rows`,
> `test_upsert_session_sets_created_and_updated_at`).

- Only the PK is indexed. Add indexes on `target`, `obs_date`, `processed_state`
  (post-W1). Add `created_at` / `updated_at` (`DEFAULT (datetime('now'))`, as
  triage's tables have) to `sessions` and `calibration_sets` so the UI can show
  "recently added" and sort by ingest time.

### W8. (Optional) Persisted session↔calibration linkage
- There's no recorded link between a session and the calibration sets used —
  matching is recomputed at query time (`find_darks/find_flats/...`). A UI
  showing "calibration used for this stack" must recompute. Acceptable; decide
  whether the UI needs a persisted `finish`-time linkage table.

### W9. Always-on web API + client/server split + deployment

> **Phase 1 shipped 2026-07-05** (`b576e64` scaffold, `d743198` write-path
> rewiring): `darkroom/webapi/` (FastAPI, all 7 /api routes, bearer auth,
> `uvicorn --factory darkroom.webapi.app:create_app_from_env`),
> `darkroom/catalog_client.py` (`CatalogBackend` / `LocalBackend` /
> `HttpBackend` / `resolve_backend`), `catalog_url`/`DARKROOM_CATALOG_URL` +
> `api_token`/`DARKROOM_API_TOKEN` config keys, and
> `catalog_db.query_calibration_sets`. All four write paths (ingest commit,
> finish, scan-processed --apply, catalog mark) now go through
> `resolve_backend`; URL unset → LocalBackend, so local/offline behaviour is
> unchanged. 6 end-to-end LocalBackend↔HttpBackend parity tests
> (tests/test_client_server.py); suite 524 passed.
> **Read paths shipped 2026-07-05** (`6c64813`): catalog.py is now the pure
> matching layer over a `CatalogBackend` (`query_sessions` deleted; matchers
> fed from `query_calibration_sets`, date/exposure/NULL-filter logic stays
> client-side); backend threaded through catalog list, wbpp picker/prep,
> scan-processed, and `finish._resolve_session_ids`. Matcher parity tests
> local vs HTTP; live smoke: `uvicorn --factory` + CLI `list`/`mark` over
> real HTTP round-trips. 528 tests. **The CLI is now fully backend-agnostic —
> flipping `DARKROOM_CATALOG_URL` switches the whole surface to remote.**
> **Phase 3 (LXC deploy) shipped 2026-07-05** (`ddc3d40` unit file): webapi
> live on the `darkroom` LXC (Debian 13, 192.168.2.217:8000). FHS layout:
> git clone at `/opt/darkroom` (`uv sync --no-dev` venv), DB at
> `/var/lib/darkroom/astro_catalog.db`, bearer token in root-only
> `/etc/darkroom/env` (`DARKROOM_API_TOKEN=`), systemd unit
> `deploy/darkroom-api.service` (tracked in-repo, `systemctl link`ed,
> enabled). Redeploy: `git pull && uv sync --no-dev && sudo systemctl
> restart darkroom-api`. Mac CLI flipped to remote via `catalog_url` +
> `api_token` in `~/.config/darkroom/darkroom.toml`; verified end-to-end.
> The server DB copy is authoritative; the Mac-local file is dormant.
> **Phase 2 (Jinja2 edit UI) built 2026-07-06** (in working tree, pending
> commit/deploy): `darkroom/webapi/ui.py` + `darkroom/templates/catalog/`
> mounted on the same app — cookie login reusing the API bearer token,
> sessions grouped by target (camera+OTA per row), one-click
> `processed_state` buttons, per-session edit form over
> `update_session_fields` (changed-fields-only, so identity renames only
> fire when actually edited). 12 UI tests; `/api` stays bearer-only.
> Alongside it, `tests/conftest.py` autouse hermeticity guard (HOME → tmp,
> `DARKROOM_*` env stripped): CLI tests had been resolving the real
> `catalog_url` from `~/.config/darkroom/darkroom.toml` since the phase-3
> remote flip and making live calls at the production API — only Little
> Snitch's block stopped `scan-processed --apply` tests writing to the
> prod catalog. Suite: 540 passed, 0 failed.
> **Nightly backup shipped 2026-07-06** (01ba55a + scp fixes):
> `deploy/darkroom-backup.{sh,service,timer}` — 04:30 Lisbon timer,
> `VACUUM INTO` dated snapshot under `/var/lib/darkroom/backups/` (14-day
> prune), pushed to the NAS at
> `darkroom-backup@192.168.2.17:/volume1/backups/darkroom` (ssh port
> 3673, key `~/.ssh/id_ed25519_nas_backup` on the LXC; same-retention
> remote prune via `find -mtime`). Verified end-to-end. Gotchas baked
> into the script comments: Synology's patched rsync needs DSM's rsync
> service running (error 43), so we use scp instead — and `-O` (legacy
> protocol), because DSM chroots the SFTP subsystem to `/volume1`, which
> would make scp-SFTP and the ssh find-prune disagree about paths.
> **Remaining:** dev-snapshot helper (pull latest NAS backup → local file
> → run uvicorn against it; deferred to the front-end work, build when
> first needed — decided 2026-07-06: full snapshot not subset, pytest
> stays on per-test tmp fixtures), then phase 4 (remove datasette).
> **Front-end design signed off 2026-07-07** ("safelight" direction, 4
> mock iterations on live data; the v4 mock is the build spec). IA:
> targets overview (home) → target detail (nights grouped by rig,
> expanded by default) → session edit; U2 queue later. Tokens: cool
> blue-grey dark ground `#14171c` / ink `#e2e6ed`, safelight red
> `#e8502a` reserved for interaction/identity; D-DIN (repo
> `datto-d-din/`) for designations/wordmark, Fira Mono for data;
> CVD-validated filter colors (L-Pro `#c98500`, L-Extreme `#0da189`,
> L-Synergy `#8a6cc9`, Baader `#3987e5`, gray for none). Signature:
> grease-pencil state marks (red circle processed / half-circle in
> progress / strike skipped / dotted open), click-to-set. Depth gauge
> (sqrt scale, ticks at 2/10/20h, zones needs-data/workable/solid/deep)
> on target rows and rig headers. Sortable columns; catalog + filter
> dropdowns (filter = any-session partial match); common names under
> designations (hardcoded map v1 — decide `common_name` storage +
> SIMBAD backfill later). Single dark theme by design.
> **Auth-flow review (queued 2026-07-07):** current browser auth is the
> raw API bearer token pasted into a login form and stored in a 90-day
> HttpOnly cookie (`ui.py:login_submit`). Fine for LAN single-user, but
> review before any exposure beyond the LAN (Tailscale is OK): consider
> a separate UI secret (revocable independently of the API token),
> cookie rotation/expiry on token change, and rate-limiting the login
> form. No user accounts — it stays single-user.

Captured 2026-07-05. **The build item** that W1–W8 were prep for: an
always-on FastAPI app on a homelab LXC that both serves the edit UI *and* owns
the catalog DB, with the Mac CLI reaching it over HTTP.

**Why a client/server split (not just "run the UI"):** two hosts must write one
catalog and they can't share a SQLite file safely. (1) The always-on web app
must live on the cluster — the Mac isn't always up. (2) The CLI pipeline is
hardware-bound to the Mac (reads the ASIAir SD card, writes WBPP symlinks, reads
the NAS archive — mounting those on the LXC over SMB makes every file-bound op a
slow network op). Both need to write. **Do not** put the SQLite file on a
NAS/SMB/NFS share and open it from both — SQLite locking is unreliable over
network FS and WAL (W6) doesn't work there. Resolution: the always-on LXC owns
the file (single writer process); the Mac CLI goes remote.

**Architecture decided (2026-07-05):** stay on SQLite — not Postgres/Supabase.
At ~200 rows growing slowly, single-user, Postgres buys nothing on performance
and costs a dialect port (WAL PRAGMA, the `total_integration_hours` VIRTUAL
generated column, the `ALTER TABLE` migration dance, `?`→`%s`); Supabase is
worse — a cloud/SaaS + latency dependency dragged into a fully-local homelab
tool. The prior-art SQLite-server projects (`~/Projects/net-worth`,
`~/Projects/investment-portfolio-tracker`) are TS/Vite + Express +
better-sqlite3 — **same architecture, wrong stack for this repo**: darkroom's
schema/migrations/`session_id` derivation/validation all live in Python
(`cataloger.init_db`, `catalog_db.py`), so a Node server would fork the write
logic across two languages and defeat W4. Build the API in **Python/FastAPI**,
modelled on the triage subpackage (in-repo FastAPI+Jinja2 reference). W4 already
funnels every write through a few functions, so the API is a transport wrapper —
logic does not move.

**Client side — `darkroom/catalog_client.py` (new):** a `CatalogBackend`
protocol with two impls selected by config:
- `LocalBackend` — opens the SQLite file directly, delegating to
  `catalog_db`/`cataloger` in-process (today's behaviour; runs `init_db` as
  needed). Used by tests and any laptop-only run.
- `HttpBackend` — httpx to the LXC with a bearer token; **no** `init_db` (server
  owns schema).
- `resolve_backend(cfg)` → `HttpBackend` iff `catalog_url` is set, else
  `LocalBackend`. New config keys `catalog_url` / `DARKROOM_CATALOG_URL` and
  `DARKROOM_API_TOKEN` slot into the CLI→env→toml chain in `config.py`. **URL
  set → remote; unset → local file** — this is what preserves "still works
  locally / offline without the server" (tests never set the URL).

**Call sites to route through `resolve_backend` (stop importing cataloger/
catalog_db fns directly):**
| File | Today | Becomes |
|---|---|---|
| `ingest.py:534,572,593` | `init_db` + `upsert_session`/`upsert_calibration_set` | `backend.upsert_session(...)` etc.; `init_db` skipped in http mode |
| `finish.py:111` | `set_processed_state` | `backend.set_processed_state(...)` |
| `procscan.py:311` | `set_processed_state` | `backend.set_processed_state(...)` |
| `catalog mark` → `mark_processed_command` | direct | `backend.set_processed_state(...)` |
| reads: `catalog list`, `wbpp` picker, `finish._resolve_session_ids`, `catalog.py` matchers | open file | `backend.query_sessions` / `find_calibration` |

**Server side — `darkroom/webapi/` (new; not `serve.py`, that's datasette):**
```
POST   /api/sessions                       → cataloger.upsert_session
POST   /api/calibration-sets               → cataloger.upsert_calibration_set
PATCH  /api/sessions/{session_id}          → catalog_db.update_session_fields   (UI edits + CLI)
POST   /api/sessions/{session_id}/state    → cataloger.set_processed_state
GET    /api/sessions            [+filters] → catalog_db.query_sessions
GET    /api/sessions/count      [+filters] → catalog_db.count_sessions
GET    /api/calibration-sets    [+keys]    → calibration rows (wbpp matching stays client-side)
GET    /  ...                              → Jinja2 edit UI (the web UI itself)
```
- Owns the file: `open_db(cfg.catalog_path)` at startup runs `init_db`/migration
  once; one uvicorn process = single writer, WAL handles concurrent reads.
- Auth: single-user homelab → one shared bearer token (`DARKROOM_API_TOKEN`) in a
  FastAPI dependency. No user accounts.
- Validation is inherited: `update_session_fields` already whitelists editable
  fields and validates `processed_state` — the PATCH route gets it for free.

**Scope decision (settled):** CLI *reads* also go through the API — the Mac keeps
no local copy, and the always-on dependency already exists for writes. Keep
`catalog.py`'s `find_darks/find_flats/find_flat_darks` *matching logic* (date
proximity) client-side; feed it candidate rows from `GET /api/calibration-sets`.
Logic stays put; only data access moves.

**Deployment (LXC):** `uvicorn darkroom.webapi.app:app` under systemd; catalog on
a **local disk, not a network mount**. Backup = **nightly `VACUUM INTO` copy of
the DB to the NAS** (cron) — good enough for a low-churn, reconstructible catalog
(worst case: `scan-processed` re-derives state, `ingest` re-registers). Litestream
(continuous replication → S3-compatible target, seconds-level RPO) is deferred to
a later task — overkill for day one; the nightly NAS copy is the v1 backup.

**Phasing (never half-broken):**
1. Build `webapi` server + `LocalBackend`/`HttpBackend` + `resolve_backend`,
   **default to local** — full parity, all tests still pass against local mode.
2. Build the Jinja2 edit UI on the read/write routes (surface processed sessions
   **grouped by target with camera + OTA visible** so cross-rig/cross-OTA
   clusters — legit multi-camera integrations — are obvious and per-session
   `processed_state` is one click to correct; see the scan-processed date-bound
   attribution caveat).
3. Deploy to LXC, flip `DARKROOM_CATALOG_URL` on the Mac, migrate the file over,
   nightly NAS backup cron on.
4. **Remove datasette** (closing step, same commit as the read view goes live):
   drop `serve.py`, the `datasette>=0.65` dep in `pyproject.toml:9`, the `serve`
   subcommand in `cli.py`, and doc mentions (`CLAUDE.md`, `README.md:94`,
   `CHEATSHEET.md:215`, `cataloger.py:9/1054/1164`). Keep it as the fallback
   browser until the new UI's read view actually works, then it's superseded.

Depends on: W1–W7 (done). Absorbs W8's decision (persisted linkage vs recompute —
default recompute). Related: U2 (filter cleanup queue) is a natural second UI view.

---

## U — CLI UX / interactive modes

Captured 2026-07-04. Root complaint: the CLI demands exact recall (target
designations, dates, session IDs, flag syntax) that nobody retains between
bursty imaging runs, and mismatches fail with a shrug instead of showing what
*does* exist. Recognition over recall.

### U1. `darkroom wbpp` interactive session picker — ✅ DONE
> Shipped 2026-07-04: `af69b4b` (picker + repeatable `--date`) and `e966200`
> (explicit prompt style — questionary's default dropdown is unreadable on dark
> terminals). New `darkroom/picker.py` (questionary imported lazily; module
> import stays dep/TTY-free), `prep.py` split into `_resolve_rows` +
> `build_wbpp_sessions`, loud failures listing available nights. Tests:
> `tests/test_picker.py`. Interactive prompts verified by pty (pexpect), not
> covered by the suite.

- Bare `darkroom wbpp` on a TTY launches a questionary-based picker:
  fuzzy-autocomplete target selection (annotated with unprocessed-night count +
  total integration) → per-night checkbox multi-select (unprocessed pre-checked,
  processed shown ✓ unchecked) → confirm → existing build pipeline.
- Kills the two worst frictions: remembering exact `--target`/`--date` values,
  and the one-session-or-all-sessions limitation (arbitrary night subsets,
  e.g. "just the four June 2026 nights").
- Also: `--date` becomes repeatable (`--date A --date B`) for scripted subsets.
- Design agreed 2026-07-04 (questionary dep; bare-invocation entry; repeatable
  `--date`, no `--from/--to`). Internal refactor: split "resolve sessions" from
  "build dirs" in `prep.py:cmd_prep` so picker and flags feed the same build path.

### U2. Filter-assignment cleanup queue for `NoFilter`/`UnknownFilter` sessions
- ASIAir doesn't write FILTER headers and Jonathan didn't always log filters, so
  the archive has sessions cataloged `NoFilter`/`UnknownFilter` that may be
  wrong — which silently poisons flat matching (`find_flats` keys on filter).
- Wanted: a review queue (natural fit: triage UI, alongside its existing checks)
  listing suspect sessions with context to jog memory — flats sets that exist
  near the session date, filters used by neighbouring sessions of the same
  target/OTA, exposure/gain hints.
- Applying a fix must update **both** the folder name (session dir encodes the
  filter) and the catalog row — note this crosses triage's current "never writes
  the catalog" boundary; either extend triage deliberately or make it a
  `catalog`-native command. Related to the deferred triage finalize/promote
  workflow.
- **Added 2026-07-07 (design conversation): duplicate/suspect target names
  too.** The live catalog has mosaic panels cataloged as distinct targets
  (`IC 4604_1-1` … `IC 4604_2-2`), duplicated designations (`M 82 M 82`,
  `M 81 M 82` vs `M 81`), and variants (`NGC 281W` vs `NGC 281`). The same
  review-queue UI should offer merge/rename for targets alongside filter
  fixes — same both-sides constraint (folder name + catalog row).

### U3. `darkroom ingest` interactive confirmation mode
- Extend the existing `ingest review` verb (today: a bare missing-filter prompt
  loop, `ingest.py:85-117`) into a full interactive confirmation pass over a
  scanned manifest: for each session/calibration group, confirm or correct the
  values parsed from ASIAir-generated FITS filenames — **filter**, **target
  name** (normalize odd ASIAir spellings to catalog designations), and
  **OTA+camera** (focal-length inference can be wrong for new/unknown optics).
- Same questionary UX as U1: autocomplete against known catalog values
  (existing targets, known filters, known OTA/camera combos) so corrections are
  picks, not typing. Writes the corrected manifest; `ingest commit` stays
  non-interactive (CCC/no-TTY constraint untouched).
- Goal: stop `NoFilter`/`Unknown` values entering the archive at ingest time —
  U2 cleans up the backlog, U3 closes the tap.

---

## F — Features

### F1. Derive processing state by scanning the archive for output artifacts — ✅ IMPLEMENTED (pending commit)
> Shipped 2026-07-04 as `darkroom catalog scan-processed --archive PATH
> [--apply]`. New `darkroom/procscan.py` (strictly read-only on the archive;
> dry-run is pure-read — no `init_db`, reads via `query_all_sessions`). Added a
> 4th enum value `in_progress` (final decision: 4-state `unprocessed /
> in_progress / processed / skipped`, collapsing "stacked" into "in_progress").
> Detection by extension: export (`.tif/.tiff/.jpg/.jpeg/.png/.psd/.psb`) →
> processed; `.xisf/.xpsm/.xosm` → in_progress; subs (`.fit/.fits/.orf/.cr2`,
> `_thn` thumbnails, anything under `Lights/`) ignored. **Attribution =
> date-bound**: an edit dated ≥ a night's `obs_date` covers it; newer nights
> stay unprocessed. Edit date recovered from a `YYYY-MM-DD` path component
> (`_Processed/<date>/`), else file mtime. `--apply` is **monotonic** (only
> upgrades along unprocessed<in_progress<processed; never downgrades, never
> touches `skipped`) and idempotent. Real read-only dry-run on the live archive:
> 75 → processed, 40 → in_progress, 90 unchanged. Tests: `tests/test_procscan.py`
> (27) + enum tests in test_cataloger/test_catalog_db/test_picker. **Requires the
> live `astro_catalog.db` to be migrated (W1) before `--apply`** — back it up
> first. See **F2** for the exact-attribution upgrade.

### F2. Exact session↔edit attribution from PixInsight WBPP logs (backfills W8) — ✅ DONE
> Shipped 2026-07-04. New `darkroom/wbpplog.py` (read-only, astropy-free):
> `parse_log_nights(log)` → set of imaging nights from a run's `Light_*` frame
> refs (basename timestamp → noon-rule night); `collect_runs(target_dir)` →
> per-run `RunEvidence(run_dir, edit_date, nights, has_export)` for every folder
> holding a `logs/` dir. `procscan.classify_target/session` now attribute a night
> from logs first (in a has-export run → processed; else in_progress) and
> **exclude logged runs' subtrees from the date-bound pools** so a logged edit
> can't over-attribute an un-logged night (the F1 fix). Falls back to F1
> date-bound for targets/nights with no logs. Dry-run tags each row `[log …]` vs
> `[date-bound …]`. Overlapping edits are fine: a night's state is the max over
> every run that used it (many-to-many is W8's concern, not state's). Real dry-run
> shift F1→F2: 75→45 processed, 40→64 in_progress (30 over-attributed sessions
> corrected). Tests: `tests/test_wbpplog.py` (15) + `tests/test_procscan.py`.
> The persisted linkage TABLE is still W8 — F2 only computes attribution at scan
> time; the log parser is the reusable piece W8 will populate from.
- **Why:** F1's date-bound attribution is a heuristic — a single edit that fused
  several nights marks *all* of a target's on-or-before nights processed, which
  over-attributes (e.g. nights shot before an edit but not actually included).
  Confirmed 2026-07-04: WBPP writes a full input manifest to
  `<Target>/_Processed/<date>/…/logs/*.log`, listing **every light sub by its
  original filename** with the ASIAir capture timestamp
  (`Light_M81_M82_180.0s_Bin1_ISO1600_20250326-000039_17.0C_0002.fit`). The M 81
  2025-04-26 edit's log names lights from exactly 4 nights (2025-03-26/27/29/30).
  118 such logs exist in the archive.
- **Do:** parse each log's `Begin calibration of Light frames` section → collect
  `Light_*.fit` filenames → `parse.parse_datetime()` →
  `cataloger.compute_imaging_night()` → match to catalog sessions by
  `(target, night)`. This yields the **exact** set of sessions per edit — the
  retroactive way to populate the **W8** session↔calibration/edit linkage table.
- **Integration:** a precision pass layered over F1 — use log-derived attribution
  where a parseable integration log exists, fall back to F1's date-bound rule
  otherwise. Record the linkage durably (W8 table) so it's not recomputed.
- **Caveats:** log paths are old *staging* paths, not archive paths — irrelevant,
  the filename (target + timestamp) is enough to compute the night. Not every
  `_Processed/` folder has logs; some folders hold many per-run logs — target the
  integration log specifically. A single edit may also combine multiple WBPP runs
  or hand-added frames not in any one log.

### F3. Web UI: show whether a session has matching calibration frames
Queued 2026-07-07 (front-end feedback round). Per session (night row in the
target detail view), indicate whether matching darks/flats/flat-darks exist in
the catalog — the matching logic already exists client-side of the API in
`darkroom/catalog.py` (`find_darks`/`find_flats`/`find_flat_darks`, fed from
`GET /api/calibration-sets`); the webapi aggregate would run the same matchers
server-side and emit e.g. `cal: {darks: true, flats: false, flat_darks: true}`
per night. UI: small indicator on the night row (missing calibration = the
attention state). Design the exposure-tolerance/flat-window parameters to
match `darkroom wbpp`'s defaults so the indicator predicts what WBPP prep
will actually find.

### F4. Scan and match ASIAir guiding logs → per-session guiding conditions
Queued 2026-07-07. ASIAir writes PHD2-style guide logs; scan them (ingest-time
or a backfill pass over the SD-card copies/archive), match log time-ranges to
sessions by imaging night, and store per-session guiding stats (RMS RA/Dec,
worst excursions, guide-star loss events). Surface in the web UI on the night
row / session edit page — "guiding conditions" alongside exposure data, to
explain why a night's subs are soft before processing. Open questions: where
logs live long-term (they are not currently archived by `ingest` — may need an
ingest extension to copy them), schema (new `guiding` columns vs a side
table), and whether to compute stats at scan time or store raw logs.

### F1. Derive processing state by scanning the archive for output artifacts (original spec)
- **Why:** A read-only audit of the live catalog on 2026-07-04 found **all 205
  sessions with a blank `processed_status`** (now `processed_state =
  'unprocessed'` after W1) — yet many targets have almost certainly been
  stacked and/or finished. The real "this is done" signal lives in the
  **archive as files**, not in the DB: the catalog was never told. This feature
  reconciles the catalog to reality by walking the archive and inferring state
  from the presence of output artifacts.
- **Detection heuristics (in priority order):**
  1. **Finished** → a **TIFF** (`.tif`/`.tiff`, case-insensitive) — the final
     exported image. Usually lives in `<Target>/_Processed/` (at any depth
     under it). If there's no `_Processed/` folder, fall back to looking in the
     target folder / known legacy locations (the archive still has pre-canonical
     org that `triage` exists to clean up — reuse/extend its walk if practical).
     → maps to `processed_state = 'processed'`.
  2. **Stacked / in progress** → a **`masterLight*.xisf`** (PixInsight/WBPP
     integration output) present but **no** finished TIFF. Means the subs were
     integrated but post-processing probably isn't done. → see enum note below.
  3. Neither → leave `unprocessed`.
- **Enum tension to resolve first:** W1's `processed_state` is
  `unprocessed`/`processed`/`skipped` — there is **no "stacked/in-progress"
  value**. Decide: (a) add a fourth enum value (e.g. `stacked` or
  `in_progress`) — cleanest, but touches the W1 migration, `set_processed_state`
  validation, `PROCESSED_STATES`, the picker (`needs_processing` — is a stacked
  night still a candidate? probably yes, it's not finished), and any UI status
  chips; or (b) record "stacked" as a separate boolean/flag or a note and leave
  the enum ternary. Recommend (a) — it's a genuine pipeline state and the whole
  point of W1 was to stop overloading one field.
- **Where it writes:** a `catalog`-native command (e.g.
  `darkroom catalog scan-processed --archive <path> [--dry-run]`) that sets
  `processed_state` (+ `processed_path` = the `_Processed/<date>` or artifact
  dir, + `processed_date` from the folder name or newest artifact mtime) via the
  W4 `update_session_fields` / `set_processed_state` API. A `--dry-run` that
  prints proposed transitions is essential given it's a bulk reconcile over 205
  rows. (Could instead live in `triage` as a check+action, mirroring U2's
  "extend triage vs catalog-native" decision — but this writes the catalog, so
  catalog-native keeps triage's "never writes the catalog" boundary intact.)
- **Caveats / design notes:**
  - **Granularity mismatch:** `finish` writes `_Processed/<date>/` **per
    target**, not per session, and marks *every* session under that WBPP target
    processed. A target-level TIFF therefore can't by itself say *which* nights
    it used — decide whether a found artifact marks all of the target's sessions
    processed (matches current `finish` semantics) or needs finer attribution.
  - Don't mistake WBPP **working** dirs (`~/WBPP/...`, transient symlink trees)
    for archive artifacts — scan the archive root only.
  - `master*.xisf` also covers `masterDark`/`masterFlat`/`masterBias`
    (calibration) — match **`masterLight`** specifically, not bare `master`.
  - Idempotent + re-runnable; safe to run repeatedly as processing progresses
    (unprocessed → stacked → processed is monotonic, but a re-run shouldn't
    downgrade a hand-set `skipped`).

---

## Suggested order for a future session
1. **B1 + B2** (finish + flat-darks) — silent data-pipeline failures, with tests. ✅ DONE
2. **R6 + W5/W6/W7** schema+helper groundwork (move name helpers, WAL, indexes,
   timestamps) — unblocks the web work and B4. ✅ DONE
3. **B4** (reuse `_parse_coords`), **B3** (confirm `01_` vs `04_`), **B5** (after
   verifying intended master/raw behaviour). ✅ DONE — B6 (doc-wide `04_`→`01_`
   rename) folded in alongside B3 at the user's request.
4. **U1** wbpp interactive picker — biggest daily-use friction, small scope. ✅ DONE 2026-07-04
5. **W1/W2/W3/W4** the real web-UI data-model + API prep. ✅ DONE 2026-07-04.
6. **F1** archive-artifact processing-state scan — ✅ DONE 2026-07-04
   (`catalog scan-processed`; 4-state enum; date-bound + dry-run). **F2** exact
   attribution from WBPP logs — ✅ DONE. Live catalog migrated to W1/W2/W3 schema
   + `scan-processed --apply` reconcile run — ✅ DONE 2026-07-05.
7. **W9** ← **IN PROGRESS.** Phases 1+3 deployed 2026-07-05; phase 2 (edit UI)
   built 2026-07-06 pending commit/deploy. Remaining: nightly NAS backup +
   dev-snapshot helper (one task), then phase 4 (remove datasette). See the
   W9 item for the full sketch.
8. **U2/U3** filter cleanup queue + interactive ingest review (U2 is a natural
   second UI view on the W9 app; U3 benefits from U1's picker helpers).
9. **R1–R5, B7** cleanup as capacity allows. Litestream (continuous DB
   replication) also lands here as an optional upgrade over the nightly backup.
</content>
</invoke>
