# darkroom — Backlog

Captured 2026-06-30 from a whole-codebase review + a web-UI readiness assessment.
Line numbers are accurate as of commit `5c8936d`; re-grep before editing if the
tree has moved on. Severity: **P1** = correctness, act first · **P2** = minor /
docs · **R** = refactor · **W** = web-UI prep · **U** = CLI UX · **F** =
features · **S** = observation sites / conditions · **M** = mosaics.

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

### B11. `wbpp` symlinks every dark master at every temperature — ✅ FIXED

> Fixed 2026-07-29. `find_darks` now takes `temperature_c` + `temp_tolerance`
> and returns only sets within ±`temp_tolerance` (default 3.0C,
> `catalog.DEFAULT_DARK_TEMP_TOLERANCE`), ranked nearest-first via
> `dark_temp_sort_key`; `_build_night` symlinks exactly one master instead of
> looping the whole ladder. Exposed as `--dark-temp-tolerance DEGREES`.
> Passing `temperature_c=None` keeps the pre-B11 behaviour, so callers that
> just want "what darks exist at this gain/exposure" are unaffected.
>
> **The two-regime plan below (exact match for the cooled camera, nearest for
> the uncooled one) was dropped after checking the data.** Session
> `temperature_c` is the raw `CCD-TEMP` of the session's *first* frame
> (`cataloger.py`), captured while the sensor may still be settling, whereas
> calibration sets round to the nearest degree (`scanner.py`). That leaves 13
> of 111 ZWOASI585MCPro sessions on values like `-19.5`, `-16.5`, `-15.0` that
> match no master exactly — so exact matching would have silently dropped them
> to zero darks. One nearest-within-tolerance rule covers both cameras and
> needs no cooled/uncooled camera registry, which the codebase doesn't have.
>
> Equidistant picks (`dark_temp_ties`) print a warning naming both candidates
> rather than taking backend row order — the failure mode B12 had. Empty
> `Darks/` now explains itself: "nearest master is 15C, 5C away; use
> --dark-temp-tolerance 5 to accept it" vs. "no darks found at this
> gain/exposure" — different problems with different fixes.
>
> `find_bias` was deliberately left alone: all 6 live bias masters have
> `temperature_c` NULL and there is exactly one per (camera, gain), so the
> multi-symlink bug cannot fire and adding temperature matching would only
> risk breaking a working path.
>
> Verified on live data, not just tests: M 42 2023-11-22 (Canon6D, 17.0C)
> previously drew all five masters from the 15/20/25/30/35C ladder and now
> links only `masterDark_180s_ISO1600_15C.xisf`.
> Tests: `tests/test_catalog.py` (+11), `tests/test_wbpp_finish.py` (+4),
> `tests/test_client_server.py` (+1 HTTP-parity, since the filter is
> client-side and would `KeyError` if the API stopped serialising
> `temperature_c`). Suite 861 → 877.
>
> **Coverage at ±3 on the live catalog** — of the 201 sessions that have any
> master at their gain/exposure, 146 now match one and 55 fall outside the
> window (Canon6D 44/94, ZWOASI585MCPro 102/107). That residue is a *darks
> library* gap, not a matching bug: most Canon combos are single-rung
> (`ISO800` at 5/60/120/180s has only a 25C master; `ISO1600` at 2s/5s only
> 10C). Jonathan is shooting a −15C ZWO set, which takes that camera from
> 102/107 to 105/107 and creates the first real equidistant tie (a −17.5C
> session between −20C and −15C), exercising the new warning.

Reported by Jonathan 2026-07-29: when multiple science-dark masters exist, all
of them get symlinked into the WBPP working folder instead of the one matching
the session.

- **Where:** `darkroom/catalog.py:25` (`find_darks`), `darkroom/prep.py:131-145`
  (the Darks loop in `_build_night`). Same shape in `find_bias` /
  `catalog.py:34` and the Bias loop at `prep.py:149-163`.
- **Problem:** `find_darks` matches on **camera + gain + exposure only** — it
  never looks at temperature, even though `calibration_sets.temperature_c`
  exists and is populated. `_build_night` then loops over *every* returned
  master row and symlinks each one. This is the direct consequence of the
  **B5** fix: that deliberately chose partition-then-fallback over
  break-after-first precisely so it wouldn't drop legitimate masters at other
  temperatures — correct at the time, but it means WBPP is handed the whole
  temperature ladder.
- **Live data confirms it** (queried 2026-07-29, 581 dark sets / 60 masters):

  | Camera | (exp, gain) | masters that all get symlinked together |
  |---|---|---|
  | ZWOASI585MCPro | 180s, gain200 | 2 — `-10C`, `-20C` |
  | ZWOASI585MCPro | 120s / 300s, gain200 | 2 each — `-10C`, `-20C` |
  | Canon6D | 180s, ISO1600 | **5** — `15C`, `20C`, `25C`, `30C`, `35C` |
  | Canon6D | 120s / 300s, ISO3200 | 4 each |

- **The fix is not one rule — the two cameras need different matching:**
  - **ZWOASI585MCPro is cooled**: `-10C`/`-20C` are deliberate set-points.
    Exact temperature match is right; a session cooled to −10C must never get
    the −20C master.
  - **Canon6D is uncooled**: the `15/20/25/30/35C` ladder brackets ambient
    temperature, which varies continuously. Exact match will usually find
    nothing — this needs **nearest temperature within a tolerance**, the
    standard DSLR dark-matching approach. Pick the tolerance so a session at
    22C lands on the 20C master, not on nothing.
- **Both sides have the data:** all 230 catalog sessions have a non-NULL
  `temperature_c`, and 55 of 60 dark masters do. Handle the 5 NULL-temperature
  masters explicitly — suggest treating NULL as "matches anything, lowest
  priority" so an untemperatured master is a fallback rather than a silent
  miss.
- **Do:** add a `temperature_c` parameter to `find_darks` (and `find_bias`)
  with nearest-within-tolerance semantics, then have `_build_night` symlink
  only the single best-matching master. Keep B5's multi-row fallback for the
  raw-subs path. Mirror the tolerance parameter on the CLI the way
  `--flat-window` already exposes flat date proximity, so it's tunable without
  a code change.
- **Related:** `find_flat_darks` (`catalog.py:60`) matches camera + exposure
  ±10% + date and likewise ignores temperature — same latent issue, lower
  impact since flat darks are short and less temperature-sensitive.
- **Tests:** cover both regimes — a cooled camera with two exact set-points
  (assert only the matching one is symlinked) and an uncooled ladder where the
  session temperature falls between rungs (assert nearest wins, and that a
  session outside the tolerance gets none rather than all).
  

### B12. `wbpp` prefers the previous night's flats over the morning-after set — ✅ FIXED

> Fixed 2026-07-29 (`41ed0bc`). New `catalog.flat_sort_key` replaces the
> proximity sort: sets from the session's own run rank first — offset **+1**
> (following morning, the normal workflow) ahead of **0** (that evening, which
> happens when filters are changed mid-session) — then everything else in the
> window by proximity, preferring the later date on a tie. The `--flat-window`
> (±3 days) is unchanged; only the ordering within it. `prep.py:_resolve_flat`
> now prints each candidate's signed offset and labels the morning-after one
> (`_flat_offset_label`), replacing an unexplained `← closest`, so a
> mid-session filter change shows both sets side by side and the default is an
> informed override rather than a guess. One existing test asserted the old
> ordering and was updated deliberately, with the rule in its docstring.
> Tests: `tests/test_catalog.py` (+6, incl. the real NGC 281 shape). Suite 861.
> Verified on the live catalog before and after, then confirmed by Jonathan
> running `darkroom wbpp` for real.

- Reported by Jonathan 2026-07-29, who had noticed it on earlier wbpp runs
  ("the suggested matching flats is often the wrong one") — found again while
  checking what the first real ingest run had produced.
- **Where:** `darkroom/catalog.py` (`find_flats`), `darkroom/prep.py`
  (`_resolve_flat`, which shows the list and auto-selects `[0]` when
  non-interactive).
- **Problem:** `find_flats` sorted by **absolute** date distance, so `-1` and
  `+1` day tied and the winner was whatever order the backend happened to
  return. A session on night N was therefore routinely handed the *previous*
  night's flats instead of the ones shot the morning after.
- **Why it matters:** those are different runs under a different sky, often
  with very different flat exposures — on the live NGC 281 data the mismatched
  pair was **0.31s vs 0.05s**. Not a cosmetic tie-break.
- **The convention already existed elsewhere:** `parse.py`'s flat-morning
  helpers, `ingest.infer_flat_filter` and `catalog.find_flat_darks` all use a
  directional `0..+1` window (and see **B2**, which fixed the flat-dark half of
  the same idea). `find_flats` was the only matcher ranking symmetrically.
- **Jonathan's workflow** (stated 2026-07-29): flats are normally shot the
  morning after. The exception is a dark-site trip with a mid-session filter
  change, where he may shoot flats both before and after the change — which is
  why offset `0` has to stay a valid in-run match rather than being excluded.

### B13. `wbpp` takes a whole night's dark params from `sessions[0]` — ✅ DONE

Found 2026-07-29 while verifying **B11** against live data, not reported.

**Fixed 2026-09-02.** Decision: split the night, don't stack dark sets.
`prep.build_wbpp_sessions` groups a night's rows by `_dark_key`
(camera, gain, exposure) and builds one `SESSION_N` per group; `_build_night`
raises if handed a mixed group, and warns when a group's temperatures spread
wider than `--dark-temp-tolerance` (F5's territory). Splitting is the right
altitude because WBPP keys darks on exposure but *not* gain, so symlinking two
same-exposure masters into one `Darks/` cannot work, while a second `SESSION_N`
costs nothing — WBPP integrates across session dirs anyway. Live scale at fix
time: 9 of 235 nights are multi-session, 4 of those mixed (1 gain, 3 exposure),
and one of the mixed ones — NGC 281 2026-07-26, L-Extreme 180s + L-Synergy
300s — was still unprocessed and about to be prepped wrong.

- **Where:** `darkroom/prep.py:_build_night` — `s0 = sessions[0]`, then camera,
  gain, exposure *and now temperature* for both Darks and Bias come from that
  one row. The comment claims "all sessions same night share params".
- **They don't.** IC 1805 2023-12-14 has three sessions in one SESSION_N:
  L-Extreme at ISO3200/6.0C, L-Pro at **ISO1600**/5.0C, and an unfiltered one
  at ISO3200/8.0C. The night gets ISO3200 darks; the L-Pro lights are
  calibrated with darks from a different ISO.
- **Scale (live catalog):** 10 of 220 nights have >1 session; of those, 1 has
  mixed gain, 2 have mixed exposure, and **0 have a temperature spread >3C**.
  So B11's temperature dimension is safe under this assumption — it's gain and
  exposure that actually diverge.
- **Why it's still open:** the fix isn't just "use each session's params" —
  WBPP's `Darks/` folder is per-SESSION_N, shared by every filter in that
  night, so supporting mixed gain means either splitting the night into
  separate SESSION_N dirs per (gain, exposure) or symlinking multiple dark
  sets and letting WBPP's own frame matching sort it out. That's a design call,
  not a patch. Low frequency (1 night in 220), so it can wait — but it should
  at minimum *warn* when a night's sessions disagree, rather than silently
  using row zero.


### B14. Session `exposure_sec`/`ra_deg`/`dec_deg`/`gain`/`temperature_c` come from directory order, not the chronologically-first frame — ✅ FIXED
> Fixed 2026-08-29. Both `scanner.py:_scan_lights` and
> `cataloger.py:SessionAnalyzer.analyze_sessions` now pick the representative
> frame by `min(frames, key=parse_date_obs)` (earliest `DATE-OBS`) instead of
> `frames[0]` (filename sort order). Regression tests in both `test_scanner.py`
> and `test_cataloger.py` reproduce the SH2-101 mixed-exposure scenario (180s
> sorts before 300s lexically but was captured later) and assert metadata comes
> from the chronologically-first frame.

Found 2026-08-29 fixing up SH2-101 2026-07-19 (F8/U4's originating incident).
Same family as **B13** (`sessions[0]` trusted instead of the actual
per-session values) — different code, worth its own entry.

- **Where:** `darkroom/scanner.py:129` (`_scan_lights`, the path `ingest`
  actually uses) and `darkroom/cataloger.py` `SessionAnalyzer.analyze_sessions`
  (~857-878, used by `scan-lights`). Both do `first_meta = frames[0]` /
  `first = frames[0]`, where `frames` comes from `parse.fits_files()` —
  plain `sorted()` on **filename string**, not `DATE-OBS`.
- **`scanner.py` already half-knows this.** The comment right above it reads:
  *"Not first_meta: `frames` is in directory-walk order, not chronological, so
  the span has to be derived from all of them (`compute_session_span` sorts by
  DATE-OBS itself)"* — correctly fixing `start_utc`/`end_utc`, then two lines
  later using the same unsorted `first_meta` for `exposure_sec`, `ra_deg`,
  `dec_deg`, `gain`, `temperature_c`, `camera`, `focal_length` anyway.
- **When it bites:** any session folder where filename-sort order diverges
  from capture order — the concrete trigger is mixed exposures in one folder.
  SH2-101 2026-07-19's `L-Synergy` folder had 5×300s then 87×180s captured in
  that order, but `"...180.0s..."` sorts before `"...300.0s..."` lexically
  (`'1' < '3'`), so `frames[0]` was a 180s frame from *after* the mid-session
  mis-slew — the catalog's `dec_deg` (34.10611°) reflected the wrong framing
  from the moment `ingest commit` first wrote the row, not just after the
  later manual fixup. Also reachable without any mis-slew: a session that
  changed gain, temperature, or exposure partway through (autofocus-driven
  exposure change, e.g.) picks whichever setting sorts first alphabetically,
  not whichever the mount was actually doing at the start of the night.
- **Fix:** pick the representative frame by `min(frames, key=lambda f:
  parse_date_obs(f["date_obs"]) or datetime.max)` (mirrors what
  `compute_session_span` already does internally) instead of `frames[0]`, in
  both call sites. Cheap — `date_obs` is already being read for the span calc
  right next to it.
- **Relevant to F8:** a `rescan-archive` divergence check that recomputes
  `ra_deg`/`dec_deg` for comparison must not reimplement this same bug when
  computing "what it should be" — fix this first, or the rescan tool will
  confidently confirm a still-wrong value on the next mixed-exposure night.

### B16. Archive-side scan takes `site_lat`/`site_lon` from one frame; ingest takes the modal value — ✅ FIXED
> Shipped 2026-09-02. `sites.session_site(positions, label)` is now the one
> call — modal position plus the stderr disagreement warning — used by
> `SessionAnalyzer.analyze_sessions`, `scanner._scan_lights` and
> `catalog_cli._extract_site` (`backfill-sites`), which had each spelled the
> `modal_site` + `describe_disagreement` pair out by hand or, in the archive
> scan's case, not at all. Tests:
> `tests/test_sites.py::TestSessionSite`,
> `tests/test_cataloger.py::TestAnalyzeSessionsSiteCoords::test_modal_site_coords_not_first_frame`.
> Original entry follows.
>
> Filed 2026-09-01 from the `/simplify` reuse pass. Decision (Jonathan,
> 2026-09-01): a site does not change mid-session, so the two scans must
> agree — the modal value is the right one, everywhere.

- **Where:** `cataloger.py:994-995` (`SessionAnalyzer.analyze_sessions`,
  `first.get("site_lat")`) vs `scanner.py:95-108` (`_session_site`, which
  runs `sites.modal_site` over every frame and warns on disagreement).
- **Problem:** `scan-all` and `rescan-archive` go through `analyze_sessions`,
  `ingest` goes through `_session_site`. `modal_site` exists precisely because
  the ASIAir's first frames can carry a confidently wrong WiFi-geolocated
  position before the phone GPS settles (see `project_asiair_site_coordinates`
  memory). Ingest resolves that; an archive rescan of the same folder reads
  straight off the chronologically-first frame — the frame *most* likely to
  hold the wrong fix — and F8 would then propose "correcting" the good value
  back to the bad one (`site_lat`/`site_lon` are not in `_CHANGE_FIELDS`
  today, so this is latent rather than live, but `scan-all` on a fresh
  catalog gets the bad value outright).
- **Do:** have `analyze_sessions` call `sites.modal_site` over the night's
  frames (lift `_session_site` out of `scanner.py` into `sites.py` or
  `cataloger.py` so both scans share it, warnings included). Behaviour
  change on rescan only for sessions with disagreeing frames — that is the
  point. Test: a night whose first frame has an outlier SITELAT yields the
  modal value from both scan paths.

### B17. `ingest commit` computes `total_integration_sec` as `frame_count × exposure_sec`; the archive scan sums per frame — ✅ DONE 2026-09-02
> Filed 2026-09-01 from the `/simplify` altitude pass. Decision (Jonathan,
> 2026-09-01): match the archive scan — the per-frame sum is the truth.
>
> **✅ DONE 2026-09-02.** `cataloger.total_integration_sec(exposures)` is the
> one summing helper; the archive scan (`analyze_sessions`) and the scan-side
> `Session.total_integration_sec` both call it, `build_session_entry` writes
> the sum into the manifest, and `cmd_commit` reads it via
> `ingest._entry_integration_sec` — which falls back to the old product for a
> manifest written before the field existed, so a pre-B17 manifest still
> commits. Tests: a 10 × 120 s + 5 × 60 s night scans as 1500 s and commits as
> 1500 s (`tests/test_scanner.py`, `tests/test_ingest.py`); suite 1284 → 1288.
>
> Not backfilled: the 12 live rows that already disagree stay as they are
> until the next `rescan-archive` proposes them as `safe`-tier updates — which
> it now can, without ingest flipping them back.

- **Where:** `ingest.py:677` (`int(entry["frame_count"] * entry["exposure_sec"])`)
  vs `cataloger.py:991` (`int(sum(f["exposure"] for f in frames))`).
- **Problem:** the product assumes a uniform exposure. A night with a
  mid-run exposure change (B13's mixed-gain night is the same shape) is
  under- or over-counted by ingest, then "corrected" by the next
  `rescan-archive` as a `safe`-tier update — so the number in the catalog
  depends on which command last touched the row. On the 2026-08-30 backup,
  12 of 240 sessions already disagree with the product formula (10 by more
  than 60 s) — those are the archive-scanned mixed-exposure nights.
- **Do:** carry the per-frame sum through the manifest. `scanner.Session`
  already holds every frame's metadata at scan time, so `build_session_entry`
  can emit `total_integration_sec` alongside `frame_count`, and `cmd_commit`
  reads it instead of multiplying. Put the sum in one helper
  (`cataloger.total_integration_sec(exposures)`) that `analyze_sessions` also
  uses. Manifests written before the field exists fall back to the product
  (`entry.get("total_integration_sec")` or the old formula) so `commit` keeps
  working on a pre-existing manifest. Test: a session with 10 × 120 s and
  5 × 60 s frames commits as 1500, not 1800 or 900.

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

### B7. `triage` CSV export uses naive quoting — ✅ FIXED
> Shipped 2026-07-29 (`810e3d6`). `/audit/export.csv` now builds output with
> `csv.writer` over an `io.StringIO` buffer. Media type, `Content-Disposition`
> filename and column order are unchanged — the only behavioural difference is
> correct quoting/escaping. Regression test
> `tests/triage/test_server.py::TestAuditExport::test_quoted_paths_round_trip`
> exports a path containing both a `"` and a `,` and round-trips it back
> through `csv.reader`.

- **Where:** `darkroom/triage/server.py:278-284` (`export.csv`)
- Hand-rolled `"`-wrapping breaks if a path contains a quote. Use the stdlib
  `csv` module. Low priority (localhost single-user tool).

### B8. Integration time always displayed in hours, rounds short subs to 0.0h
- **Where:** `darkroom/catalog_cli.py:39,48` (`catalog list`, `f"{hrs:.1f}h"`),
  `darkroom/templates/catalog/session.html:41` (same `/3600.0` + `h` format).
- Both hardcode seconds→hours with 1 decimal. Fine for typical DSO subs
  (60–300s), but solar/lucky-imaging subs are sub-second (e.g. Sun session
  `Sun_20260708_..._AstronomikL2`: 100µs–900µs exposures) — total integration
  time renders as `0.0h` regardless of frame count, which is useless.
- Fix: scale the unit to the magnitude (s / m / h) instead of hardcoding hours,
  in both places. Noticed 2026-07-09 while fixing that Sun session's filter;
  see also B9 (Sun may not belong under `01_Deep Sky Objects`/this catalog at
  all, which would make this moot for solar specifically but the underlying
  formatting bug is real for any short-sub target).

### B9. Should solar imaging live under `01_Deep Sky Objects` / this catalog? — ✅ DONE
> Decided and executed 2026-07-09: solar carved out to a top-level `07_Sun`
> folder on the archive (confirmed present on the live archive
> `/Volumes/Photography 4TB/Astrophotography/07_Sun`), matching the
> Moon/Milky Way/Comets precedent, and left **out of `astro_catalog.db`
> entirely**. The miscategorized `Sun_20260708_FRA400-07x_ZWOASI585MCPro_
> AstronomikL2` row was deleted from the catalog (by hand at the time — the
> motivating case for W10's delete path); verified 2026-07-29 that no Sun
> sessions remain. No code change was needed: `ingest`/`catalog` only ever
> look under `01_Deep Sky Objects`, so `07_Sun` is simply never scanned.
> B8's unit-scaling bug is still real for any short-sub target and stays open.
- The archive already has separate top-level categories for non-DSO capture —
  `02_Milky Way`, `03_Moon`, `04_Meteors`, `05_Star Trails`, `06_Comets` — none
  of which are cataloged by `darkroom` (catalog is scoped to DSO per
  `CLAUDE.md`). Solar imaging (lucky-imaging-style stacking, sub-second subs,
  totally different WBPP/processing flow than DSO) fits that same pattern
  better than living inside `01_Deep Sky Objects`.
- Raised 2026-07-09 after cataloging a Sun session there by habit. Jonathan is
  leaning toward carving out a `07_Sun` (or similar) top-level folder and
  leaving it out of `astro_catalog.db` entirely, matching Moon/Milky
  Way/Comets precedent — not decided, no action taken yet.
- If decided: needs an archive move (folder rename/relocate) + a catalog
  cleanup (delete or otherwise disposition the now-miscategorized Sun rows,
  e.g. today's `Sun_20260708_FRA400-07x_ZWOASI585MCPro_AstronomikL2`) rather
  than a code change — `darkroom ingest`/`catalog` already only ever look
  under `01_Deep Sky Objects` for DSO, so nothing needs to explicitly reject
  Sun, it would just stop getting scanned/ingested there.

### B15. An identity edit orphaned the session's `session_guiding` row — ✅ FIXED

> Fixed 2026-08-30 (`840605b`). `update_session_fields` now re-keys
> `session_guiding` to the recomputed `session_id` inside the same identity
> change that renames the row.

Found 2026-08-30 while verifying the 12 rename proposals from F8's first live
queue: 11 of 151 guiding rows stopped joining to any session.

- **Cause:** `session_guiding` keys on `session_id` **TEXT** (F4 chose a side
  table so "no guiding data" is simply row-absent), and SQLite has no cascade
  here. W3's anti-orphan guarantee moved `processed_state`/`notes`/
  `created_at` across a rename because they live *on* the renamed row, and U2's
  `pending_renames` was safe because it keys on the numeric `session_row_id`.
  `session_guiding` was the one side table keyed on the text id and never
  wired in.
- **Not F8's bug.** The web edit form has always been able to do this — any
  identity edit orphaned the guiding row silently. F8 merely triggered it 11
  times in one pass, which is what made it visible at all.
- **The data was never lost**, just unreachable: the rows survived pointing at
  session_ids that no longer existed, so the UI showed those 11 sessions as
  having no guiding data.

**Repair (worth recording, because the obvious fix was wrong):** the tempting
move is a surgical SQL remap of old → new `session_id`. It is **not safe
here** — the join key available (`lights_path`) is *not unique*: the legacy
IC 4604 panels share one folder across several nights, so the remap mapped
`IC4604_1-2` 2025-04-27's guiding onto the 2025-04-26 session and only failed
loudly because of a UNIQUE constraint. Rehearsing on a copy caught it.
The correct fix is that `session_guiding` is **derived data** — the PHD2 logs
are the source of truth — so the orphans were deleted and `scan-guiding
--apply` regenerated all 151 rows under current ids. Proof the remap would
have been wrong: the two nights came back **9.58″ and 6.98″**, not one shared
value.

**Generalises to:** anything else keyed on `session_id` text. Today that's
`rescan_proposals` (harmless — a stale proposal is superseded by the next
scan) and `pending_renames` (already safe). Check this list before adding a
new side table; prefer the numeric `sessions.id` as the foreign key.

### B10. Repo hygiene — untracked leftovers in the repo root — ✅ DONE
> Captured 2026-07-12 (whole-app review). `ingest.yaml` (stale manifest) and
> `tmp/` (loose IC 1848 frames) deleted 2026-07-13; `check_missing_object.py`
> kept deliberately as a standalone tool (already tracked, with
> `tests/test_check_missing_object.py`). Closed out 2026-07-29:
> `datto-d-din/` is gone from the working tree — the UI serves the two weights
> it actually needs from `darkroom/webapi/static/fonts/` (`D-DIN.woff2`,
> `D-DIN-Bold.woff2`, alongside Fira Mono + `OFL.txt`), so the loose source
> folder was redundant rather than something to commit. `.gitignore` now
> covers `tmp/` and manifest filenames (`ingest.yaml`, `manifest.yaml`,
> `*.manifest.yaml`) so future working artifacts don't land in the tree again.

---

## R — Refactors

### R1. Consolidate the two calibration-scan implementations — ✅ FIXED
> Shipped 2026-07-29. The threshold really was defined **three** times — an
> earlier note in this file claiming it was down to two was wrong: `scanner.py`
> declared `FLAT_DARK_THRESHOLD_SEC` (no leading underscore) as a *local*
> inside `_scan_calibration`, so an underscore-prefixed grep missed it.
>
> Shared, in `darkroom/parse.py`: `FLAT_DARK_THRESHOLD_SEC = 10.0` and
> `reclassify_flat_dark(frame_type, exposure_sec)`. All three call sites
> (`cataloger.CalibrationCataloger.scan`, `scanner._scan_calibration`,
> `triage/suggest.suggest_calibration_dest`) now import both. `parse.py` was
> chosen over `names.py` because it already hosts `parse_ota`, the same kind of
> classify-a-derived-value work, and it keeps the astropy-free constraint.
>
> **Deliberately NOT shared** — the three paths differ for real reasons, and
> merging them would have silently imposed one side's trust model:
> - *Frame-type inference* (resolving the type before reclassification):
>   `cataloger` checks the `IMAGETYP` header first, then falls back to a
>   folder-name substring (the archive tree's structure varies);
>   `scanner` trusts the literal top-level ASIAir folder (`Flat`/`Dark`/`Bias`)
>   with no header check, because the SD-card layout is fixed; `suggest` does a
>   dict lookup on the folder basename supporting singular+plural. Different
>   amounts of trust in folder names vs headers — this is the step that decides
>   Dark vs FlatDark, so a wrong merge here mis-files calibration and poisons
>   WBPP matching downstream.
> - *Group keying*: `cataloger`'s key includes `folder_path` and always
>   includes temperature (the archive segments flats by `OTA_Camera_Filter/date/`,
>   so the folder proxies for OTA+filter); `scanner`'s includes `ota`/`filter`
>   explicitly and drops temperature for temperature-insensitive types
>   (Flat/FlatDark/Bias). Merging would either lose the archive's folder-based
>   dedup or add meaningless temperature splits to the SD-card scan.
> - *Temperature rounding*: both call bare `round()`; wrapping a stdlib builtin
>   buys no drift protection. (`suggest` doesn't round at all — doesn't need to.)
> - *Filter-from-filename*: was never actually duplicated — all three already
>   called `parse.parse_filter()` with the same header fallback.
>
> **One behavioural change worth knowing:** the shared helper guards
> `exposure_sec is not None`. Previously a Dark with a missing exposure raised
> `TypeError` and aborted the scan; it now stays classified as a science Dark.
> Loud crash → quiet conservative default. Such a frame can't be matched by
> WBPP anyway (dark matching keys on exposure), so it's an improvement, but
> it is a change, not a pure refactor.
>
> Tests: threshold-boundary cases (exactly 10.0s / 9.99s / 10.01s) pinned for
> **all three** paths — `tests/test_parse.py`, `tests/test_cataloger.py`
> (new `TestCalibrationCatalogerFlatDarkThreshold`; there had been no direct
> unit test of `CalibrationCataloger.scan` at all), `tests/test_scanner.py`,
> `tests/triage/test_suggest.py`. Suite 740 → 753.

- `cataloger.CalibrationCataloger.scan` and `scanner._scan_calibration`
  independently re-implement frame-type inference, the flat-dark threshold,
  temp rounding, filter-from-filename, and group keying.
- Extract one shared grouping helper + one threshold constant so the two ingest
  paths can't drift.

### R2. Delete the legacy `cataloger.finish_command` — ✅ FIXED
> Removed `finish_command`, its argparse subparser (`finish`) + dispatch
> branch, and the `TestFinishCommand` tests that only exercised it.
> `mark_processed`, `mark_processed_by_target`, and `_find_latest_processed_date`
> were left in place (still directly unit-tested, not part of this ask) but
> their docstrings no longer reference the deleted function. The live command
> is `finish.py:cmd_finish`.
>
> **Follow-up R2b — ✅ FIXED 2026-07-29:** with `finish_command` gone, those
> three helpers had **no production caller left** — the only references were
> their own defs plus `tests/test_cataloger.py`. `finish.py` writes state via
> `backend.set_processed_state`, so `mark_processed` (which wrote the legacy
> free-text `processed_status` column W1 retired) was dead too. All three
> deleted along with `TestMarkProcessed`, `TestFindLatestProcessedDate` and
> `TestMarkProcessedByTarget` (14 tests, 220 lines; suite 754 → 740).
> `mark_processed_command` — the live `catalog mark` CLI handler, a
> confusingly similar name — is untouched. `re`/`sys` remain in use, so no
> imports were dropped. Verified clean before deleting: `catalog_client.py`,
> `webapi/`, `scripts/`, `deploy/` contain no reference to any of the three.
> The `processed_status` **column** itself still exists on the live DB (W1
> kept it deliberately for migration safety); only the writers are gone.

- `cataloger.py:497-542` (`finish_command`) + its argparse wiring
  (`cataloger.py:1070-1095`, dispatch at `:1109-1110`). The live command is
  `finish.py:cmd_finish`; the cataloger one is reachable only via
  `python -m darkroom.cataloger finish` and builds paths differently
  (`_normalize_target` vs `_target_slug`). ~100 lines of confusable dead surface.
  Remove once nothing references it.

### R3. Unify the two `set_id` builders — ✅ FIXED
> Shipped 2026-09-02. `names.make_cal_set_id` is the single builder (camera
> via `_normalize_camera`, gain via `_format_gain`, so a DSLR set gets the
> `ISO` form from both paths); `ingest.build_cal_entry`,
> `ingest_review.recompute_cal_entry` and `CalibrationCataloger.scan` all call
> it. No backfill, as predicted below. Tests:
> `tests/test_ingest.py::test_make_cal_set_id_dslr_uses_iso`,
> `tests/test_cataloger.py::TestCalSetIdParity`. Original entry follows.
>
> Re-found 2026-09-01 by the `/simplify` reuse pass, still open. Line numbers
> refreshed; the decision is now easy — see below.

- `cataloger.py:1155-1158` (in `CalibrationCataloger.scan`) vs
  `ingest.make_cal_set_id` (`ingest.py:274-285`). They format gain differently
  (`names._format_gain` → `ISO1600` for a DSLR / `200g` for a ZWO, vs the
  literal `{gain}g` regardless of camera). A Canon6D flat set committed by
  `ingest commit` and the same folder read back by `catalog scan-calibration`
  or `rescan-archive` therefore get **two different `set_id`s** for one
  physical set, i.e. a duplicate row rather than an upsert.
- **Which one wins is already decided by the data** (checked on the
  2026-08-30 backup): all 826 Canon6D sets carry the `ISO` form, zero carry
  `Ng`. `ingest` has never written a DSLR set (the 6D is shot via
  BackyardEOS/NINA, not the ASIAir), so `make_cal_set_id` is the outlier and
  the fix is to make it call `_format_gain` — **no backfill needed**. The 8
  `ASCOMCameraDriver` rows with `_Ng_` ids predate the INSTRUME rewrite
  (BLOCKERS #14) and will be re-scanned under `Canon6D`/`ISO` anyway.
- **Do:** move one `make_cal_set_id(frame_type, camera, gain, exposure_sec,
  temperature_c, capture_date)` into `names.py` (it is stdlib-only), call it
  from both `ingest.build_cal_entry`/`ingest_review.recompute_cal_entry` and
  `CalibrationCataloger.scan`. Test: the same group through both paths yields
  one id, for a ZWO gain and for a DSLR ISO (including `ISOAuto` for gain 0).

### R4. Share `_target_slug` — ✅ FIXED
> Shipped 2026-07-29 (`d539a94`, alias cleanup in `ca99ee6`). The two
> definitions were verified byte-identical (`target.replace(" ", "")`) before
> merging, so this is a pure move: `names.target_slug` is now the single
> source, imported by `prep.py` and `finish.py` under its own name. `names.py`
> stays astropy-free (the helper is stdlib-only). Tests:
> `tests/test_names.py::TestTargetSlug`.

- Defined identically in `prep.py:56` and `finish.py:16`. The wbpp↔finish
  handoff depends on them staying identical — co-locate (e.g. in `config.py` or
  a small `names.py`) to remove silent-drift risk.

### R5. Dedup FITS-file collection — ✅ FIXED
> `scan_all_command`'s per-`lights_path` collection now calls `parse.fits_files()`
> (non-recursive — `find_lights_folders` already returns leaf dirs). This also
> excludes ASIAir "_thn" thumbnail `.fit` files from frame_count/
> total_integration_sec, which the old hand-rolled iterdir() didn't — a real
> bug fix, not just a dedup (see `tests/test_cataloger.py::TestScanAllCommandFitsCollection`).
> Two call sites were deliberately left hand-rolled: `find_lights_folders`'s
> per-directory "has any FITS" check (needs a bool off os.walk's own
> `filenames`, not a file collection — see comment at its top), and
> `migrate_archive_command`'s file-move loop (must move `_thn` thumbnails too,
> or they're left behind and `old_abs.rmdir()` fails — see comment there).

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

### R7. The session/calibration query-filter signature is retyped in every layer
> Filed 2026-09-01 from the `/simplify` altitude pass. Jonathan: worth
> addressing sooner rather than later, across all the modules.
>
> **Partly done 2026-09-01** by the webapi `/simplify` pass: `webapi/app.py`
> now declares a `SessionFilters` dataclass (the nine session filters as
> query params via `Depends()`), and `get_sessions` / `get_sessions_count`
> unpack it with `dataclasses.asdict` — two of the eleven session copies
> gone. That dataclass is the seed for step 2 below, but it lives at the
> wrong layer: it should move down to `catalog_db` and be threaded through
> the backends, with the route importing it. Still open: the nine
> `catalog_db`/`catalog_client` session copies, all eight calibration-set
> copies (`get_calibration_sets` still spells its six filters out), and the
> step-1 signature-introspection test.

- **Where (sessions, 9 filters: target/obs_date/session_id/camera/ota/filter/
  date_from/date_to/processed_state, plus limit/offset):** `catalog_db.py`
  `_build_where` :78, `query_sessions` :126, `count_sessions` :161;
  `catalog_client.py` `CatalogBackend.query_sessions`/`count_sessions` :48/:64,
  `LocalBackend` :212/:255, `HttpBackend` :526/:555; `webapi/app.py`
  `SessionFilters` + `get_sessions` :264, `get_sessions_count` :283 (now one
  list, see above). Was **eleven** signatures, each followed by the same
  keyword list forwarded by hand to the next layer; nine remain.
- **Where (calibration sets, 6 filters):** `catalog_db.query_calibration_sets`
  :184; the Protocol, `LocalBackend`, `MemoryCalibrationBackend` and
  `HttpBackend` in `catalog_client.py` (:78/:282/:410/:582); `webapi/app.py`
  `get_calibration_sets` :335. Eight more.
- **Why it matters — the failure is silent in the two places that count.**
  Adding a filter means editing every copy in lockstep, and the layers fail
  differently when one is missed:
  - miss `LocalBackend`/`HttpBackend`: the caller's new keyword raises
    `TypeError` — loud, caught on first use;
  - miss the **FastAPI route**: `HttpBackend` sends the new query parameter,
    FastAPI silently drops parameters it doesn't declare, and the server
    returns the **unfiltered** set. A filter that works against the local
    file and quietly returns everything over the network — which is exactly
    the deployment split we run (Mac ingest → LXC webapi);
  - miss `_build_where`: every layer accepts the keyword and the SQL ignores
    it. Also silent.
  Nothing is inconsistent today; the risk is that the next filter (F11's
  `exposure_set`, a `panel` filter for the mosaic views, `site`) lands in
  ten copies and not the eleventh. Python checks none of this: `Protocol`
  conformance is structural and never enforced at the call, and the route
  parameters are FastAPI's contract, not the backend's.
- **Do (two steps; the first is cheap and closes the silent hole):**
  1. One `SESSION_FILTERS` tuple (and `CALIBRATION_FILTERS`) in `catalog_db`,
     and a test in `tests/test_client_server.py` that introspects the
     signatures of `_build_where`, `query_sessions`, `count_sessions`, both
     backends' methods and both FastAPI routes (`inspect.signature`) and
     asserts each accepts exactly that set. That makes a missed copy a test
     failure instead of an unfiltered response.
  2. Collapse the copies: a `SessionFilters` dataclass built once at the
     edge (route or CLI), threaded through `_build_where` → `query_sessions`/
     `count_sessions` and the backends as one argument; `HttpBackend`
     serialises it with `dataclasses.asdict`, the route rebuilds it from its
     declared query params (the route must still declare them — FastAPI needs
     the names — so the test from step 1 stays). Public call sites keep
     keyword spelling via a thin `**kwargs` shim, so `prep.py`, `catalog_cli`,
     `ui.py` and `procscan` don't change. Behaviour-preserving; verify with
     the existing client/server round-trip tests.
- **Not in scope:** the `ui.py` HTML views call `catalog_db.query_sessions`
  directly with one or two filters — they benefit from step 2 but need no
  edits.
  
### R8. `SessionAnalyzer.analyze_sessions` and `scanner._scan_lights` still each resolve filter and panel by hand
> Filed 2026-09-02 from the `/simplify` altitude pass over `cataloger.py`.
> B16 was one symptom of these two night→session builders drifting; that
> pass shared the site position (`sites.session_site`) and the once-per-frame
> DATE-OBS parse, and left the rest alone on purpose — the two differ in
> grouping (night vs night+filter) and output shape (DB dict vs `Session`
> dataclass), so one function would need an `is_ingest` branch, which is worse
> than the duplication.

- **What is still worth sharing:** two pure helpers over a night's frames —
  filter resolution (filename-first across the frames, header/path
  fallback) and panel resolution (`parse_panel` + `panel_from_dirname`
  fallback). Each is ~6 lines in both `cataloger.py:~960` and
  `scanner.py:~130`.
- **Do:** only when one of them next changes; lift it into `parse.py` at
  that point, the way `calibration_filter` was.

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

### W9. Always-on web API + client/server split + deployment — ✅ DONE

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
> `deploy/darkroom-backup.{sh,service,timer}` — 01:30 Lisbon timer
> (moved from 04:30 on 2026-07-07: the NAS powers down 02:00–08:00),
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
> **Auth-flow review — ✅ RESOLVED 2026-07-13** (`79ac9c9`, pending deploy):
> browser auth is now a real password login, fully decoupled from the API
> bearer token (which stays `/api`-only and untouched). New
> `darkroom/webapi/auth.py` (stdlib-only): scrypt password hash stored as
> `DARKROOM_UI_PASSWORD_HASH` in `/etc/darkroom/env` (generate with
> `python -m darkroom.webapi.passwd`), cookie is a stateless HMAC-signed
> expiry stamp keyed on the hash string itself — so changing the password
> invalidates every browser, and no browser ever holds an API credential.
> The bookmarkable `/login?token=...` is REMOVED (that was the
> history/access-log leak). Extras: per-IP login rate limit (5 failures/min,
> checked before password verification) and `--no-access-log` on the
> systemd unit. Sliding 90-day window unchanged. `scripts/dev-snapshot.sh`
> mints a dev hash (password `dev`) at launch. 28 new tests; suite 593.
> Deploy needs a one-time step: generate the hash on the LXC, add the env
> var, `daemon-reload` + restart; each browser logs in once with the new
> password. **Passkeys considered and deferred**: WebAuthn requires a
> secure context + domain-based RP ID — impossible on
> `http://192.168.2.217:8000`. Becomes feasible if the app ever moves
> behind Tailscale Serve (HTTPS + ts.net domain); recorded as a possible
> future upgrade, not queued.

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

**Server side — `darkroom/webapi/`:**
```
POST   /api/sessions                       → cataloger.upsert_session
POST   /api/calibration-sets               → cataloger.upsert_calibration_set
PATCH  /api/sessions/{session_id}          → catalog_db.update_session_fields   (UI edits + CLI)
POST   /api/sessions/{session_id}/state    → cataloger.set_processed_state
GET    /api/sessions            [+filters] → catalog_db.query_sessions + sites.annotate_sessions (S2)
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
4. **Remove datasette** — ✅ DONE 2026-07-07: dropped `serve.py`, the
   `datasette>=0.65` dep in `pyproject.toml`, the `serve` subcommand in
   `cli.py`, and doc mentions (`CLAUDE.md`, `README.md`, `CHEATSHEET.md`,
   `cataloger.py`). Removing datasette also dropped `python-multipart` as a
   transitive dep — re-added it explicitly since `webapi`/`triage` form
   handling needs it directly. `uv sync` + full test suite (550 passed)
   confirm clean removal.

Depends on: W1–W7 (done). Absorbs W8's decision (persisted linkage vs recompute —
default recompute). Related: U2 (filter cleanup queue) is a natural second UI view.

---

### W10. Edit UI/API can't fix `lights_path`, and there's no way to delete a session — ✅ DONE

> Shipped 2026-07-13 (`1cc9c61`, merged `109940d`): `session_dest_rel` moved to
> `darkroom/names.py` (stays astropy-free; ingest re-imports it) as the single
> source of truth for `lights_path` derivation. `update_session_fields` now
> recomputes `lights_path` on any identity-field change — independent of whether
> the session_id itself changed (a spacing-only target edit renames the folder
> but keeps the slug); a NULL `lights_path` stays NULL. Delete path added:
> `catalog_db.delete_session`, bearer-auth `DELETE /api/sessions/{id}`, and a
> confirm-guarded delete button on the session edit page (redirects to the
> target page, or `/` when it was the target's last session). Catalog row only —
> archive files untouched. 13 new tests; suite 565 passed. Not yet deployed to
> the LXC.

The `/sessions/{session_id}` edit form and `PATCH /api/sessions/{id}` both
correctly recompute `session_id` when an identity field (target/obs_date/
ota/camera/filter) changes (W3's anti-orphan guarantee) — but `lights_path`
is deliberately excluded from `_EDITABLE_FIELDS`/`_EDIT_FIELDS`, so it's left
pointing at the old (pre-correction) folder name. `finish`/`wbpp` resolve
sessions by matching `lights_path` under the archive root, so a stale value
silently breaks that matching until someone notices frames aren't picked up.

Hit 2026-07-09: corrected a Sun session's filter (`L-Synergy` → `AstronomikL2`,
matching an on-disk rename Jonathan had already done) and had to fall back to
raw `curl`/`POST /api/sessions` upsert to also fix `lights_path`, since
neither the edit UI nor the PATCH endpoint exposes it.

Fix: recompute `lights_path` server-side whenever an identity field changes
(mirror the session_id recompute in `update_session_fields`) rather than
exposing it as a raw editable text field — the path is derived from
target/date/ota/camera/filter, so it shouldn't need separate manual input.

Also no delete: there's no `DELETE /api/sessions/{id}` (or UI equivalent), so
removing a miscategorized/duplicate row means SSHing into the LXC and hand-
running SQL against `/var/lib/darkroom/astro_catalog.db` — done manually
2026-07-09 to remove the Sun session once Jonathan moved solar imaging out to
`07_Sun` (see B9). Add a delete path (API route + confirm-guarded UI button)
alongside the `lights_path` fix, scoped the same way (auth-gated, single row
by session_id/id).

Depends on: W9 (done).

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

### U2. Filter-assignment cleanup queue for `NoFilter`/`UnknownFilter` sessions — ✅ DONE

> Shipped 2026-07-15 (`1724e5a`, `6fbb17d`, `a43dec6`), pending deploy to the
> LXC. Three pieces:
> **1. Pending-renames ledger** — the both-sides constraint resolved: the LXC
> has no NAS mount, so identity edits that change `lights_path` record the
> owed folder move in a new `pending_renames` table (one coalesced row per
> session; `old_path` stays pinned to what's on disk across repeated edits;
> editing back to the on-disk identity deletes the row; `delete_session`
> clears it). New Mac-side `darkroom catalog apply-renames --archive PATH
> [--apply]` (dry-run default) fetches the ledger over the API, moves folders,
> prunes emptied dirs (never at/above the archive root), detects already-done
> renames, skips conflicts/missing, and acks. Also closes the W10 gap where
> edits silently orphaned folders. `GET/DELETE /api/pending-renames`.
> **2. `/queue` view** on the W9 app — sessions with filter NULL/`UnknownFilter`
> ("unknown") or a non-`KNOWN_FILTERS` value ("suspicious" — the mosaic-panel-
> in-filter-column rows), unknown-OTA badges, hints (neighbouring same-target
> sessions' filters, Flat sets ±7 days), inline fix via the standard
> `update_session_fields` machinery. `KNOWN_FILTERS` lives in `names.py`.
> **3. Target merge/rename** — `rename_target` = N per-row identity edits (so
> ledger/session_id/lights_path recompute fire per row; same-night panel
> collisions are per-row errors, batch continues); handles normalization-drift
> (`SH2-103`→`Sh2-103`) via raw+normalized matching. `POST /api/targets/rename`
> + Targets section on `/queue` with suggestions (panel suffix `_N-M`,
> duplicated designations, drift) and a manual merge form.
> Verified end-to-end against a live server: login → queue → fix → ledger →
> `apply-renames` dry-run → `--apply` moved folders + acked. Suite 651.
> **Deploy**: restart webapi on the LXC (init_db migrates the live DB —
> creates `pending_renames` at startup), then work the queue in the browser
> and run `apply-renames` on the Mac against the NAS mount afterwards.
> Live-data note (2026-07-15): 92 filter-suspect sessions, 9 panel-name
> filter rows, ~7 dup/suspect targets (`M 82 M 82`, `M 81 M 82`, `NGC 281W`,
> `IC 4604_*`); many legacy `lights_path` values won't exist on disk under
> their recorded names — expect `missing`/`conflict` rows in the first
> `apply-renames` dry run; they stay pending and are harmless.
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

### U3. `darkroom ingest` interactive confirmation mode — ✅ DONE

> Shipped 2026-07-29. New `darkroom/ingest_review.py` (pure helpers stdlib-only,
> `questionary` imported lazily inside the `_prompt_*` functions — same shape as
> `picker.py`); `ingest.py` keeps only a lazy `_run_review` dispatcher, which is
> also what breaks the import cycle (ingest_review imports the manifest builders
> from ingest).
> **Flow**: `review` now walks *every* session and calibration group, not just
> `needs_review` ones (`--flagged-only` restores the old behaviour). Per entry:
> a summary block (optics / filter / frames / status / destination) plus ⚠ lines
> from `entry_issues`, then an accept-or-edit menu that re-displays after each
> edit so the recomputed destination is visible before accepting. Edits offered:
> **target** (autocomplete over catalog targets, pre-filled with
> `_normalize_target(current)` so Enter applies the normalization), **filter**
> (select over `KNOWN_FILTERS`, Flats get their inferred candidates hoisted to
> the top and annotated), **OTA + camera** (select over observed combos, with a
> manual escape hatch). Pick-lists are seeded from the catalog via
> `resolve_backend`, so this works against the remote webapi too; a dead catalog
> degrades to known-kit-only suggestions rather than failing the command.
> **`suggested_action`** lands the cursor on the fix rather than on *Accept*
> when an entry has a problem (unknown filter > unknown OTA > target drift), so
> a clean entry is one Enter and a broken one is harder to wave through than to
> correct. `commit` stays fully non-interactive (CCC/no-TTY constraint intact);
> `review` hard-refuses a non-TTY rather than hanging on `input()`.
> **Two supporting changes**: (1) `KNOWN_FILTERS` consolidated onto
> `names.py` — `ingest.py` had a second, divergent copy; the union adds
> `L-Enhance`/`OmegonHelievo`, which also widens what the U2 web queue accepts.
> (2) `ingest.plan_session_files` extracted from `build_session_entry`, and the
> manifest now lists **every** frame with a per-file `copy` flag (previously
> `existing` entries carried `files: []`). That's what lets review re-derive the
> new/existing/topup verdict after an identity edit changes the session_id —
> without it, retargeting a session off a colliding id left it marked `existing`
> and commit silently skipped it. Manifests written before this change are
> detected and warned about rather than guessed at.
> Tests: `tests/test_ingest_review.py` (77) + `plan_session_files`/`cmd_commit`
> coverage added to `tests/test_ingest.py` (commit had none). Suite 841. Prompts
> themselves verified by pty (pexpect), not covered by the suite — same as U1.
> **No-TTY regression check (2026-07-29)**: re-verified the CCC postflight path
> end to end against synthetic FITS with stdin on `/dev/null` — `scan
> --manifest` and `commit` (both manifest and one-shot `--asiair` forms) run
> clean, midnight-crossing nights stay one session, short darks reclassify to
> FlatDarks, re-run is idempotent, and `darkroom.ingest` imports no
> questionary/prompt_toolkit. Two fixes fell out: `ingest review` under no TTY
> now exits 1 instead of hitting `EOFError` inside `resolve_filter` and
> **silently stamping every flagged entry `NoFilter` with `needs_review`
> cleared** — the old loop treated EOF as a deliberate answer; and
> `existing_catalog_sessions` no longer tracebacks on a catalog file that
> exists without the schema (sqlite creates an empty db on connect, so a
> touched path aborted an otherwise fine unattended ingest).
> **Follow-up 2026-07-29 (server-backed catalog)**: two bugs found while working
> out what the CCC postflight looks like now the catalog lives behind the HTTP
> API. (1) `ingest scan` computed new/existing/topup by reading local SQLite
> directly, never `resolve_backend` — proved by pointing `DARKROOM_CATALOG_URL`
> at a dead port and watching scan succeed while commit failed — so on a
> server-backed machine every already-archived session was reported `new`. New
> `ingest.resolve_catalog_sessions` goes through the backend when a
> `catalog_url` is configured, reads the file directly otherwise (LocalBackend
> ensures the schema, and a scan must stay read-only on the catalog like
> procscan), and on an unreachable server still writes the manifest but warns
> and records `meta.status_verified: false`, which `commit` re-warns about.
> `meta.catalog` deliberately stays a filesystem path (commit feeds it to
> resolve_backend as the offline fallback); the URL goes in a new
> `meta.catalog_url`. (2) The cost of that false `new` was **silent notes
> loss**: commit sends `notes: ""` on every upsert and the ON CONFLICT clause
> protected `processed_state` but not `notes`. Now
> `COALESCE(NULLIF(excluded.notes, ''), sessions.notes)` — a real note still
> wins, a blank one preserves — matching the convention `set_processed_state`
> already followed. One fix covers both backends since `webapi/app.py` calls
> the same `cataloger.upsert_session`. Verified against a live uvicorn: first
> scan `new` → commit → note added over the API → re-scan reports `existing`
> → re-commit leaves the note and processed_state intact, no local db created.
> **Not done**: `scan` still writes the raw ASIAir target name; normalization
> only happens if you run `review`. Making scan normalize would change ingest
> behaviour for the no-TTY path, which is a separate call.

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

### U4. `scan-lights`/`scan-calibration` don't reach the live catalog when `catalog_url` is configured — ✅ DONE

> Fixed 2026-08-29. `catalog_cli.py`: added `catalog_url_flag`
> (`--catalog-url`/`--api-token`) as a parent parser to both `scan-lights`
> and `scan-calibration`; `_scan_lights_run`/`_scan_calibration_run` now
> build a `CatalogBackend` via `resolve_backend` and pass it to
> `scan_all_command`/`scan_calibration_command`. `cataloger.py`: both
> commands accept an optional `backend` kwarg — when provided they call
> `backend.upsert_session`/`backend.upsert_calibration_set` instead of the
> raw `upsert_*` functions against `Path(args.db)`; the legacy
> `python -m darkroom.cataloger` path (no backend) is preserved. The
> shared `catalog_url_flag` definition was moved up near `catalog_flag`
> and the duplicate removed.

Filed 2026-08-29, out of a manual catalog fixup (SH2-101 2026-07-19 mis-slew:
deleting the 87 off-target subs needed `frame_count`/`total_integration_sec`/
`start_utc`/`end_utc`/`dec_deg` corrected on the *live* session row, and there
was no clean way to do it).

- **Where:** `catalog_cli.py` — `scan-lights`/`scan-calibration` wired to
  `_resolve_db` → `config.resolve_catalog` (raw sqlite path only), while
  `scan-guiding`/`apply-renames`/`sites`/`backfill-sites` all took
  `--catalog-url`/`--api-token` and went through `catalog_client.resolve_backend`
  (local file *or* webapi, per W9). `cataloger.scan_all_command`/
  `scan_calibration_command` called `upsert_session`/`upsert_calibration_set`
  directly against `Path(args.db)` — never the backend abstraction.
- **Symptom:** rescanning an archive folder on the Mac to pick up a manual
  disk change (deleted/added subs, corrected filenames) silently updated a
  stale local sqlite file (`~/.config/darkroom/astro_catalog.db`, currently
  92KB and untouched since 2026-08-04) while the deployed LXC catalog — the
  one the web UI and everyone else reads — was untouched. No error; it just
  looked like nothing happened.

---

## F — Features

### F1. Derive processing state by scanning the archive for output artifacts — ✅ IMPLEMENTED 
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

### F3. Web UI: show whether a session has matching calibration frames — ✅ DONE
Queued 2026-07-07, shipped 2026-07-30.

> `darkroom/catalog.py:match_session_calibration` is the shared answer to "what
> would `wbpp` find for this session", returning `{darks, flats, flat_darks}`
> with a `status` of `ok` / `missing` / `na` / `unknown`, a label, a `detail`
> line and the matched rows. It orchestrates the existing `find_*` matchers with
> their own defaults — `DEFAULT_FLAT_WINDOW_DAYS` is now shared with
> `wbpp --flat-window` so the two can't drift — and `nearest_dark` was lifted out
> of `prep._no_darks_note` so the near-miss line ("nearest master is -20C, 4C
> away") is written once and phrased per surface.
>
> `catalog_client.MemoryCalibrationBackend` feeds the matchers a page's worth of
> calibration rows from one query; without it a target page would open one
> SQLite connection per session per frame type. It preserves the caller's row
> order rather than re-deriving `ORDER BY is_master DESC, capture_date, set_id`
> (masters store `capture_date = ""`, so re-sorting means re-deriving SQLite's
> collation). Matching all 231 live sessions takes 45 ms.
>
> UI: a Cal column of D/F/FD chips on each night row (`app.js:calCell`), tooltips
> carrying the `detail`, plus a verbose panel on the session page naming the
> exact sets. `/` deliberately doesn't compute it — the overview shows no
> calibration state.
>
> **Two things it is not.** It has no disk check: `_build_night` only uses a set
> whose `folder_path` exists, and the webapi host has no NAS mount, so this is
> catalog-level truth and says so in the footnote. And it matches **per session**
> where `_build_night` takes dark params from `sessions[0]` for the whole night
> — that's **B13**, and per-session is where B13 lands anyway.
>
> Ground-truthed against `darkroom wbpp` on two real M 101 nights: the
> fully-matched night's chips agreed with the symlink counts and the flat pick
> (2026-02-26, +1 morning after), and the missing-darks night produced a `wbpp`
> note character-identical to the chip's tooltip.
>
> Note for **F5**: `na` never fires on the live catalog — every camera has some
> sets of every type, including ZWOASI585MCPro flat darks (49 sessions match,
> 82 miss). The "ZWO doesn't need flat darks" note in CLAUDE.md isn't what the
> catalog says.
>
> Chip styling was revised the same day (see the `.caldot` block): `missing`
> dropped `--safelight`, because on that row the colour means *doneness* (the
> grease-pencil mark, the PROCESSED label) and a missing flat isn't a point on
> that scale. `missing` and `na` deliberately look alike — both mean the frames
> won't be in the WBPP input and the same manual step follows (shoot them, or
> match and copy them in by hand), so only `ok` needs to stand apart. Don't
> "fix" that contrast.

Original entry: per session (night row in the
target detail view), indicate whether matching darks/flats/flat-darks exist in
the catalog — the matching logic already exists client-side of the API in
`darkroom/catalog.py` (`find_darks`/`find_flats`/`find_flat_darks`, fed from
`GET /api/calibration-sets`); the webapi aggregate would run the same matchers
server-side and emit e.g. `cal: {darks: true, flats: false, flat_darks: true}`
per night. UI: small indicator on the night row (missing calibration = the
attention state). Design the exposure-tolerance/flat-window parameters to
match `darkroom wbpp`'s defaults so the indicator predicts what WBPP prep
will actually find.

### F4. Scan and match ASIAir guiding logs → per-session guiding conditions — ✅ DONE
Queued 2026-07-07, shipped 2026-07-30.

> **Matched by time, never by target name.** The open question "match by imaging
> night" resolved into intersecting each session's UTC wall span with the guide
> segments. Log target names are messy (`NGC7000` vs `NGC 7000`, 147 `FOV`
> framing blocks, `Sun`/`Moon`) *and* sometimes simply wrong at acquisition —
> filenames/FITS/catalog were corrected afterwards, the logs weren't. The
> catalog is the truth; a log is trusted only for *when* it was guiding.
>
> Shape: `guidelog.py` parses (pure, stdlib-only — calibration blocks with their
> different column header, `DROP` rows, non-zero `ErrorCode`, per-segment pixel
> scale, truncated final segments, local→UTC via `ZoneInfo`); `guidescan.py`
> matches and reduces (`scan()`/`apply()`, mirroring `procscan`); `logs.py`
> archives the files to `<archive>/00_Logs/ASIAir/`. Storage is the side table
> `session_guiding`, one row per session — "no guiding data" is simply
> row-absent, which is the common case (141 of 231 sessions).
>
> **Windowing is what makes the numbers mean anything.** Whole-log RMS is
> garbage because framing/plate-solving/slews sit inside guiding segments:
> IC 5070 2026-07-10 reads 84.39" whole-log, **0.97"** windowed. Regression
> anchors, all reproduced on real data: NGC 281 2026-07-28 **0.92"**,
> IC 5070 **0.97"**, Sadr 2025-07-29 **2.57"**, M 45 2025-09-25 **15.04"**,
> M 33 **2.35"**. M 45's number is real signal, not a bug — same night, M 31
> 1.67" and M 33 2.28"; that night genuinely guided at ~2px. Anything above
> ~50" means the window filter is broken.
>
> Three rules the implementation turns on, each of which was got wrong first:
> settle exclusion is **per segment** (dither offsets are relative to that
> segment's start); RMS is **pooled over the surviving rows**, never averaged
> across segments (which would weight a 3-minute segment like a 3-hour one);
> and `guided_sec` is the **union** of overlaps, not the sum, so a duplicated
> log can't push coverage past 1.0.
>
> **A folder is not a session.** Real-data validation caught `backfill-times`
> spanning every light under `lights_path`: M 81 2025-03-25 and 2025-03-28
> share a folder (legacy layout) and both got the same 76-hour window, then
> both swallowed the same guide rows for an identical bogus 49.59". Same for
> IC 4604_1-2 2025-04-26/27 and NGC 7000 2025-07-30/31; it dragged the median
> to 3.96". Fixed by keeping only frames whose `compute_imaging_night` equals
> the session's `obs_date`, skipping rather than falling back when none match.
>
> Settle timeouts are this rig's norm (`Settling failed` 10,706 : 1,350
> `complete`; the Autorun log agrees) — excluded statistically, never surfaced
> as an alarm. `--settle-exclude` makes the window tunable: a good night barely
> moves (NGC 281 0.92" → 0.90" from 15s to 120s), a bad one moves a lot
> (M 45 15.04" → 5.60"), which is why the default stays pinned at 15s — the
> anchors above are calibrated to it.
>
> UI: a Guiding column on the night row (`app.js:guidingCell`) showing total RMS
> — good <1", fair 1–2", poor >2" — em-dash where no log covers the night, which
> means *not measured*, never *guided badly*. Tooltip carries RA/Dec, peak, p95,
> coverage %, star-loss/dropped counts and the source logs, and says so outright
> when coverage < 80%. Verbose panel on the session page. `poor` is the one
> place the `.caldot` "don't borrow `--safelight`" rule is deliberately broken:
> a 3" night is exactly what the column exists to make you look at.
>
> **Spike marker (added 2026-07-30, after F4 shipped).** RMS squares each error,
> so a handful of wrecked subs drags an otherwise excellent night into `poor`.
> When `rms_total >= 2 * p95` the UI appends a dim ▲ and the tooltip names p95 as
> the typical frame (`ui.py:_is_spike_dominated`); the value and its colour band
> are untouched — this is presentation only, nothing stored changed. The ratio is
> what separates the two cases, measured across all 141 live sessions: clean
> nights ≤1.0, a *uniformly* bad night ~1.2 (M 45 2025-09-22, 35.30"/28.30" —
> correctly NOT marked), spike-dominated nights 4–12 (NGC 6888 2026-07-20,
> 19.18"/2.11"). 24 of 141 marked; highest unmarked ratio is 1.98, so the
> threshold sits in a real gap rather than through a cluster. Jonathan's framing
> when he asked for it: *don't fix it just to make the numbers look better* — the
> point is not mislabelling a good night, not flattering a bad one.
>
> **Deferred, filed here rather than done:**
> - **Autorun log parsing** — autofocus runs, focuser temperature drift,
>   `Download failed` events. The logs are archived now, parseable later.
> - **Per-frame windowing** instead of the session envelope — now filed
>   separately as **F7**, which carries the full scoping. Measured better
>   (M 45 15.70" → 12.65", M 31 2.26" → 1.67") because it excludes inter-exposure
>   settle, but it needs every frame's `DATE-OBS` at scan time, which would tie
>   the scan to a mounted archive (the LXC has none). Envelope + settle exclusion
>   is the right default; revisit only if the numbers must be defensible to a
>   decimal.
>
>   **The case that would justify it, measured 2026-07-30 on NGC 6888
>   2026-07-20:** culling bad subs currently changes *nothing*. `scan-guiding`
>   never opens a FITS file, `backfill-times` only considers sessions whose
>   `start_utc IS NULL`, and — the real blocker — the envelope runs
>   first-frame-start → last-frame-end, so deleting *interior* subs cannot shrink
>   it (verified: span stayed `23:15:10 → 03:46:26` after culling 10 of 48).
>   That night reads 19.18" on the envelope, 18.05" per-frame over all 48 subs,
>   and **1.10" per-frame over the 38 subs worth keeping**. So per-frame
>   windowing is what makes "cull the bad subs and rescore" work at all. Until
>   then the spike marker above carries the practical value: it says *this night
>   has cullable subs* without needing the cull first.
> - **Scale-relative colour bands** — 1" RMS means something different at FRA400
>   (~1.5"/px) than FMA180 (~3.3"/px). Needs a camera pixel-size table the repo
>   doesn't have. Absolute bands are good enough to rank nights.
>
> Incidental, not F4's to fix: M 33's `obs_date` is `2025-09-25` (correct —
> frames run 04:29–06:15 local on the 26th) but its archive folder is named
> `2025-09-26_FMA180_ZWOASI585MCPro`. A triage/W10 concern; worth a grep for
> others.

Original entry: ASIAir writes PHD2-style guide logs; scan them (ingest-time
or a backfill pass over the SD-card copies/archive), match log time-ranges to
sessions by imaging night, and store per-session guiding stats (RMS RA/Dec,
worst excursions, guide-star loss events). Surface in the web UI on the night
row / session edit page — "guiding conditions" alongside exposure data, to
explain why a night's subs are soft before processing. Open questions: where
logs live long-term (they are not currently archived by `ingest` — may need an
ingest extension to copy them), schema (new `guiding` columns vs a side
table), and whether to compute stats at scan time or store raw logs.

### F5. Model session temperature as a *range*, and bracket darks for uncooled cameras

Queued 2026-07-29, out of Jonathan's question while reviewing **B11**: what
happens when an uncooled sensor drifts over the course of a night?

**Builds on B11, does not undo it.** B11 replaced "symlink every master in the
ladder" with "symlink the single nearest". That is unambiguously right for a
cooled camera and right for the pathological case it fixed (five masters
spanning 15–35C, some 18C from the session). This item is about the case B11
does not model: an uncooled session is not *at* a temperature, it spans one.

- **Measured drift on real Canon6D sessions** (CCD-TEMP across every light,
  2026-07-29):

  | Session | catalog temp | first→last | min–max | drift |
  |---|---|---|---|---|
  | SH2-103 2025-07-23 (250f) | 22C | 22→23 | 19–24 | **5C** |
  | SH2-103 2025-07-24 (239f) | 26C | 26→26 | 23–28 | **5C** |
  | M 42 2023-11-22 (194f) | 17C | 13→16 | 12–18 | **6C** |

  Drift exceeds the ±3C default tolerance. One master cannot be correct for the
  whole session.

- **Second, separate defect found while measuring:** `sessions.temperature_c` is
  `frames[0]["temperature"]` (`scanner.py:137`, `cataloger.py:726`), where
  `frames[0]` is first in *file-iteration* order, **not** sorted by `DATE-OBS`.
  On M 42 the catalog stores 17C while the chronologically-first frame is 13C
  (range 12–18). So the stored value is not reliably "the first light" — it is
  an arbitrary frame. This is worth fixing on its own even if the range work is
  never done, because every temperature-keyed decision reads that one scalar.

- **Why bracketing rather than a better scalar:** WBPP does its own per-frame
  dark matching when handed multiple masters. Symlinking the masters that
  *bracket* the session's measured range (e.g. 20C and 25C for a 19–24C night)
  lets WBPP calibrate each frame against the nearer one — strictly better than
  any single choice. The pre-B11 behaviour accidentally resembled this but was
  unbounded and included masters nowhere near the session; a bounded bracket is
  a different thing.

- **Shape of the work:** ingest-side, not matcher-side. Store `temperature_min`
  /`temperature_max` (or percentiles — the tails are single frames while the
  sensor settles, so p05/p95 may match better than raw min/max) alongside the
  existing scalar, which stays as the representative value. `find_darks` then
  grows a range-aware mode returning the bracketing set; `_build_night` keeps
  B11's single-master path when the range is narrow or the camera is cooled.
  Needs a rescan/backfill to populate the new columns for existing sessions.

- **Decide first, before building:** whether this earns its complexity. It only
  matters for the Canon (the ZWO is cooled and its drift is a settling artifact,
  not a trend), and the Canon's coverage is currently limited far more by the
  **darks library** than by the matcher — 44 of 94 Canon sessions match a master
  at ±3C, and most Canon gain/exposure combos have a single-rung ladder. Shoot
  the missing darks first; the bracketing question only becomes interesting once
  there are enough rungs to bracket *between*.

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

### F6. Web UI: home-equivalent hours on the row, not in a tooltip — ✅ DONE
Filed and shipped 2026-07-30, off the back of F3's UI round.

> The target page's Hours column showed **raw** integration hours, with the
> SQM-weighted home-equivalent figure — the one that says how much usable depth a
> night actually bought — reachable only by hovering the gauge. A 2.2h night from
> Santa Susana is worth 20.2h from home and the page said "2.2h".
>
> `app.js:hoursHTML` now renders weighted-first with the raw figure alongside in
> `.rawh` (`--ink-3`, the site-name grey), on both the night row and the rig
> summary. The raw figure appears only when the site's sky quality actually moves
> the number (the 0.05h threshold `gaugeHTML` already used), so home sessions
> still read as a bare `5.5h`. The gauge tooltip, no longer the only place those
> numbers live, drops to zone + range (`deep — 20h+`) on the target page; the
> overview keeps its numbers, since nothing else on that page shows them.
>
> The zone ladder moved into one `ZONES` table that also generates the footnote
> text. Both footnotes previously read `<2h needs data · 5–10h workable · 20h+
> deep` — a 5h floor that doesn't exist in the code, and no `solid` zone at all.
> Thresholds are unchanged; only the labels were wrong.
>
> **Watch the row width.** F3's Cal column and this wider Hours cell both come out
> of the 1fr Site track inside `.wrap`'s 1100px measure; Filter gave back 30px to
> pay for it. "Santa Susana" plus its weight badge still fits, but the longest
> site names ("Mount Pico (Pico Island, Azores)") now ellipsise where they didn't
> pre-F3. If a third column ever lands on this row, widen `.wrap` for the detail
> view rather than squeezing Site again.

### F7. Score guiding per *frame* instead of per session envelope

Filed 2026-07-30, out of F4. **Scoped and decided 2026-08-29 — Option A below.
Ready to build.** Promoted from an F4 deferral bullet because a real question
("if I cull the bad subs and re-run `scan-guiding`, do the numbers update?")
turned out to have a surprising answer: **no, and nothing short of this changes
that.**

- **Why culling does nothing today**, all verified in code: `guidescan.py` never
  opens a FITS file (it reads `sessions.start_utc`/`end_utc` and intersects
  those with the guide logs); `_backfill_times_run` filters to
  `r.get("start_utc") is None`, so it won't recompute a span that already
  exists; and — the real blocker — the envelope runs first-frame-start →
  last-frame-end, so deleting *interior* subs cannot shrink it. Measured on
  NGC 6888 2026-07-20: after culling 10 of 48 subs the span stayed exactly
  `23:15:10 → 03:46:26`.

- **What it would buy**, same session: 19.18" on the envelope, 18.05" per-frame
  over all 48 subs, **1.10" per-frame over the 38 subs worth keeping**. The
  middle number is the honest measure of the change in isolation — most of the
  gain comes from *combining* per-frame windowing with a cull. Earlier
  side-by-sides without a cull were more modest (M 45 15.70" → 12.65",
  M 31 2.26" → 1.67"), which is the fairer expectation for a normal night.

- **The cost, and the design question it forced:** per-frame windows need every
  frame's `DATE-OBS` at scan time, so `scan-guiding` stops being pure
  catalog+logs and starts needing a mounted archive — which **the LXC does not
  have**. Either the scan becomes Mac-only (like `backfill-times`), or per-frame
  intervals get precomputed and stored so the scan stays archive-free (a
  `session_frames` table, or a packed interval blob per session, which would
  also give F5 its per-frame temperature series).

- **Don't do it just for prettier numbers.** Jonathan's standing instruction
  when the spike marker was proposed: the goal is not mislabelling a good night,
  not flattering a bad one. The spike marker (`rms >= 2 * p95`) already flags
  *this night has cullable subs* without any of this machinery, which is most of
  the practical value. Revisit F7 only if culling-then-rescoring becomes a real
  part of the workflow. **Answered 2026-08-29:** it has — two hand-culls now
  (NGC 6888 2026-07-20, SH2-101 2026-08-28), and both left the guiding numbers
  unchanged and wrong. That is what moved this from "scope it" to "build it";
  the constraint still stands as a design rule, not as a reason to defer.

- **Third real case, 2026-08-29 (SH2-101 2026-08-28, `L-Extreme`):**
  `rms_total_arcsec` 87.84″, `p95_arcsec` 1.92″ — a 46× ratio, easily
  spike-dominated (threshold is 2×), and the session page also shows
  `coverage` 0.775 with the "partial log — not the whole night" warning
  (`session.html:81-82`, cutoff `< 0.8`). Jonathan suspected the coverage
  warning itself was wrong — a second midnight-split log file missed, or a
  UTC/local mismatch in the matcher. **Neither, checked against the raw log**
  (`~/02_Astrophotography/01_ASIAir/ASIAIR/log/PHD2_GuideLog_2026-08-28_220037.txt`,
  read directly, plus the live `session_guiding` row over SSH): only one PHD2
  log exists for the night and it's the one recorded in `source_logs`; log
  timestamps (local) and `DATE-OBS` (UTC, +1h for Portugal's August DST) agree
  throughout, calibration starting ~50 min before the first light frame and
  the log's last line landing within a minute of `end_utc` converted to local.
  **The coverage shortfall is real, not mismatched:** `Guiding Ends`/`Begins`
  pairs show a clean 26-minute dark stretch (03:26:33→03:52:21 local) plus,
  either side of it, dozens of guiding segments under a minute — some under
  15 seconds — meaning most of that stretch's guiding time is thrown away by
  the settle-exclude before it ever reaches `guided_sec`. That whole window
  sits exactly inside the frame gap (`#0070`–`#0104`) from the
  forgotten-meridian-flip stall (see **B14**'s entry and the `L-Extreme`
  cleanup). **Worth fixing regardless of F7:** the "partial log" wording reads
  as *log data is missing*, when here the true story is *guiding was actually
  failing for part of the night* — a different, more useful fact the UI
  currently can't distinguish from a genuine matching gap. Per-frame windowing
  (this entry) would fix both at once — a `coverage` computed against the subs
  actually worth keeping stops being dragged down by a period whose frames are
  already gone from the archive.
  - **Correction after per-segment analysis (`guidelog.parse_log` +
    `segment_stats` run directly against the log, not just the `Guiding
    Begins`/`Ends` markers):** the coverage/blackout window and the RMS spike
    are **two separate incidents**, not one. `peak_arcsec` 2937.42″ (49
    arcminutes — essentially a lost star) comes from an isolated 22-minute
    segment at **22:54–23:16 local**, over 4.5 hours *before* the meridian
    stall — and it lines up with the other deleted frame, the single
    tree-obstructed `#0006` (its neighbours place it at ~23:10–23:14 local,
    squarely inside that segment). The meridian-stall window
    (02:49–04:33 local) is well-behaved wherever it has enough rows to score
    at all (1.07″–3.64″ per segment) — its damage is the *coverage* gap (the
    26-minute blackout plus many settle-excluded sub-minute segments), not the
    RMS spike. **Every other segment across the whole night — first to
    last, both sides of both incidents — sits in the 0.7″–1.4″ range**, tight
    and unremarkable, which is the actual answer to "were the guide settings
    off": no. A settings/tuning problem would show up as elevated or
    drifting baseline RMS in the clean segments too, and none of them do,
    including the very first segment before anything went wrong. Both bad
    stretches trace to a named external cause (tree; forgotten meridian-flip
    re-enable), not the guide config.

#### Decision, 2026-08-29: read the archive live, but make the read optional

**Option A — live archive read — with the archive read optional per run rather
than mandatory.** `scan-guiding` grows an `--archive` flag: given one, it
computes per-frame windows; without one, it behaves exactly as it does today.
Nothing is removed from the current capability, the LXC keeps producing
envelope-mode numbers, and a Mac run overwrites them with better ones.

Rejected: the stored `session_frames` table (Option B), and a third option
considered and dropped, deriving per-frame windows from the **Autorun logs**
(they carry per-frame start times — `2025/03/24 21:39:23 Exposure 180.0s image
2#` — in the same `00_Logs/ASIAir/` directory `scan-guiding` already reads, so
they'd be zero-schema and LXC-runnable). Why, in order of weight:

- **The cull loop needs ground truth on what exists *now*.** F7's value is
  almost entirely the 1.10″ number, not the 18.05″ one, and that requires
  knowing which files survive. A live archive read is true by construction. A
  stored table is only true if something refreshes it — and that something is
  **F8**, so Option B isn't "F7 with a cache", it's F7 blocked on F8, and
  shipped before F8 it is the **B14** failure shape with a bigger blast radius.
  The Autorun logs can never see a cull at all: they record what was *shot*.
  They'd deliver the modest improvement and never the large one, which is
  exactly the half that doesn't justify a build. Keep them in mind as a
  cross-check oracle, not a data source.
- **The per-run cost con doesn't hold on this rig.** The live archive is a
  locally attached APFS volume (`/dev/disk9s1 on /Volumes/Photography 4TB`), not
  SMB. Measured 2026-08-29: `fits.getheader` runs **0.5 ms/frame** there, so all
  15,787 light frames in the archive read in **~8 seconds**. Even 10× worse is
  under two minutes for a full rescan. (CLAUDE.md still describes the archive as
  an SMB-mounted NAS — stale; that's where it *was*.)
- **Option B's migration cost was overstated, but its write path wasn't
  priced.** A new side table is one `CREATE TABLE IF NOT EXISTS` in `init_db`,
  exactly like `session_guiding` (`cataloger.py:445`) — no rebuild, no
  retrofit. The real cost is the write path: a backend method on both
  `LocalBackend` and `WebAPIBackend` (`catalog_client.py:143`, `:445`), a
  Pydantic model + POST endpoint (`webapi/app.py:165`), ingest population, a
  one-off backfill, and then F8 to own refresh.

Bank Option B for when **F5** arrives with a second consumer *and* F8 owns the
refresh. At that point `session_frames` is cheap and correct; today it is
neither.

##### Build scope

- **Share the reader with `backfill-times`.** Lift the header loop out of
  `_backfill_times_run` (`catalog_cli.py:545-599`) into a shared
  `read_session_frames(archive, row) -> [(start_utc, end_utc)]`, keeping the
  `compute_imaging_night(...) == obs_date` filter — a `lights_path` folder still
  holds more than one night, and legacy layouts still have two session rows
  sharing one folder. F5 gets its temperature series off the same helper later,
  without a table. This answers Option A's "doesn't help F5" con: the reader is
  shared, only the *storage* isn't.
- **Store `window_mode` (`'envelope'` | `'frame'`) on `session_guiding`.**
  Mandatory, not nice-to-have: frame-mode numbers are not comparable to
  envelope-mode ones — the same caveat `--settle-exclude` already carries — and
  the UI has to be able to say which it is showing. `scan-guiding` should report
  the split (*N envelope, M frame*) rather than silently mixing.
- **Never downgrade silently.** A later envelope-mode run must **refuse** to
  overwrite an existing frame-mode row unless `--force` is passed. Better data
  does not get clobbered by worse data because a cron ran on the LXC.
- **Redefine `coverage` in frame mode** as guided-and-in-frame seconds ÷
  **summed exposure time**, not wall span, or it silently becomes duty cycle and
  stops meaning what the F4 tooltip says it means. `guided_sec` moves with it.
- **Audit trail without the table:** `session_guiding` already has
  `computed_at`; add `frames_used` and `exposure_sec_used`. `sessions` already
  carries `frame_count` and `total_integration_sec` (`cataloger.py:295-296`), so
  staleness becomes a two-integer comparison the UI can make on read — *this RMS
  was computed over 74 frames / 13320s; the session now says 110 / 19800s →
  stale, rescan*. That is the whole audit-trail value Option B was buying with a
  15,787-row table, at four columns on a table that already exists, and it
  *detects* staleness rather than merely recording provenance.
- **Two calibrations to redo after, not assume:** `--settle-exclude` largely
  no-ops in frame mode (dither settle happens between exposures, i.e. outside
  the frame windows), and the spike marker `rms_total >= 2 * p95`
  (`ui.py:_is_spike_dominated`) was tuned on envelope numbers. Re-check both on
  NGC 6888 2026-07-20 and M 45 before trusting the bands.

### F8. `catalog rescan-archive` — diff the archive against the catalog, queue the divergence for review — ✅ DONE

> Shipped 2026-08-30, built in two parallel worktrees and merged to main
> (`9c6eebe` web half, `a98a6b2` scan half, guard fix `c83fbaf`, live-data
> fixes `ee6571f`). **DEPLOYED to the LXC 2026-08-30** — `rescan_proposals`
> created on the live DB (240 sessions / 1028 cal sets / 151 guiding rows all
> unchanged, `integrity_check` ok), `/api/rescan-proposals` returns `[]`, and
> `/rescan` auth-gates like `/queue`. Rollback point:
> `/var/lib/darkroom/backups/astro_catalog-pre-F8-20260830-193601.db`, copied
> to `~/darkroom-backups/` on the Mac.
>
> **The first live dry run found two defects that only real data exposes**
> (243 proposals, 209 of them spurious — fixed in `ee6571f`):
> 1. **`upsert_session` canonicalizes `camera`/`exposure_sec` on write**, so a
>    fresh scan's raw header values are *not* what would be stored. rescan
>    compared raw against canonical and proposed rewriting 209 of 231 sessions
>    from `Canon6D`/`ZWOASI585MCPro` back to the raw INSTRUME spellings.
>    Extracted as `names.normalize_session_fields`, now used by both
>    `upsert_session` and the rescan walk. **`scan-lights` was never affected**
>    — `upsert_session` normalizes regardless of what its caller passes.
> 2. **`make_session_id` only strips whitespace from the target**, while the
>    disk side also applies `_normalize_target` — so a legacy `SH2-101_...`
>    row and a fresh `Sh2-101_...` scan of the same night surfaced as an
>    unrelated delete + create. Applying that pair would drop the row's
>    `id`/`created_at`, `processed_state` and `session_guiding` row and re-add
>    a bare one. They now pair into one **`rename`** proposal, applied through
>    `update_session_fields` (recomputes `session_id`/`lights_path` in place).
>    Ambiguous pairings decline to guess and fall back to create/delete.
>
> **Live dry run after the fixes: 24 proposals** — 2 rename (the `SH2-101`
> pairs), 5 delete (C 49 ×3, IC 1805, NGC 7000 2026-06-16 — genuinely absent
> from disk), 1 create, 16 update. Zero safe-tier, so nothing is
> bulk-appliable on a catalog with this much legacy drift; every one needs an
> individual look. Pushed to the queue 2026-08-30 (`--apply`, 24 rows into
> `rescan_proposals`); verified it wrote no session rows — counts unchanged and
> zero sessions with today's `updated_at`.
>
> **One proposal to leave pending — see M2.** The single `create` is
> `NGC7000_20250801_FRA400_Canon6D_Stars`. First read of it (including in an
> earlier revision of this entry) was that it's a processing byproduct
> mis-scanned as lights. **That was wrong** — checking the files, the folder
> holds 40 *raw* lights (20×10s + 20×30s @ ISO800), a short-exposure star
> layer shot after the main 12×300s ISO1600 run. The create is therefore
> *correct*: those frames really are uncatalogued. What's wrong is that the
> folder name becomes `filter='Stars'`, so applying it writes a junk filter.
> It also drags one `update` with it — the parent's `end_utc` shrinks, because
> the stored span had swallowed the star layer's frames. That update is a
> genuine correction. M2 owns the modelling question.
>
> **Both scope questions in this entry were decided before building:**
> - *Shape:* CLI **plus** the `/queue`-style web view, not CLI-only. Forced by
>   a fact worth recording — the LXC serving the webapi has the catalog DB but
>   **no archive mount** (`deploy/darkroom-api.service`), so the scan can never
>   run server-side. The CLI scans on the Mac and pushes proposals to
>   `rescan_proposals`; the server only reviews and applies. That constraint is
>   what makes the proposals table necessary rather than optional — a live
>   server-side scan was never available as an option.
> - *RA/Dec:* compared, with a **0.5° default tolerance** (`--pointing-tolerance`),
>   RA wrap-corrected at 360 so 359.9 vs 0.1 reads as 0.2° apart. This is the
>   check that catches the SH2-101 mis-slew class; the tolerance is what keeps
>   ordinary re-centring between sessions sharing a folder quiet.
>
> **Tiering as built:** `safe` = an `update` whose only changed fields are
> `frame_count`/`total_integration_sec` (the pure interior-deletion case —
> bulk-appliable in the queue UI). `review` = every `create`, every `delete`,
> and any `update` touching pointing/timing/equipment. `--apply` on the CLI
> **pushes proposals to the queue and never writes to `sessions`**; the only
> thing that edits the catalog is an explicit per-proposal Apply in the UI.
>
> **`_EDITABLE_FIELDS` widened:** `catalog_db` now allows `frame_count`/
> `total_integration_sec`, required by the safe-tier apply path.
> `total_integration_hours` stays excluded (GENERATED), and `ui.py:_EDIT_FIELDS`
> still doesn't expose either on the manual session edit form.
>
> **The guard that had to be added (review finding, not spec):** the first cut
> let `_scan_disk` return `{}` for an unreachable archive, which is
> indistinguishable from a genuinely empty one — so an unmounted NAS would
> classify **every** catalog session as a `delete` proposal, and `--apply`
> would wipe the real pending set to push them. Exactly the "can silently drop
> or fabricate sessions" failure this entry exists to prevent. Now:
> `ArchiveRootMissing` is a hard error, and a walk finding 0 sessions against a
> non-empty catalog raises `EmptyDiskDivergence`, which the CLI turns into a
> warning naming both counts plus a required confirmation (`--yes`, or a `yes`
> at the prompt; no TTY and no `--yes` refuses, matching `ingest review`). The
> reverse — empty catalog, full disk — is an ordinary first run and never
> prompts. **That asymmetry is the point:** only the empty-disk direction
> generates deletes.
>
> **B14 dependency honoured:** the recompute goes through
> `SessionAnalyzer.analyze_sessions` rather than reimplementing frame
> selection, which is how it inherits the chronologically-first-frame fix
> instead of regrowing the bug this entry warned it would otherwise confirm.

Filed 2026-08-29, alongside U4, out of the same SH2-101 fixup. Manually
computing `frame_count`/`total_integration_sec`/`dec_deg`/`start_utc`/`end_utc`
by hand after deleting bad subs is exactly the kind of thing that should be a
scan, not arithmetic — and it's not just deletions: renamed/moved session
folders, sessions dropped from the archive entirely (old test sessions, a
blown SD-card copy), and folders that appeared on disk without ever going
through `ingest` all leave the same kind of silent divergence.

- **Shape:** read-only pass over the archive (`find_lights_folders` +
  `SessionAnalyzer`, same walk `scan-lights` already does) compared against
  `resolve_backend().query_sessions()`. For each `session_id` seen on either
  side, classify:
  - **on disk, matches catalog** — no-op, not queued.
  - **on disk, diverges from catalog** (`frame_count`/`total_integration_sec`/
    `start_utc`/`end_utc`/`ra_deg`/`dec_deg`/`exposure_sec` differ from a
    fresh scan of what's actually there) — queue an *update* proposal,
    `session_id` unchanged.
  - **on disk, no catalog row** — queue a *create* proposal (folder exists but
    was never ingested/committed).
  - **in catalog, `lights_path` missing on disk** — queue a *delete*
    proposal, never an automatic delete (mirrors `scan-processed`'s
    never-auto-write posture; W10's session-delete already exists as the
    apply step once a proposal is confirmed).
- **Review queue, not direct write:** same posture as `scan-processed`
  (dry-run by default, `--apply` writes) and U2's filter-cleanup queue — a
  diff this consequential (can silently drop or fabricate sessions) shouldn't
  auto-commit. Given W9's existing `/queue` precedent, a
  `/api/rescan-proposals`-style endpoint + queue view is probably the more
  honest shape than a CLI-only `--apply` that just applies everything found.
- **Depends on U4:** pointless to build if the diff can't be reconciled
  against the live catalog — needs `resolve_backend`, not raw sqlite.
- **Scope check before building:** decide whether "diverges" should include
  `ra_deg`/`dec_deg` (mount-target drift mid-session, as caught here) or only
  frame-count/timing fields — the former is what caught the SH2-101 mis-slew,
  but it also fires on ordinary re-centering between sessions that share a
  folder, so it needs a tolerance, not an exact-match diff.

**Second real case, 2026-08-29 (SH2-101 2026-08-28, `L-Extreme`, 110→74
frames):** deleted 36 subs — one single tree-obstructed frame (`#0006`) plus a
contiguous 35-frame run (`#0070`–`#0104`) from a forgotten-meridian-flip mount
stall. Confirms the classification this entry proposes, and narrows the
"what to auto-fix vs. what to escalate" line more than the first case did:

- **Both deleted runs were interior** — the surviving first (`#0001`) and last
  (`#0110`) frames were untouched, so `start_utc`/`end_utc` (computed as
  min/max over all frames, per `compute_session_span`) came back byte-identical
  to what was already stored. Only `frame_count` (110→74) and
  `total_integration_sec` (19800s→13320s) actually changed — `ra_deg`/`dec_deg`
  also came back unchanged, because frame `#0001` (the directory-order "first"
  frame — see **B14**) survived the cull. A **pure interior-deletion divergence
  is cheap to auto-resolve**: recompute, diff, and if only `frame_count`/
  `total_integration_sec` moved, the update proposal needs no human judgement
  call the way a `ra_deg`/`dec_deg` change does — those two are safe to
  auto-apply, or at least pre-approved in the queue UI, while any change to
  `ra_deg`/`dec_deg`/`start_utc`/`end_utc` should still require a look.
- **Confirms F7's finding from the other direction:** F7 noted deleting
  interior subs can't shrink the guiding envelope; here it shows the same
  envelope-from-edges arithmetic is *exactly* why the recompute below is a
  no-op for the timing fields specifically when the edges survive — the two
  are the same underlying fact (`start_utc`/`end_utc` only ever look at the
  earliest/latest frame) cutting both ways. **F7 scoped 2026-08-29 and chose a
  live archive read precisely so it does not depend on this entry** — but F8
  stays the prerequisite for ever storing per-frame data (`session_frames`),
  since a stored table is only true if something owns refreshing it. F7's
  `frames_used`/`exposure_sec_used` staleness check is the cheap stand-in until
  then, and F8's recompute is what would clear it.
- **B14 dependency confirmed live, not just theoretical:** this session's
  folder holds one exposure length throughout, so `frames[0]` happened to be
  chronologically first and `ra_deg`/`dec_deg` came back correct by luck of
  the naming, not by correctness of the code. The `L-Synergy` case in the same
  BACKLOG.md update got the wrong answer from the identical code path. Fix
  B14 before wiring up `rescan-archive`'s comparison — otherwise the tool's
  own "recomputed value" is only as trustworthy as filename-sort-order happens
  to be that night.
- **Net effect on the original question this entry exists to answer:** yes, a
  targeted `POST /api/sessions` upsert (same shape as the `L-Synergy` fixup)
  is the right call here too, and is *lower-risk* than that first case —
  `frame_count`/`total_integration_sec` are the only fields moving, nothing
  representative-frame-derived changed. Deleting the row and re-ingesting
  would be strictly worse: it'd cost the existing `id`/`created_at` and, if
  `scan-guiding` had already run, wouldn't even change its output (per the
  point above).

---

### F9. A camera lens can impersonate a telescope — ✅ DECIDED + BUILT 2026-08-31

Filed 2026-08-30 out of M1's `Canon50mm` naming decision; decided and
implemented 2026-08-31 (`parse.py`, `tests/test_ota_lenses.py`).

**The filed premise was wrong in the one way that mattered.** This entry said
"the zoom has not been used for a catalogued session yet ... low urgency". A
read of the live catalog on 2026-08-31 found **28 sessions** already shot on
Canon glass, spanning 2023-04-15 → 2024-01-11, all on the Canon 6D:

| Catalogued as | Rows | Actually | Tell |
|---|---|---|---|
| `FRA400` | 8 | Canon zoom @ 391–395mm | dated 2023-11 → 2024-01, a year before the FRA400 was bought |
| `Unknown` | 18 | Canon zoom @ 100/104/136/200/202/301/386mm | between the scope windows |
| `Unknown` | 2 | `Canon50mm` | M 17 2023-08-09 (fl 56) and NGC 7000 2023-09-14 (fl 53) |

The 8 `FRA400` rows are the damage this entry predicted: a wrong optic baked
into `session_id` and the folder name, and — worse — the matching flat sets
(`00_Calibration/Flats/400mm_Canon6D*`, 6 sets) were registered `ota=FRA400`
too, so the wrong-optic attribution was self-consistent and invisible.

**The decision.**

1. **Each marked zoom stop is its own OTA**: `Canon100mm`, `Canon135mm`,
   `Canon200mm`, `Canon300mm`, `Canon400mm`, alongside the existing
   `Canon50mm`. *Not* a single `Canon100-400`. Flat matching keys on
   OTA + camera + filter, so one name for the whole range makes a 100mm flat a
   legal match for a 400mm light — the same class of bug this entry exists to
   prevent, merely relocated. The archive already separates them by hand
   (`Flats/{100,135,200,300,400}mm_Canon6D`), so per-stop names map 1:1 onto
   what is on disk.
2. **The date breaks the tie.** `OTA_ACQUIRED` records when each scope entered
   service — FMA180Pro January 2023, FRA400 (and its 0.7x reducer) January
   2025. `parse_ota` gained a keyword-only `obs_date`: when a scope window
   matches but the session predates that scope, inference falls through to the
   lens of the same focal length. Without `obs_date` the result is
   byte-identical to before, so every legacy call site is unchanged. Wired at
   the four call sites that have a date in hand (`scanner.py:169,223`,
   `cataloger.py:947,1114`).
3. **The 50mm window now reaches 60.** The M 17 2023-08-09 session reports
   `FOCALLEN 56` but plate-solves to **51mm**, and its flats live in
   `50mm_Canon6D/2023-08-10` (flat-morning +1). The header is simply wrong at
   that end; nothing of Jonathan's lives between 60 and 95, so the slack is
   free.
4. **Aperture stays out of it** (unchanged from the original filing): the EF
   adapter is mechanical with no aperture control, so both Canon lenses are
   always wide open and focal ratio is not an identity component.

**What the rule cannot do.** It is retrospective only. A zoom night shot
*today* at 180 or 400mm is genuinely indistinguishable from the scope, and
`parse_ota` will answer with the scope. That still needs a hand correction in
`ingest review` — which is now possible, since `KNOWN_OTAS` carries the lens
names. Off-stop focal lengths (250, 350, …) stay `Unknown`, deliberately: an
off-mark reading is not snapped to the nearest stop.

**Verified against the live catalog** before anything was written: exactly the
28 rows above change, nothing else moves, and afterwards **no session in the
catalog has `ota='Unknown'`**.

---

### F9a. Apply the F9 correction to the live catalog and the archive — ✅ DONE 2026-08-31

Executed the same day F9 was decided, against the live catalog and the mounted
archive. Deployed on `c7a5ef7`.

| Step | Result |
|---|---|
| Session OTAs | 28 rows PATCHed. Catalog now holds **zero `Unknown` OTAs**, and zero rows disagreeing with `parse_ota` |
| Camera fix | `M42_20230415` `ASCOMCameraDriver` → `Canon6D` (confirmed by Jonathan: a non-ASIAir acquisition path, BackyardEOS or N.I.N.A., writing a generic driver string) |
| Archive folders | 24 renames applied, 4 already_done, **0 conflict / 0 missing / 0 error**; ledger drained to 0 |
| Frame split | 227 frames moved into 4 per-night folders (see below) |
| Calibration | **175 sets** re-derived: 72 `FRA400`→`Canon400mm`, and 103 `Unknown`→`Canon{50,100,135,200,300}mm`. All dated 2023-04-15 → 2024-01-24; **nothing dated 2025+ moved** |
| Stale row | The `60mm_Canon6D` flat set re-registered at its real path, `50mm_Canon6D/2023-08-10` (those flats read `FOCALLEN 59` — which is what the widened window is for) |

Backups on the LXC: `astro_catalog-pre-F9-20260831-222645.db`,
`-pre-F9cal-20260831-224730.db`, `-pre-deploy-20260831-225233.db`.

**Two legacy folders each held two sessions' frames** and had to be split
before any rename could run (`apply-renames` moves whole directories and is
all-or-nothing, so the 24 clean moves were blocked behind them):

- `M 31/2023-11-18_FRA400_Canon6D_L-Pro/Lights` → 36 frames (night 2023-11-17)
  + 87 (night 2023-11-20)
- `C 49/2024-01-10_FMA180_Canon6D_L-Pro/Lights` → 49 (2024-01-10) + 55 (2024-01-11)

Nights were assigned from `DATE-OBS` via `cataloger.compute_imaging_night`, and
every count matched the catalog's own `frame_count` exactly before anything
moved. Note both folder *names* were wrong at the time they were written by
hand (`FRA400`/`FMA180` on frames reading 391-394mm) — the rename resolved a
disagreement rather than creating one.

**Residue — three follow-ups, none blocking:**

1. **Three more shared-folder pairs existed** — NGC 7000 2024-02-26/27
   (10+100 frames in one folder), M 81 2025-03-25/28 (48+77), NGC 7000
   2025-07-30/31 (11+60). ✅ **All three split 2026-08-31**, 306 frames, same
   method: assign by `compute_imaging_night`, verify against `frame_count`,
   move, then re-send an unchanged identity field so `update_session_fields`
   recomputes `lights_path` and queues the rename. The catalog now has **zero
   `lights_path`s shared by more than one session**.

   Two of the six came back from `apply-renames` as `conflict` rather than
   `already_done`, correctly: their old path still existed because it now held
   the *other* session's `Lights/`. Both were verified satisfied (right frames
   at the new path, no loose frames at the old) and their ledger rows deleted
   by hand. Worth knowing that "deepening" splits produce this shape.

   `IC4604_20250426_P1-2`'s 35-vs-33 turned out **not** to be a discrepancy:
   33 frames are from the night of 2025-04-26 (timestamped `20250427-02:41` →
   `04:12`) and 2 are from `20250428-00:05`, i.e. the night of 2025-04-27 — a
   4-minute aborted start, panel 1-2 only, with no session row. The catalog's
   33 is correct. Those 2 frames are still unregistered; decide whether to
   register or relocate them.
2. **`ASCOMCameraDriver` survives on 3 calibration sets.** `camera` is part of
   `set_id`, so unlike `ota` it cannot be migrated by a rescan — correcting it
   means new rows and orphaned old ones. Consequence: `M42_20230415` (now
   `Canon6D`) matches none of its own flats, which sit in
   `100mm_Canon6D/2023-04-17` under the old camera string.

   **Decided 2026-08-31: fix the headers, not the code.** A
   `_CAMERA_ALIASES` entry was considered and rejected — `ASCOM Camera Driver`
   is a *generic driver string*, not a camera, so aliasing it to `Canon6D`
   would be silently wrong the first time the ZWO is driven through
   N.I.N.A./ASCOM, with no date available to disambiguate it the way F9 has.
   The archive instead needs `INSTRUME` rewritten to the real camera, after
   which a rescan derives `Canon6D` on its own. Scope, measured 2026-08-31:
   **154 files, all April 2023**, in 6 folders —
   `Bias/Canon6D/Raw/2023-04-17` (26), `Darks/Canon6D/Raw/20s/2023-04-15` (40),
   `Flats/100mm_Canon6D/2023-04-17` (40),
   `M 42/2023-04-15_Canon100mm_Canon6D/Lights/L-Pro` (40), and 8 in
   `M 42/_Processed/`. The calibration folders are themselves named `Canon6D`,
   which is the corroboration. Note the rewrite changes `set_id` for the
   affected calibration sets, so the 3 old rows must be retired by hand
   afterwards. `_Processed` outputs are derived products and can be left.
3. **Flat coverage after the fix: 18 of 36 Canon-optic sessions match a flat
   set.** The misses are genuine (no flats within ±3 days, or a filter
   mismatch), not attribution errors. The 8 M 8 mosaic panels match nothing
   because **no flats were shot that night** — confirmed by Jonathan
   2026-08-31, so this is a fact about the data, not a matching bug. Stacking
   that mosaic will need flats from another `Canon50mm` + `ZWOASI585MCPro`
   occasion, or none at all.

---

### F10. `.darkroom-ignore` — a directory marker that keeps rejects out of the catalog

Filed 2026-08-31 at Jonathan's request, out of the F9a archive sweep.

**The problem.** Rejected subs are kept on disk, in a subfolder beside the good
ones, because "bad" is a judgement that gets revisited — slight eccentricity
often turns out to be usable at a lower weight. But the archive walkers have no
way to know a folder is a holding pen, so those frames get catalogued as
sessions: their name becomes the *filter* (the M2 mechanism) and their
integration time is counted as real depth.

Measured on the live archive, 2026-08-31: **14 such folders holding 404
frames**, of which **4 are already catalogued as sessions, contributing 6.83
hours of integration time that does not exist**:

| Session | Frames | Counted |
|---|---|---|
| `IC 1805/2023-12-14_FMA180_Canon6D_L-Extreme/bad` | 35 | 1.75h |
| `C 49/2024-01-20_FMA180_Canon6D_L-Extreme/bad` | 91 | 3.03h |
| `C 49/2024-01-21_FMA180_Canon6D_L-Extreme/delete` | 20 | 1.00h |
| `C 49/2024-01-22_FMA180_Canon6D_L-Extreme/delete` | 21 | 1.05h |

**Why a marker file and not a name list.** The folders in the archive today are
named `reject`, `Rejects`, `Rejected`, `delete`, `Delete`, `bad` and `Bad` —
seven spellings across 14 folders, and that is only what has been used so far.
Name matching would be both leaky (the next spelling is not in the list) and
dangerous (a legitimately named target or filter folder could match). An
explicit opt-out file is unambiguous, is visible in the folder it affects, and
is created with `touch`.

**Design.**

1. A directory containing `.darkroom-ignore` is skipped by every archive
   walker, **along with everything beneath it**. One helper — the same
   single-source-of-truth discipline as `parse.py` — checked by
   `cataloger.scan_lights`, `scan_calibration`, `rescan`, `procscan`,
   `backfill-times`/`backfill-sites` and `guidescan`. Never re-implement the
   check inline.
2. **It never deletes or moves a file.** The marker is a catalog-visibility
   control, and the "never delete source files" rule is unchanged. The frames
   stay exactly where they are.
3. **Existing rows must be cleaned up, not orphaned.** `catalog rescan-archive`
   already diffs the archive against the catalog, so an ignored folder that
   still has a session row becomes a `delete` proposal in `/rescan` — reusing
   the confirm-before-delete path added in `840605b` rather than deleting
   silently. That is the whole cleanup mechanism; nothing new is needed on the
   web side.
4. **It has to be reversible.** Remove the marker, rescan, and the session
   comes back. This matters because the workflow it serves is explicitly
   "blink through these again later" — the marker records *undecided*, not
   *condemned*.

**Not the same problem as M2.** M2 is about a sub-folder holding real data with
a role the scanner has no vocabulary for (`NGC 7000/.../20250802_..._RGB_Stars`
is a broadband star layer, deliberately shot to be composited). F10 is about a
sub-folder holding data that should not count at all. Do not let one fix absorb
the other: a `.darkroom-ignore` in the `Stars` folder would hide data Jonathan
wants, and M2's answer (a role/kind on the session) would wrongly dignify a
reject pile.

**Follow-on, deliberately out of scope.** The end state Jonathan wants is not
binary. After re-blinking, a marginal sub should be able to contribute at a
*lower weight* rather than being in or out — which is F6's weighted-hours
machinery applied at frame level, and shares the per-frame problem already
scoped in **F7**. F10 is the cheap half: stop counting the obviously-bad, keep
the files, keep the option open.

---

### F11. `session_exposure_sets` — a night's parameters are not scalar

Filed 2026-09-01, out of a design discussion with Jonathan about how to model
multi-exposure acquisition. Decided, not yet built.

**The problem.** A session row carries one `exposure_sec`, one `gain`, one
`temperature_c`. A night that brackets exposures therefore stores whichever
value the first frame happened to have, and the rest are silently wrong. This
is not an edge case — it is the normal shape of solar, lunar and HDR work, and
it is also **B13** (night-level dark params from `sessions[0]`) and **F5**
(temperature is a range) seen from a third angle.

**Two real datasets, measured 2026-09-01, both still un-ingested:**

| | Sun (`Autorun/Light/Sun`) | Moon (`Autorun/Light/Moon`) |
|---|---|---|
| Frames / integration | 365 / **33.7s** | 259 / **14.8s** |
| Nights | 3 (2026-08-05/07/12) | 1 (2026-08-28 frames → night **2026-08-27**) |
| Exposures | 15, 100µs → 2s | 9, 5ms → 200ms |
| Gains | 0 and 50 | 0 |
| Under today's rule | **3 rows**, each with an invented exposure | **1 row**, ditto |
| With exposure+gain in identity | 41 rows | 9 rows |
| **Under F11** | 3 sessions + 41 sets | 1 session + 9 sets |

The 2026-08-12 Sun run is the total solar eclipse and the 2026-08-28 Moon run
is the lunar eclipse — irreplaceable data, which is part of why the model
should be settled before ingesting them.

**The decision.** One session row per (target, night, filter, OTA, camera) as
today, plus a child table of acquisition sets:

```
session_exposure_sets(session_id, exposure_sec, gain, temperature_c,
                      frame_count, total_integration_sec, ...)
```

Rejected alternatives, with reasons, so this is not relitigated:

- **A `layer` column** (`main`/`stars`/`hdr`). Rejected: layer is
  *post-processing intent*, not an acquisition fact. ASIAir filenames, Astrobin
  acquisition tables and N.I.N.A. file patterns all key on
  (filter, exposure, gain, binning) and none of them model intent.
- **Exposure + gain as session identity.** Honest, and it is the field-standard
  key — but it turns the Sun into 41 rows and the whole catalog into a 247-row
  `session_id` migration, to express something that is really one night's work.

**Consequence that is the actual scope: this moves the dark-match key.**

| | lives at | matches |
|---|---|---|
| filter | **session** | flats (OTA + camera + filter) |
| exposure, gain, temperature | **exposure set** | darks (camera + gain + exposure + temp) |

`catalog.find_darks`, `prep.py` and the WBPP tree layout all read those off the
session row today. The schema is the easy half; relocating dark matching one
level down is the work. And per **M3**'s finding, assume each exposure set
needs its **own WBPP tree** — a grouping keyword is not a stacking boundary —
but *test* that rather than assuming it, the way the panel case was settled.

**Prerequisite — microsecond exposures are not representable today.** Found
while checking the Sun data:

1. `parse.EXPOSURE_RE` matches `ms|s` only, so `500.0us` returns `None` — 236
   of the 365 solar frames. Only `wbpp.discover_dark_files` consumes it, which
   would then match no darks at all, silently.
2. `names._round_exposure` rounds to 4 dp, so **100µs and 125µs both store as
   `0.0001`** and 250µs stores as `0.0003`. If exposure is what separates
   sub-runs, it cannot be a rounded float — carry the ASIAir label (`500us`,
   `128ms`, `2.0s`) or store full precision.

Neither blocks the Moon data (all ≥5ms, zero failures, zero collisions); both
block the Sun.

**Solar / lunar / planetary sit outside integration accounting** (Jonathan,
2026-09-01). They can still be culled and stacked lucky-imaging style, but they
have no darks or flats, thermal noise is negligible at these exposures, and
"integration depth" is meaningless for them — 33.7s of eclipse must not appear
in a target's weighted hours. That needs its own mechanism (a target kind, or a
per-session flag) and it also disposes of **B8**'s `0.0h` rendering for these
rows, since they should not be rendered as depth at all.

**What F11 is NOT: the RGB star layer.** That layer differs by **filter**, and
filter must stay at session level because flat matching keys on it — an
unfiltered star layer folded into an L-Extreme session would be matched to
L-Extreme flats. It is already handled correctly as its own session, a sibling
under the same night (`Lights/L-Extreme/` and `Lights/NoFilter/`), and it needs
its own calibration frames. It only becomes an F11 bracket if a star layer is
ever shot through the *same* filter as the main run.

**One expectation to set:** an after-midnight event is dated by the night it
began. The 2026-08-28 lunar eclipse will be catalogued as **2026-08-27**.
Correct by the session-date rule, surprising for a named event — a note field
is probably the answer, not an exception to the rule.

---

## S — Observation sites & conditions

### S1. Observation-site tracking + SQM-weighted depth — ✅ DONE

> Shipped 2026-07-16 (`0e96718`, `2a94cb1`, `51f9e72`, `281a1f0`, plus UI
> follow-ups `495279b`/`571ce4a`/`699acbe`/`4ff649e`/`8550c03`); deployed to
> the LXC and seeded the same day. Hardened 2026-07-29 (`26ba335`, `40162ce`,
> `0211bce`, `3ea2c5c`).
>
> **Why:** integration hours aren't fungible across sites. Four hours from
> Bortle 2 on Pico is worth far more than four from the Bortle 7 back garden,
> so a raw hours gauge over-reports how deep a target actually is.
>
> **Phase 1 — schema (`0e96718`):** `sessions` gained nullable
> `site_lat`/`site_lon` (from the ASIAir's `SITELAT`/`SITELONG` headers;
> `COALESCE`d on rescan so a rescan never blanks a known position). New
> `sites` table (name/lat/lon/radius_m/bortle/sqm/is_home, partial unique
> index enforcing a single home). New `darkroom/sites.py` — stdlib-only:
> `haversine_m`, `resolve_site` (nearest site within its `radius_m`),
> `home_sqm`, and `session_weight` = flux ratio `10^((sqm − sqm_home)/2.5)`.
> **Sites resolve at query time from coordinates — no stored `site_id`** — so
> moving a site or fixing its radius reclassifies history for free.
> **Phase 2 — API (`2a94cb1`):** `GET`/`POST`/`PATCH /api/sites` + matching
> `CatalogBackend` methods on both Local and Http impls.
> **Phase 3 — CLI (`51f9e72`):** `darkroom catalog sites add/list/set`
> (`list` doubles as a resolve debugger — shows which sessions land where) and
> `darkroom catalog backfill-sites --archive PATH [--apply]` (dry-run default,
> idempotent).
> **Phase 4 — UI (`281a1f0` + follow-ups):** the depth gauge now reads in
> **home-equivalent hours** with the raw figure shown alongside; per-night
> site chips with `×weight` badges; a site filter on the overview sorted by
> distance from home; editable `site_lat`/`site_lon` on the session edit
> screen; a missing-site-coordinates section on `/queue` linked from the
> overview's cleanup teaser.
>
> **Live state (verified 2026-07-29):** 8 sites seeded with SQM/Bortle from
> deepskysites.com — Home (Palmela) 19.19 · Quinta do Lago (Azeitão) 19.38 ·
> Santa Susana 21.06 · Santa Susana (SE) 21.6 (added 07-28) · Sorte Verde ·
> São Cristóvão · Mount Pico 21.75 · Cais do Pico 20.96. 230 sessions, 169
> with coordinates. Telescopius' API carries no SQM (checked against a live
> payload) — the numbers are entered by hand via `sites set NAME --sqm X
> --bortle N`.
>
> **Hardening 2026-07-29 — one bad frame decided a whole night.** Both the
> ingest path and `backfill-sites` took the **first** FITS frame's
> `SITELAT`/`SITELONG` as the session's position. The ASIAir sources its fix
> from the phone, which sometimes opens a session with a stale or
> WiFi-geolocated position (see the standing caveat below), so a single
> unrepresentative frame mislabelled the night. Real damage in the live
> catalog: the 2026-07-26 NGC 281 session was filed as home when 43 of its 44
> frames were 43 km away, and four sessions overall were wrong — two dark-site
> nights filed as home (silently losing their SQM weighting, the exact thing
> S1 exists to provide) and two home nights filed as a dark site.
> Fixed by taking the **modal** position across every frame:
> `sites.modal_site` + `sites.describe_disagreement` (`26ba335`), wired into
> ingest scan → manifest → commit (`40162ce`, so new sessions land with their
> site already set instead of needing a later backfill pass) and into
> `backfill-sites` (`0211bce`, which also now skips unreadable frames
> per-frame rather than letting one bad file cost a session its coordinates).
> Disagreement is reported on stderr and surfaced at scan time.
> `scripts/fix_site_headers.py` (`3ea2c5c`) repairs the underlying headers on
> disk: overwrites `SITELAT`/`SITELONG` under a directory with a known-correct
> position, recording the original in a `HISTORY` card so the edit stays
> auditable; dry-run by default, `--apply` to write, idempotent (frames
> already within `--tolerance` are skipped).
>
> **Standing caveat (not a bug to fix):** the ASIAir takes its coordinates
> from the connected phone, and WiFi geolocation returns *confidently wrong*
> positions. Verify the fix before a field session — a wrong position is
> indistinguishable from a right one in the header.

**Remaining / open:**
- 61 sessions still have no coordinates — mostly Canon-era nights predating
  the ASIAir's `SITELAT`/`SITELONG` headers. They resolve to no site and get
  weight 1.0 (home), which is right for most of them, but any that were
  actually dark-site trips are silently under-credited. Worth a manual pass
  through the `/queue` missing-coordinates section, entering positions from
  memory where the trip is recognisable.
- Site radius is a flat `radius_m` per site (default 1000 m). Fine so far;
  revisit only if two genuinely distinct sites ever fall inside one radius.

### S2. Expose SQM/dark-site weighting on the JSON API, not just the HTML dashboard — ✅ DONE

> Shipped 2026-07-30 (`3cfed19` + review follow-up): took the "fold it into the
> main payload" option, not the separate endpoint. New shared helper
> `darkroom.sites.annotate_sessions(rows, sites, *, home=None)` returns copies
> of each row with three added keys — `site` (resolved name or None), `weight`
> (`round(session_weight, 3)`), `weighted_hours` (`h * weight`). Wired into
> `GET /api/sessions` (`app.py`) **and** `LocalBackend.query_sessions`, so the
> two backends return the same weighted shape rather than the weighting being
> an HTTP-only artifact. `_build_aggregate` now delegates to the same helper
> instead of carrying its own inline copy, and maps the verbose keys onto the
> short `w`/`wh` the embedded dashboard JS already reads — app.js and the
> aggregate tests are unchanged.
>
> Three decisions worth remembering:
> - **Verbose names on the API, short names in the aggregate.** The first cut
>   used `w`/`wh` everywhere for symmetry; the aggregate is terse because it's
>   an embedded JS payload where bytes matter, which is not a pressure a JSON
>   API shares. `weight`/`weighted_hours` are self-explanatory to anyone
>   hitting the endpoint with curl.
> - **`weighted_hours` multiplies by the *rounded* weight**, so a consumer
>   recomputing `h * weight` from the published fields lands exactly on the
>   published `weighted_hours`. Rounding one and not the other left the two
>   disagreeing in the last decimals (2.512 vs 2.5118864…).
> - **`annotate_sessions` is pure** — returns copies, never mutates its input.
>   `_build_aggregate` annotates rows it doesn't own, so in-place mutation
>   would have been a side effect visible to its caller.
>
> `LocalBackend` deliberately does **not** `_ensure_schema` on this read path
> (procscan's read-only dry-run contract), so a pre-S1 file with no `sites`
> table catches `OperationalError` and degrades to "no sites → weight 1.0" —
> exactly the pre-S2 behaviour. Unreachable via the webapi, since `create_app`
> runs `init_db` at construction; covered by a local-backend test that drops
> the table and asserts the read didn't migrate it back. Suite 924 → 948.
> Not yet deployed to the LXC.

Queued 2026-07-29. Checked against the live code: `session_weight`/`resolve_site`
(`darkroom/sites.py`) are only ever called from `_build_aggregate`
(`darkroom/webapi/ui.py:105-169`), which feeds the server-rendered dashboard
(`GET /`, `GET /targets/{target}`) and its embedded JS blob
(`darkroom/webapi/static/app.js`). `GET /api/sessions`
(`darkroom/webapi/app.py:182-210`) calls `catalog_db.query_sessions` and
returns raw DB rows (`site_lat`/`site_lon` only) straight through — no
`site`/`w`/`wh` fields, confirmed by `tests/test_webapi.py:632-668` which only
asserts the raw lat/lon columns. Anything consuming `/api/sessions` directly
(scripts, the `HttpCatalogBackend`, future tooling) sees unweighted hours only.
Fix: either resolve site + weight per row inside `query_sessions`/the handler
and add `site`, `weight`, `home_equivalent_hours` (naming TBD) to the response,
or add a dedicated `GET /api/sessions/{id}/weight`-style aggregate endpoint if
folding it into the main payload is too heavy for list views. Prefer the
former — it's the same shape other JSON consumers will want, and it keeps
`_build_aggregate` from being the only place this math runs.

### S3. Show moon phase in the session list; open question on moon/elevation weighting
Queued 2026-07-29. Checked: there is no moon-phase, moon-separation, or
altitude/elevation-tracking code anywhere in the repo today (no `skyfield`/
`ephem`/`astral` dependency, no `moon`/`lunar` hits under `darkroom/`) — this
is new, not a wiring gap like S2. `astropy` is already a dependency
(`pyproject.toml:7`) and has `astropy.coordinates.get_body("moon", time,
location)` available for phase/illumination and, given the session's RA/Dec +
site lat/lon, angular separation.
- **Display scope:** compute moon phase (illumination %, or simple
  new/crescent/quarter/gibbous/full label) for each session's date/site and
  show it on the night row in the target detail view (`ui.py` aggregate +
  `app.js` render), similar to how the site chip is shown today. Needs a
  timestamp per session to feed the ephemeris — check whether `sessions` has
  a usable session-start time or just a date (imaging night is a calendar
  date per `cataloger.py:compute_imaging_night`; moon phase is only stable to
  ~half a day of precision either way, so date-level granularity is probably
  fine).
- **Open question (not decided — needs a call before scoping further):**
  should moon phase and moon elevation/separation *during* the session also
  feed into the integration weighting (`session_weight` in `sites.py`) the
  same way SQM does, on the theory that a full moon or low moon-separation
  degrades a night regardless of site darkness? This would need per-session
  moon altitude/separation at imaging time (not just phase), which pushes
  toward needing a real session-start timestamp rather than a date, and
  raises the same "avoid double-counting" question SQM already answers for
  site — a bright moon and a bad site are correlated in some ways but not
  others. Recommend: ship phase-as-display first (S3 core), decide the
  weighting question separately once real moon-phase data exists to look at
  and judge whether it's actually moving the needle on real sessions.

---

## M — Mosaics

### M1. Model mosaic panels as a session dimension, not as separate targets

> **✅ INGEST HALF DONE 2026-08-30** (`66fd93a`, `9bbf42a`, `8cde7de`; tests in
> `tests/test_panel.py`, suite 1142 → 1190). `parse_panel` + `panel_from_dirname`,
> the nullable `sessions.panel` column, panel-aware `make_session_id` /
> `session_dest_rel` (keyword-only `panel=None`, so every pre-M1 call is
> byte-identical), and the wiring through `scanner` → `ingest` →
> `ingest_review` → `catalog_db` → `webapi.SessionIn`. The 50mm blocker is
> cleared: `parse_ota` has a 45–55 window and `KNOWN_OTAS` has the name.
>
> Two things landed beyond the touchpoint table below, because that table
> predates F8 and would have left the archive-read side lying:
> `cataloger._filter_from_path` now skips a trailing `P<panel>` dir (without it
> every panel reads as `UnknownFilter`, since the panel dir sits where the
> filter dir used to), `analyze_sessions` records the panel, and
> `rescan._canonical_session_id` splits a legacy panel-in-target row so it
> reads as a **rename** rather than a delete + create — the latter would drop
> the row's `processed_state` and its `session_guiding` row on apply.
>
> **Deferred half — status 2026-08-31.** ✅ Per-panel WBPP trees and the
> panel-aware `finish` both landed as **M3**. ✅ The live IC 4604 migration is
> done (Jonathan supplied `L-Pro`; 8 panelled rows + 1 NULL-panel row, folders
> moved, ledger drained). Live catalog now holds 16 panelled sessions —
> IC 4604 ×8 and M 8 ×8.
>
> **✅ WEB UI HALF DONE 2026-09-02.** All three gaps closed:
>
> - `_EDIT_FIELDS` carries `panel`, and `session.html` has the field — a mosaic
>   ingested before panels existed can acquire one, and a panel typed on by
>   mistake can be cleared. It is an identity edit like target/filter, so the
>   same `update_session_fields` path renames `session_id`, recomputes
>   `lights_path` and queues the folder move.
> - **Panel-aware rollups.** `_build_aggregate` puts `panel` on each night and
>   `n_panels` on each target; `app.js` divides every *depth* figure by the
>   panel count (overview gauge, per-rig gauge) while leaving the *totals* as
>   the true panel-time they are, so 18.5h across 8 panels now reads as a
>   2.3h-deep mosaic instead of a deep target. The target page gains a per-panel
>   breakdown (one gauge per panel, panels under ¾ of the deepest marked) and a
>   Panel column on the night table; the overview gains an `N-panel mosaic`
>   badge and a mosaic count in the statline.
> - `_PANEL_SUFFIX_RE` is gone — the suggestion now uses `parse.parse_panel`
>   (which also catches the `" N-M"` spelling) and carries the label out, so
>   `rename_target(..., panel=...)` sets target **and** panel in one edit. Two
>   panels of the same night merge without colliding; a merge with no panel
>   still leaves an existing one alone.
>
> Suite 1271 → 1284.

Queued 2026-07-30, out of Jonathan's question: the U2 cleanup queue flags
`IC 4604_1-1` … `IC 4604_2-2` as a probable 4-panel mosaic — so how should a
mosaic actually be laid out in the archive?

**Decision: a panel is a fourth identity dimension on the session** (alongside
obs_date / OTA / camera / filter), carried in a new nullable `sessions.panel`
column and in the folder path. The base target stays the object designation
(`IC 4604`), so target browsing, `--target IC 4604`, flat matching and
duplicate detection all keep working unchanged.

#### Layout

```
01_Deep Sky Objects/
  IC 4604/                                    ← base target, one folder per object
    2025-04-26_FRA400_Canon6D_NoFilter/       ← one folder per night, unchanged
      Lights/
        NoFilter/
          P1-1/  P1-2/  P2-1/  P2-2/          ← lights_path points here, per panel
    _Processed/
      2025-05-29/
        1-1/ …                                ← the manual convention on disk (bare, no "P" — verified 2026-08-31)
```

One session row per **panel-night**: 4 panels × 2 nights = 8 rows, each with its
own frame count and integration. That is the correct grain — panel `2-2`
genuinely has less data than `1-1`, and panels accumulate across nights
independently (live data: the 2025-04-26 and 2025-05-24 nights have different
per-panel frame counts, and 2025-04-27 has a 2-frame tail on `1-2` alone).

#### Why not the alternatives

- **Panel as the target name** (`IC 4604_1-1`) — what the live catalog has today,
  and what U2's Targets suggestions flag as broken. Loses object-level grouping,
  pollutes the target list, breaks `--target IC 4604`.
- **Panel in the session folder name** (`2025-04-26_FRA400_Canon6D_NoFilter_P1-1/`)
  — duplicates the night folder 4×, so one night's work is no longer one
  directory and that night's single flat set appears to belong to four sessions.
- **All panels in one session row** — registration across non-overlapping panels
  fails, and per-panel depth becomes invisible.
- **A `mosaics` table with grid geometry** — overkill. Base target + a `panel`
  string gives the grouping for free; the grid is implied by the `N-M` labels.

#### Code touchpoints

| Piece | Change |
|---|---|
| `cataloger.py:_SESSIONS_SCHEMA` | nullable `panel TEXT`, additive migration; NULL = ordinary single-pointing session (the overwhelming majority) |
| `names.py:make_session_id` | append `_P1-1` when panel is set — **this is what fixes the same-night collision** `catalog_db.rename_target` currently reports as a per-row error |
| `names.py:session_dest_rel` | append the panel dir under `Lights/<filter>/` |
| `parse.py` | new `parse_panel()`: split a trailing `_N-M` off the ASIAir object name. The panel already arrives there — real frames are `Light_IC4604_1-1_120.0s_Bin1_ISO1600_20250427-041855_14.0C_0001.fit` |
| `ingest.py` | use it at scan time so base target and panel are separated before anything is copied |
| `ingest_review.py` | show panel in the summary block, editable like filter |
| `prep.py` / `wbpp.py` | **the one real behaviour change** — panels must not be stacked together, so a mosaic prep emits one tree per panel: `~/WBPP/IC4604_P1-1/SESSION_1..N/`. Picker grows a panel step (or "all panels" → N trees in one go). ✅ **Re-confirmed 2026-08-31** after the nested-grouping-keyword alternative was tested and failed at integration — see **M3**. ⚠️ But "keeps the `wbpp → finish` handoff working unchanged" below is **wrong**: `finish.py:189` looks up `wbpp_root/target_slug(target)` and would never find `IC4604_P1-1`. See M3 for what finish actually needs |
| `finish.py` | output to `_Processed/<date>/P1-1/` |
| `webapi/ui.py` | ✅ the panel suggestion sets target **and** panel in one edit (target-only hits the collision); the target view shows "4 panels · 2.1h/panel" and a per-panel breakdown rather than implying that 8.4h of panel-time is 8.4h of depth |

Nothing else moves: `parse.fits_files` is non-recursive by default (so the new
nesting can't leak frames into a sibling panel's symlink set), and flat/dark
matching never keys on target.

#### Migrating the live IC 4604 rows (verified against the live catalog 2026-07-30)

> **Re-verified against the live server 2026-08-30 — two corrections:** there
> are **10** rows carrying `4604`, not nine; and **all four panels** were
> revisited on 2025-05-24, not just `1-1`. So the mosaic is two complete
> 4-panel nights (2025-04-26 and 2025-05-24) plus the 2-frame 2025-04-27 tail
> and the bare 2023-07-15 single-pointing session. All 10 now have
> `filter = NULL`. See `BLOCKERS.md` #3 for the per-row table.

Nine rows carry `4604`. Eight are panels, and the panel name landed in **both**
the target and the filter column (`target: 'IC 4604_1-1'`,
`filter: 'IC4604_1-1'`) — these rows came from a scan that read
`Lights/<subdir>` as a filter, so **the real filter for those two nights is not
recorded anywhere** and Jonathan has to supply it.

1. Per row: set target `IC 4604` + panel `1-1` + the real filter in **one** edit.
2. That writes `pending_renames`; `darkroom catalog apply-renames` then moves
   `Lights/IC4604_1-1/` → `Lights/<Filter>/P1-1/`.
3. Unrelated oddities in the same data, for whoever does the pass: the
   2023-07-15 row (21 frames, `ota: Unknown`, `lights_path` with no `Lights/`
   level) is a genuine single-pointing session of the same object — leave its
   `panel` NULL (Jonathan confirmed 2026-08-30; see "the bare `IC 4604` row is
   NOT a stray" below, it's a design constraint, not a leftover); and there is
   a stray 2-frame `IC 4604_1-2` row on obs_date 2025-04-27 that looks like a
   tail past the night boundary.
4. **Their `filter` column is now empty, not `IC4604_1-1`** — M2's
   KNOWN_FILTERS guard (2026-08-30, `0e54759`) evicted the panel names, and
   F8's rename proposals applied that to all 9 rows. So the "panel name in
   both target and filter" description above is now only true of the *target*
   column, and those rows will surface in U2's filter queue wanting the real
   filter, which still isn't recorded anywhere.

#### Do first / do later — ⚠️ UPDATED 2026-08-30: the trigger condition has fired

The original note said the ingest half should land "before the next mosaic is
shot". **That mosaic now exists and is waiting to be ingested**, so the ingest
half (`parse_panel` + the `panel` column + `make_session_id`) is no longer
speculative priority — it is the thing standing between Jonathan and ingesting
real data. Ingesting first means the new mosaic recreates the five-fake-targets
mess from scratch and doubles the cleanup. He has deliberately held the ingest.

The web-UI panel-aware totals and the per-panel WBPP prep can still follow.

#### The pending mosaic (facts confirmed by Jonathan 2026-08-30)

- **8 panels**, **50mm lens**, roughly centred on **M8**, shot in **blocks**
  (all of one panel, then the next), not interleaved.
- **Shot without the guidescope** — at 50mm the FOV is wide enough that he
  judged guiding unnecessary. **Consequence: there will be no PHD2 guide log
  for that night, so `scan-guiding` will list those 8 sessions as unmatched.
  That is correct and expected, not a failure to debug.** F4's design already
  treats "no guiding data" as row-absent rather than an error.

**Headers read off the real frames 2026-08-30** (preliminary-processing copy at
`~/02_Astrophotography/03_Processing/WBPP/M8_Mosaic/Lights/`), which corrects
two assumptions above:

| | |
|---|---|
| Folders | `M 8_1-1` … `M 8_4-2` — a **4×2 grid**, target with a space, panel after an underscore |
| `FOCALLEN` | **51** on every panel, not a nominal 50 — same measured-vs-nominal drift as FRA400 reporting 402. The 45–55 window covers it with room to spare |
| `INSTRUME` | **`ZWO ASI585MC Pro`** — ⚠️ *not* Canon6D, as the "Canon lens legacy" note below assumed. The 50mm is on the ZWO body, so the folder reads `2026-08-13_Canon50mm_ZWOASI585MCPro`. **Verify the lens is actually a Canon before this is baked in** |
| `FILTER` | absent, as always — filter comes from the filename: `AstronimikL2`, already aliased to `AstronomikL2` (M2) and in `KNOWN_FILTERS` |
| `OBJECT` | `'M 8_1-1'` — the panel is in the header too, so the archive-side scan needs `parse_panel`, not just the ingest scan |
| Other | 30s subs, gain 200, 2026-08-13, `TELESCOP` `ZWO AM5N`, and a new `176deg` rotator component in the filename (harmless — `parse_filter` still reads `parts[-2]`) |

**⚠️ Blocker to clear before ingesting: 50mm has no OTA mapping.** *(cleared —
see the DONE banner; retained for the reasoning.)*
`parse.parse_ota` only covers 170–190 (`FMA180`), 270–290 (`FRA400-07x`) and
390–410 (`FRA400`); everything else returns `"Unknown"`. So all 8 panel
sessions would ingest with `ota='Unknown'`, which (a) bakes `Unknown` into
every `session_id`, (b) trips U2's unknown-OTA badge 8 times, (c) breaks flat
matching, which keys on OTA+camera+filter, and (d) can't even be corrected in
`ingest review`, because the pick-list comes from `parse.KNOWN_OTAS`.

Needs a naming decision before ingest, not after — the OTA is an identity
component, so changing it later is a rename of all 8 rows plus their folders.
Two useful precedents: **20 live sessions already sit at `ota='Unknown'`**
(mostly Canon6D legacy), and the archive already contains a lens-named folder
`NGC 7000/2023-04-16_100mm_Canon6D/`, i.e. a bare focal length used where an
OTA name goes. Pick the convention, add the tolerance window to `parse_ota`,
and add the name to `KNOWN_OTAS` in the same change.

#### The bare `IC 4604` row is NOT a stray — it is a design constraint

Confirmed by Jonathan 2026-08-30: of the five `4604` targets in the catalog,
the bare `IC 4604` row (2023-07-15, 21 frames, Canon6D) is a **legitimate
single-pointing session of the same object**, shot before the mosaic existed.
The other four are the mosaic panels.

So a target legitimately holds **both** panelled and non-panelled sessions at
once. That is not an edge case to tolerate — it is the normal end state for any
object shot single-frame first and mosaicked later. It confirms the nullable
`panel` design (NULL = ordinary session) and adds required behaviour:

- `panel IS NULL` and `panel = '1-1'` rows must coexist under one target, and
  every per-target rollup (integration hours, depth gauge, calibration chips)
  has to stay correct across the mix.
- The target view can't just say "4 panels · 2.1h/panel" — it needs to show the
  single-pointing session alongside the panel breakdown without double-counting
  or implying the two are interchangeable depth.
- Make it a test case, not an afterthought: one target, one NULL-panel session,
  N panelled sessions.

#### Guiding interacts with acquisition *order*, not with mosaics as such

Checked against the live catalog 2026-08-30 (the question was whether **F7**
would help here — it would not, and here is why):

IC 4604's panels were shot in disjoint sequential blocks, e.g. 2025-04-26 ran
`2-2` 23:02→00:11, `2-1` 00:18→01:34, `1-2` 01:39→03:11, `1-1` 03:16→04:30. So
each panel-night already has a clean non-overlapping window, envelope matching
is correct, and the per-panel RMS spread on one night (14.14″ / 9.58″ / 5.50″ /
6.96″) is real signal rather than cross-contamination. Span vs integration
overhead is only ~0.13h on ~1.1h, so F7 would refine those numbers slightly and
change no conclusion. The pending 8-panel mosaic was also shot in blocks.

**The conditional worth remembering:** if panels are ever shot *interleaved*
(P1, P2, P3, P4, P1, …, which some mosaic sequencers do by default to keep
altitude and rotation even), then every panel's envelope spans the whole night,
they overlap completely, and all panels get near-identical whole-night guiding
stats — silently. That is precisely the failure **F7** exists to fix, and it is
undetectable without it. So F7's value for mosaics is a function of acquisition
pattern, not of mosaics themselves. Block-sequential needs nothing.

---

### M2. Sub-folders inside a session folder are scanned as sessions, and their name becomes the *filter*

Filed 2026-08-30, out of F8's first live dry run, which proposed creating
`NGC7000_20250801_FRA400_Canon6D_**Stars**` — a session whose "filter" is
`Stars`. Same family as **M1** in that both are non-standard grouping *inside*
a target that the scanner has no vocabulary for, but the two are not the same
problem and shouldn't share a fix (see the split below).

**What's actually on disk** (checked, not assumed — the first read of this was
wrong):

```
NGC 7000/2025-08-01_FRA400_Canon6D/
  Light_NGC 7000_300.0s_..._ISO1600_20250801-*.fit    ← 12 frames, the session
  20250802_FRA400_NoFilter_RGB_Stars/
    Light_NGC 7000_10.0s_..._ISO800_20250802-*.fit    ← 20 frames
    Light_NGC 7000_30.0s_..._ISO800_20250802-*.fit    ← 20 frames
```

Those 40 files are **raw lights, not processed output** — a deliberate
short-exposure star layer (10s/30s @ ISO800) shot after the main run
(300s @ ISO1600) for star reduction/replacement during processing. The folder
exists *for* processing, which is why it reads as a processed-data folder at a
glance, but nothing in it is a processing artifact. `_Processed` is already in
`cataloger._SKIP_DIR_NAMES_LOWER`; this folder is correctly *not* skipped.

**So the create proposal is right and the diagnosis of "false positive" was
wrong.** Those 40 frames genuinely are not in the catalog. Two things are
wrong around it instead:

1. **The folder name becomes the filter.** `find_lights_folders` collects any
   directory holding FITS, and the filter falls out of the path, so
   `..._RGB_Stars` yields `filter='Stars'`. The folder name even says
   `NoFilter`. A filter value is being invented out of a folder name that
   encodes *purpose*, not filtration — the same class of bug **U2** cleaned up
   when mosaic panel names landed in the filter column.
2. **The parent session's span silently covered it.** F8 also proposes
   `end_utc: 2025-08-02T00:02:11 -> 2025-08-01T22:25:15` on the parent. The
   stored value ran to the *star layer's* last frame, because at ingest both
   sets were grouped into one session by imaging night. That proposal is a
   genuine correction, and it's the same "one folder, two things" hazard F4
   hit from the other direction (`backfill-times` had to filter frames to the
   session's own night because one `lights_path` can hold several).

**Jonathan's intent, confirmed 2026-08-30:** these were shot deliberately —
*"RGB stars to be composited with the narrowband data"*. That's the standard
narrowband workflow (broadband stars grafted onto an NB stack, because NB
stars are colourless and bloated), so this is a **repeatable technique, not a
one-off**. It will recur on every narrowband target he wants natural stars on,
which moves this from "clean up one bad row" to "the catalog needs to be able
to express this".

**The real modelling question, and why it isn't M1's:** a night can contain
more than one *acquisition run* — a narrowband main integration plus a short
broadband star layer. M1's panel is a **spatial** subdivision of one run; this
is a **purpose** subdivision of one night on the same framing.

**But the confirmation above probably settles it cheaply.** The star layer is
shot *through a different filter* from the narrowband run it serves — that's
the entire point of it. Filter is already an identity component, so the two
runs already produce distinct `session_id`s naturally, with no new dimension
at all. On that reading the whole fix is: **take the filter from the frames,
not the folder name**, and this becomes an ordinary second session for the
night, correctly filtered `NoFilter`/RGB, sitting alongside the narrowband one.
No `purpose` column, no schema change, and the per-filter breakdown in the UI
already shows them separately.

Two things to check before committing to that:
- Does anything downstream assume one filter per night per rig? Flat matching
  keys on OTA+camera+filter, so a `NoFilter` star layer wants its own flats —
  which is correct behaviour, but confirm `find_flats` handles the night
  having two.
- Does WBPP prep do anything silly with two sessions on one night that are
  *meant* to be stacked separately? They should be two SESSION_N dirs, which
  is what `prep` already does per session row.

The fallback, if that turns out not to hold: a `purpose`/`layer` column
(`main`/`stars`/`hdr`, default `main`) parallel to M1's `panel`. Skipping the
folder outright is the one option to reject — these are real lights that
belong in the archive and the integration totals.

#### The cheap half — ✅ DONE 2026-08-30 (`0e54759`)

`_filter_from_path` now only trusts the trailing component when it is in
`names.KNOWN_FILTERS`; otherwise it searches the remaining components for one,
and failing that returns `None` so the session reaches U2's queue as
`UnknownFilter` rather than carrying an invented value. The NGC 7000 star
layer now resolves to the `NoFilter` its own folder name states. Also aliased
`AstronimikL2` → `AstronomikL2` (one archive folder is misspelt; with the
guard in place an unaliased typo would silently demote a correctly-filtered
session to `UnknownFilter`).

**Two things this surfaced that were not visible before:**

1. **It fixed 8 mosaic rows as a side effect.** The IC 4604 panel names
   (`IC4604_1-1` …) were sitting in the filter column — U2 knew about this and
   couldn't clean it up because nothing recomputed those rows. They now
   resolve to `UnknownFilter` on disk and appear as **rename** proposals, so
   M1's "the panel name is not a filter" half is already half-solved by the
   guard; what M1 still owes is the `panel` column that gives them somewhere
   correct to live.
2. **`rescan._canonical_session_id` had to canonicalize filter too.** Without
   it those 9 rows (8 panels + the misspelt Moon session) surfaced as
   unrelated delete + create pairs rather than renames — which on apply would
   have dropped `id`/`created_at`/`processed_state`/`session_guiding`. General
   rule now encoded there: **every identity component must be canonicalized on
   both sides, or drift in any one of them reads as "different session".**

Live dry run went 24 → 33 proposals, but the composition is the point:
5 delete (unchanged, all genuine), **12 rename** (was 2), 1 create (now
correctly `NoFilter`, was `Stars`), 15 update.

#### Still open

The modelling question above — whether a star layer is just an ordinary second
session separated by filter (likely), or wants a `purpose`/`layer` column. The
`create` proposal in `/rescan` is now *safe to apply* (it writes `NoFilter`,
not `Stars`), but applying it commits to the "ordinary second session" reading,
so it's worth deciding first rather than by default.

---
### M3. `wbpp` must emit one tree per mosaic panel — ✅ DONE 2026-08-31

> **✅ DONE 2026-08-31** (`745435e` names helpers, `29f552e`/`61cb026` prep,
> `788f07d`/`144a65c` finish, `17d8cf8` mixed-target guard, `09acdf7` picker
> "already prepped?" fix; docs `59817b5`, `7277548`, `7441443`). Shipped:
> `names.wbpp_panel_dir`/`parse_wbpp_panel_dir`/`panel_sort_key`/
> `processed_panel_dir`, one `PANEL_<n>/` tree per panel with the NULL-panel
> path byte-identical, the two-stage finish (per-panel → `in_progress`,
> hand-merged target-level `Output/processed/` → `processed`), and
> `prep._confirm_mixed_panels`. Tests in `tests/test_wbpp.py`,
> `tests/test_wbpp_finish.py`, `tests/test_names.py`.
>
> **One assumption still unconfirmed:** finish reads the hand-merged mosaic
> from target-level `~/WBPP/<slug>/Output/processed/` — the proposal in
> "Open question for Jonathan" below, implemented as specified but never
> confirmed against where PixInsight actually saves the merge. If it lands
> elsewhere, finish silently finds nothing to file.

Filed 2026-08-31 from Jonathan prepping the (now correctly catalogued) IC 4604
mosaic; **design settled the hard way after a full WBPP run** — see the
finding below, which is the durable part of this entry.

**What happens now.** `prep.py:_build_night` (~line 156) loops the night's
sessions and symlinks each into `Lights/FILTER_<name>/`:

```python
for sess in sessions:
    dest = session_dir / "Lights" / f"FILTER_{filter_name}"
```

Sessions are grouped into one `SESSION_N` per `obs_date` (`prep.py:370`), so a
mosaic night's four panel rows — same target, night, optics and filter — all
resolve to the *same* `FILTER_L-Pro/` directory and merge into one flat list.
WBPP then stacks four non-overlapping pointings as a single frame set, and
registration fails. Nothing warns; the frame count just looks unusually large.

#### ⚠️ The finding: WBPP grouping keywords separate *calibration*, not *integration*

Tested end to end by Jonathan 2026-08-31, and this is worth remembering
because the first two thirds of the pipeline make it look like it works.

He set WBPP's grouping keywords to `SESSION` + `PANEL` and built a nested tree
(`SESSION_1/Lights/FILTER_L-Pro/PANEL_1-1/`). WBPP **did** honour it: frames
listed as `SESSION 1 : PANEL 1-1`, and every calibration stage grouped
correctly. But at **final integration it merged all panels into one stack
anyway** and failed, because non-overlapping panels cannot register. There is
no setting that makes integration treat a panel the way it treats a filter.

So a grouping keyword is not a stacking boundary. **The only reliable
separation at integration time is a separate WBPP run** — i.e. a separate
tree. Do not re-attempt the nested-keyword layout; it is not a configuration
problem to solve.

(Superseded by the above: an earlier revision of this entry specified the
nested `PANEL_` level, on the strength of the calibration stages working.
Also superseded: the confirmation that flats need no `PANEL_` level — true,
but moot, since each panel now gets its own tree and its own flats.)

#### The design: a panel level *inside* the target dir

One WBPP run per panel (that part is forced), but nested under the target
rather than as sibling top-level dirs — Jonathan's refinement 2026-08-31:

```
~/WBPP/IC4604/                      <- target_slug(target), unchanged
  PANEL_1-1/
    SESSION_1/  SESSION_2/          <- that panel's nights
      Lights/FILTER_L-Pro/
      Darks/  Flats/FILTER_L-Pro/  FlatDarks/
    Output/                         <- one WBPP run's output, per panel
  PANEL_1-2/  PANEL_2-1/  PANEL_2-2/
  Output/                           <- target level: the merged mosaic
```

Preferred over sibling `~/WBPP/IC4604_P1-1/` dirs because `finish --target
"IC 4604"` resolves one directory and iterates panels inside it, rather than
globbing `IC4604_P*` siblings at the WBPP root, and `~/WBPP/` keeps one entry
per target. **It does not, however, make the finish change smaller** —
`finish.py:56` globs `wbpp_target/SESSION_*` and `finish.py:191` expects
`wbpp_target/Output`; with panels one level down, both miss either way.

Calibration is symlinked into every panel's tree. Real duplication — N copies
of each dark/flat set — but they are **symlinks into the archive**, so the cost
is inodes, not gigabytes. Accepted deliberately; correctness first.

A non-mosaic target is unaffected: `panel IS NULL` keeps producing
`~/WBPP/IC4604/SESSION_N/` exactly as today, with no `PANEL_` level.

#### ⚠️ The finished mosaic belongs to the TARGET, not to any panel

Raised by Jonathan 2026-08-31, and it is the part that makes mosaics
structurally different from everything else in the pipeline. Panels are
stacked separately and then **merged into a single image**. That merged result
is the real deliverable, and it is not any panel's output — so writing it to
`_Processed/<date>/P1-1/` would be wrong for every panel equally.

The archive already encodes the right answer (checked, not assumed):

```
_Processed/2023-07-17/            <- ordinary target
  masters/  process/              <- intermediates, in subdirs
  result_600s.fit                 <- the deliverable, at the TOP level
  starless_result_600s.fit  starmask_result_600s.fit

_Processed/2025-05-29/            <- the IC 4604 mosaic
  1-1/  1-2/  2-1/  2-2/          <- per-panel WBPP output (master/, logs/)
  (no top-level result — not merged yet)
```

Same rule both times: **intermediates in subdirectories, the deliverable at
`_Processed/<date>/` top level.** A mosaic simply has one subdir per panel
where an ordinary target has `masters/`.

Note the existing dirs are bare `1-1`, **not** `P1-1` — M1's layout sketch
claims `P1-1/` is "already the manual convention on disk", which is wrong.
Match the disk (`1-1`) unless deliberately changing it; the `P` prefix earns
its place in the *session_id* (where it disambiguates a flat identifier) but
not inside a directory that already means "panel".

So finish becomes two operations:

| | copies | to | marks sessions |
|---|---|---|---|
| per panel | `PANEL_<n>/Output/master/` + `processed/` | `_Processed/<date>/<n>/` | `in_progress` |
| the mosaic | target-level `Output/processed/` | `_Processed/<date>/` (top) | `processed` |

That falls straight out of the existing `processed_state` enum, which already
distinguishes `in_progress` ("stacked and/or editing, no final export yet")
from `processed`. A stacked-but-unmerged panel is *exactly* `in_progress`, and
a mosaic's panels all become `processed` together when the merge is filed —
which is also correct, since none of them is individually finished.

**Open question for Jonathan:** the merge happens by hand in PixInsight, so
finish cannot find it in a WBPP `Output/master/`. Proposal above is that it
goes in a target-level `~/WBPP/IC4604/Output/processed/`, reusing the existing
"hand-finished work lands in `Output/processed/`" convention one level up.
Confirm that, or name where the merged file actually gets saved.

Calibration is symlinked into every panel's tree. That is real duplication —
N copies of each dark/flat set — but they are **symlinks into the archive**,
so the cost is inodes, not gigabytes. Accepted deliberately; correctness first.

A non-mosaic target is unaffected: `panel IS NULL` keeps producing
`~/WBPP/IC4604/` exactly as today.

#### ⚠️ The `wbpp -> finish` handoff does NOT survive this unchanged

M1's touchpoint table claims "folding the panel into the WBPP slug keeps the
`wbpp -> finish` handoff working unchanged". **That is wrong** — checked
2026-08-31:

- `finish.py:189` does `slug = target_slug(target)` and looks in
  `wbpp_root/<slug>`. From `--target "IC 4604"` that resolves to
  `~/WBPP/IC4604/`, which will not exist for a mosaic — every tree is
  `IC4604_P*`. finish exits with "WBPP target dir not found".
- `finish.py:41` `_build_dest` builds
  `<archive>/01_Deep Sky Objects/<target>/_Processed/<date>/`, with no panel
  level. M1 specifies `_Processed/<date>/P1-1/`, which is also already the
  manual convention on disk (`IC 4604/_Processed/2025-05-29/1-1/`).

So finish needs real work, not none. Suggested shape:

- `finish --target "IC 4604"` finds every `IC4604` **and** `IC4604_P*` tree and
  finishes each into its own `_Processed/<date>/P<panel>/`. One command per
  mosaic, matching `wbpp --target "IC 4604"` having prepped them all.
- `--panel 1-1` to do exactly one.

`names.target_slug` is documented as the single source of truth shared by
`wbpp` and `finish` ("the handoff depends on these staying identical"), so the
panel-aware form belongs there too — a `wbpp_slug(target, panel)` helper both
sides call, rather than either side concatenating `_P{panel}` itself.

#### Scope

- `prep.py`: build one target dir per panel; group rows by panel before the
  existing per-night loop. Leave the NULL-panel path byte-identical.
- `picker.py`: a mosaic target needs a panel step, or an "all panels" choice
  that emits N trees in one go.
- `finish.py`: panel-level SESSION/Output discovery, plus the two-operation
  split above (per-panel -> `in_progress`, merged mosaic -> `processed`).
- `names.py`: `wbpp_slug(target, panel)`.

#### ✅ Resolved: the *mixed* target, guarded on the prep side

Found 2026-08-31 running the real IC 4604 through prep + finish. That target
holds four panels **and** a NULL-panel 2023-07-15 single-pointing night, and
prep does the right thing — `PANEL_1-1/`…`PANEL_2-2/` plus a target-level
`SESSION_1` for the 2023 night. But `<target>/Output/` is then **overloaded**:

- as a mosaic, it is where the hand-merged result goes (what `finish` reads);
- as an ordinary target, it is the WBPP output dir for that target-level
  `SESSION_1`.

It cannot be both. And `finish` in mosaic mode iterates `PANEL_*` and
**silently ignores the target-level `SESSION_*`**, so the single-pointing
night is never copied or marked. Verified in a dry run: "Mosaic detected: 4
panel(s)" and no mention of `SESSION_1`.

**Decided by Jonathan 2026-08-31: option 2 — guard on the prep side, so
`finish` only ever sees a clean tree, mosaic or ordinary, never both.**
Working on a mosaic means targeting its dates specifically anyway, so the
mixed prep is not a case worth supporting; it is a case worth refusing
loudly. Implemented in `prep._confirm_mixed_panels` (`17d8cf8`): keyed on the
*resolved rows* rather than on which flag was used, so `--date A --date B`
spanning a mosaic night and an ordinary one is caught too. It lists which
sessions are which and prints the two commands to run instead.

Verified against the live catalog: a bare `--target "IC 4604"` lists all nine
rows by kind, builds nothing, and exits; the suggested
`--date 2025-04-26 --date 2025-05-24` then produces a clean panels-only tree.

**Also fixed (`09acdf7`, flagged by the prep implementer):**
`_run_interactive`'s "already has SESSION_N dirs" check called
`next_session_num(target_dir)`, which only looks at direct children — right
for numbering (each panel numbers its own sessions) but wrong for "has this
been prepped before?", since a mosaic's `SESSION_N` dirs live under `PANEL_*/`.
The picker skipped its Append/Regenerate/Abort prompt on a fully-prepped
mosaic and silently appended a second set of trees. Now
`prep._has_existing_sessions`, which checks both levels.

**Tests:** one night with four panel sessions plus a NULL-panel session on
another night, asserting four `<slug>_P*` trees plus an unpanelled one, that
each panel's tree carries its own complete calibration, and that a
non-mosaic target's tree is unchanged. Plus a finish-side test that
`--target` alone resolves every panel tree and writes each to its own
`_Processed/<date>/P*/`.

---

## Suggested order for a future session
1. **B1 + B2** (finish + flat-darks) — silent data-pipeline failures, with tests. ✅ DONE
   — **B12** (flat-morning ranking) ✅ DONE 2026-07-29 is the third of the same
   family: calibration matched by a rule that looked right but quietly picked
   the wrong set. **B11** (dark masters at every temperature) is still open and
   is the last known one.
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
7. **W9** — ✅ DONE. All four phases shipped and deployed 2026-07-05→07-07
   (API + client/server split, edit UI, LXC deploy, datasette removed), plus
   the nightly NAS backup, `scripts/dev-snapshot.sh`, the safelight front-end,
   and the 2026-07-13 password-login auth review. **W10** ✅ DONE 2026-07-13.
8. **U2/U3** filter cleanup queue + interactive ingest review (U2 is a natural
   second UI view on the W9 app; U3 benefits from U1's picker helpers).
   U2 ✅ DONE 2026-07-15 (ledger + apply-renames + /queue + target merge),
   deployed same day. U3 ✅ DONE 2026-07-29 — the "close the tap" counterpart
   to U2's backlog cleanup: `ingest review` now confirms target/filter/OTA
   against catalog-seeded pick-lists before anything is copied.
9. **S1** observation sites + SQM-weighted depth — ✅ DONE 2026-07-16,
   hardened 2026-07-29 (modal-across-frames site attribution). Only the
   61 coordinate-less legacy sessions are left as a manual pass.
10. **F3** (calibration-match indicator) — ✅ DONE 2026-07-30. **F4**
    (guide-log stats) followed it — ✅ DONE 2026-07-30: `darkroom logs import`
    archives the logs, `catalog backfill-times` + `scan-guiding` fill
    `session_guiding`, and the night row grew a Guiding column. Autorun parsing
    and scale-relative bands stay deferred inside the F4 entry; per-frame
    windowing was promoted out to **F7** (a scoping item — decide before
    building, since it would cost `scan-guiding` its archive independence).
11. **B11** (`wbpp` symlinks every dark master at every temperature) — ✅ DONE
    2026-07-29. The last of the B1/B2/B12 family of calibration matchers that
    looked right and quietly picked the wrong set. Two follow-ups came out of
    verifying it: **B13** (night-level dark params taken from `sessions[0]`)
    and **F5** (session temperature is a range, not a scalar, on uncooled
    cameras — 5–6C measured drift vs a ±3C window). Neither is urgent; F5 in
    particular should wait until the Canon darks library has enough rungs to
    bracket between, since that is the binding constraint today.
12. **B8** (integration time hardcoded to hours — short subs render `0.0h`),
    then **R3** (the `set_id` builders, the last open refactor — it can create
    duplicate calibration rows, so it needs care rather than a quick pass) and
    **B7**/**R1–R5** leftovers. R1, R2, R4, R5 and B7 all landed 2026-07-29;
    only R3 remains from that block. Litestream (continuous DB replication)
    also lands here as an optional upgrade over the nightly backup.
13. **M1** (mosaic panels as a session dimension) — **ingest half ✅ DONE
    2026-08-30**; the WBPP/UI half and the IC 4604 migration remain. The
    mosaic is now ingestable. Original note follows. **⚠️ WAS THE TOP
    PRIORITY as of 2026-08-30: an 8-panel 50mm
    mosaic around M8 is shot and waiting to be ingested, and Jonathan is
    holding the ingest until this lands.** Ingesting first recreates the
    five-fake-targets mess (IC 4604 is currently 5 separate "targets") and
    doubles the cleanup. Do the ingest half (`parse_panel` + `panel` column +
    `make_session_id`); the WBPP/UI half can follow. **Clear the 50mm OTA
    blocker in the same change** — `parse_ota` has no window for it, so all 8
    sessions would ingest as `ota='Unknown'` and can't be fixed in `ingest
    review` either (its pick-list is `KNOWN_OTAS`). Also closes U2's known
    same-night panel-collision gap. See M1 for the confirmed facts.
14. **M3** (panel-aware `wbpp` prep) — filed 2026-08-31, design settled and
    tested in PixInsight. This is the last thing standing between the
    catalogued mosaics and actually stacking them: `wbpp` currently merges a
    mosaic night's four panels into one flat symlink list, which cannot
    register. Small, well-specified change to `prep.py:_build_night`.
15. **U4** and **F8** — both ✅ DONE (U4 `46653ea`, F8 merged 2026-08-30).
    Filed 2026-08-29 out of the SH2-101 2026-07-19 mis-slew fixup (5 of 92
    subs actually on target; the rest needed hand-built catalog corrections
    because no rescan path reached the live catalog) — that hand-correction
    is now `catalog rescan-archive`. Deployed to the LXC 2026-08-30, and its
    first 24 proposals are queued in `/rescan` awaiting review.
    **Open follow-up:** work that queue (hold the one `create` — see **M2**),
    and re-run `scan-guiding` for any session whose `start_utc`/`end_utc` you
    end up changing, since `backfill-times` only fills NULL spans and won't
    revisit them.
