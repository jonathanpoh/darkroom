# BLOCKERS — things only Jonathan can do

Work that is waiting on **you**, not on code: decisions nobody else can make,
and commands that need the NAS mounted, the SD card present, or a judgement
call about your own data.

`BACKLOG.md` is the engineering queue. This file is the human queue. If
something here is unblocked, the corresponding backlog item can move.

> Catalog figures below were read from the live server (`darkroom.jpoh.net`) on
> **2026-08-31**. Re-check before trusting them — `darkroom catalog list`, or
> the web UI.

## ⚡ Start here (2026-08-31, evening)

**Actually waiting on you, in order:**

1. **One `/rescan` pair is a trap — do not apply it as-is.** See **#2b**. Edit
   that session's OTA in the web UI instead of applying delete + create. (Its
   row now reads `Canon50mm` rather than `Unknown`, but the trap is unchanged.)
2. **Decide the `Stars` sub-folder** (**#4**) — it is the only thing holding
   the `create` proposal in `/rescan`, and it is a modelling call nobody else
   can make.
3. **Rewrite `INSTRUME` on 154 April-2023 files** (**#14**) — the last of the
   `ASCOMCameraDriver` mess. Fixes the M 42 session that currently matches none
   of its own flats.
4. **Then work the `/rescan` deletes** (**#2b**) — safe to apply now that the
   orphan-guiding fix is deployed; before it, each delete left a stale row.

**Done today, no action needed** — deploy (prod on `c7a5ef7`), the F9 optics
decision applied across the whole corpus, the rename ledger drained to zero,
and every folder that held two sessions' frames split apart. The catalog now
has **zero `Unknown` OTAs** and **zero `lights_path`s shared by two sessions**.

**Two small loose ends:** 2 unregistered frames in
`IC 4604/2025-04-26_.../P1-2` (they belong to the night of 2025-04-27 — a
4-minute aborted start; register or relocate), and the M 8 mosaic has no flats
because none were shot that night (**#1**) — stack it with flats from another
`Canon50mm` + `ZWOASI585MCPro` occasion, or without.

---

## 🔴 Blocking other work

### 1. Ingest the M8 mosaic ✅ done

**Status: unblocked as of 2026-08-30 — this is the next thing to do.**

The 8-panel 50mm mosaic (shot 2026-08-13) has been held since M1's ingest half
didn't exist. It does now: `parse_panel`, the `sessions.panel` column, the
`Canon50mm` OTA, and the wiring through scan → review → commit are all on
`main`. Everything needed to ingest it correctly is in place.

```bash
darkroom ingest scan --asiair <staging> --manifest ~/ingest/m8-mosaic.yaml
darkroom ingest review ~/ingest/m8-mosaic.yaml
darkroom ingest commit ~/ingest/m8-mosaic.yaml
```

**Check in `review` before committing** — this is the first exercise of the
panel path on live data:

- 8 session entries, target `M 8`, panels `1-1` … `4-2` (a 4×2 grid).
- OTA `Canon50mm`, camera `ZWOASI585MCPro`, filter `AstronomikL2`.
- Destinations ending `.../Lights/AstronomikL2/P1-1/` … `/P4-2/`.

Expect `scan-guiding` to list those 8 sessions as **unmatched** afterwards —
no guidescope was used at 50mm, so there is no PHD2 log for that night. That is
correct, not a failure to debug.

### 2. Work the `/rescan` queue — 21 pending proposals ✅ done except 'Stars' create and all deletes

Verified live 2026-08-30: **15 `update`, 5 `delete`, 1 `create`** pending
(12 `rename` proposals from the earlier pass are already applied).

Review them at `/rescan` in the web UI, then apply.

- **Hold the single `create`.** It is the `NGC 7000/2025-08-01` `Stars`
  sub-folder — a broadband star layer shot to be composited onto narrowband
  data. Applying it would create a session whose "filter" is `Stars`. It waits
  on decision **#4** below.
- The 5 `delete` proposals now ask for confirmation before removing a row
  (fixed in `840605b`), but still read each one: a `delete` against an
  unmounted NAS is indistinguishable from a genuinely removed session.
- After applying anything that changes a session's `start_utc`/`end_utc`,
  **re-run `darkroom catalog scan-guiding --apply`**. `backfill-times` only
  fills NULLs and will not revisit a row, so the guiding stats derived from the
  old span stay stale silently.

### 3. Supply the real filter for the IC 4604 mosaic nights ✅ Done

**Nobody has this information but you, and it is not recoverable from disk.**

Those rows were catalogued by a scan that read `Lights/<subdir>` as the filter,
so the panel name landed in the filter column. M2's `KNOWN_FILTERS` guard
(`0e54759`) then correctly evicted those bogus values — which means the **real
filter for those nights is now recorded nowhere**.

Live state (10 rows across 5 fake targets, all with `filter = NULL`):

| Target (as catalogued) | Nights | Frames |
|---|---|---|
| `IC 4604` | 2023-07-15 | 21 |
| `IC 4604_1-1` | 2025-04-26, 2025-05-24 | 33, 18 |
| `IC 4604_1-2` | 2025-04-26, 2025-04-27, 2025-05-24 | 30, **2**, 23 |
| `IC 4604_2-1` | 2025-04-26, 2025-05-24 | 29, 27 |
| `IC 4604_2-2` | 2025-04-26, 2025-05-24 | 22, 24 |

Two corrections to what BACKLOG.md says about this data:

- It says **9** rows; there are **10**.
- It says only panel `1-1` was revisited on 2025-05-24. In fact **all four
  panels** were revisited that night. The mosaic is 2 full nights plus a stray.

Notes for the migration pass:

- The bare `IC 4604` (2023-07-15, Canon6D, 21 frames) is a **legitimate
  single-pointing session**, not a stray panel. Leave its `panel` NULL. A
  target holding both panelled and non-panelled sessions is the intended
  design, not a mess to clean up.
- The 2-frame `IC 4604_1-2` on 2025-04-27 looks like a tail past the night
  boundary. Decide whether to keep or delete it.
- Once you supply the filter, each row is **one** edit setting target + panel +
  filter together. Target-only hits the same-night collision — though M1's
  `panel` in the session_id is exactly what now makes that edit possible.
- Then run `darkroom catalog apply-renames --archive … --apply` to move the
  folders (see #6).

### 2b. ⚠️ One `/rescan` pair would destroy a `processed` row

Queue as of 2026-08-31: **6 `delete` + 2 `create` pending** (16 updates and 21
renames already applied). One create/delete pair is the *same session*:

```
delete  NGC7000_20230914_Unknown_Canon6D_UnknownFilter    fl=53.0, state=processed
create  NGC7000_20230914_Canon50mm_Canon6D_UnknownFilter
```

M1's `Canon50mm` window (45–55) retroactively reclassified that 2023 night,
which shot at 53mm. `rescan` can't pair an OTA change across the session_id, so
it proposes delete + create — and **applying that drops the row's `processed`
state and any guiding row** (the B15 failure mode).

**Do this instead:** edit its OTA to `Canon50mm` in the web UI. That is an
identity edit, so it renames the row in place and carries everything forward,
then `apply-renames` moves the folder. Only this one session is affected.

### 4. Decide how a `Stars` sub-folder should be modelled (M2's open half)

A session folder can contain a sub-folder that is **not** a filter and not a
mosaic panel — `NGC 7000/2025-08-01_FRA400_Canon6D/Stars/`, a broadband star
layer shot to be composited onto narrowband data.

The cheap half is done: the scanner no longer invents `filter='Stars'`. The
open half is what it *should* be. M1 settled the analogous question for panels
(a nullable identity column), and the backlog sketches a parallel
`layer` column (`main` / `stars` / `hdr`, default `main`).

**This blocks the one `create` proposal in #2.** Until it's decided, that
proposal stays queued.

---

## 🟠 Do soon — not blocking, but degrading

### 5. Run `darkroom logs import` ✅ done

Run by Jonathan (the file just didn't get updated). Verified after his
re-import, 2026-08-31: **292 of 292** non-CHN files in
`~/02_Astrophotography/01_ASIAir/ASIAIR/log` are archived under
`00_Logs/ASIAir/`, byte-for-byte identical, with nothing stale on the archive
side.

The one earlier miss was a mangled extension (`._2txt` instead of `_2.txt`) on
a `_2`-disambiguated duplicate filename, so the `*.txt` glob could not see it.
Renamed at the source and re-imported. Worth knowing the ASIAir can emit two
logs with the *same* filename, and the `_N` suffix is Jonathan's manual
disambiguation — `logs import` has no opinion about it.

`scan-guiding` against the archived logs reports **118 logs, 2212 guiding
segments, 150 sessions matched** — which is exactly what the catalog already
holds, so the import brought no new guiding coverage. 5 logs match no session;
that is expected (the M 8 mosaic night has no guide log at all, since no
guidescope is used at 50mm).

### 6. Drain the rename ledger ✅ done — 0 pending

Verified live **2026-08-31, evening**: the ledger is **empty**. The 8 IC 4604
panel moves, the 28 F9 renames and the 6 session-split recomputes have all been
applied. The two `Sh2-101` case-only conflicts are gone with them.

Two notes worth keeping, both learned the hard way today:

- **A "conflict" can mean the move is already done.** When two sessions used to
  share one folder, splitting them leaves the old path existing (it now holds
  the *other* session's `Lights/`), so `apply-renames` correctly refuses. Verify
  the new path holds the right frame count and the old path has no loose
  frames, then delete that ledger row (`DELETE /api/pending-renames/{id}`).
- **The ledger is a separate queue from `/rescan`.** `/rescan` holds proposals;
  folder moves only ever appear here.

```bash
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE"           # dry run
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE" --apply
```

The old `IC4604_20250427` 2-frame row is gone, and its 2 frames sit
unregistered in the 2025-04-26 `P1-2` folder — see Start here #6.

### 7. Push and deploy ✅ done — prod on `1d232db`

Deployed **2026-08-31, 23:45 WEST** (an earlier deploy that evening put
`c7a5ef7` live; this one adds the F10 filing and the orphan-guiding fix). That shipped `acc9bc7` (nested rename
classification), all of **M3** (panel-aware `wbpp` prep, two-stage `finish`,
mixed-target guard, picker fix) and **F9** (Canon lens OTAs + the
acquisition-date rule), including the calibration-upsert fix that lets a rescan
correct an `ota` at all.

Rollback backups on the server, newest last:
`astro_catalog-pre-M1-20260831-090848.db`, `-pre-F9-20260831-222645.db`,
`-pre-F9cal-20260831-224730.db`, `-pre-deploy-20260831-225233.db`.

Everything local is pushed and deployed. Post-deploy state, read from the
server: **247 sessions, 150 guiding rows (150 joined), 1050 calibration sets,
0 pending renames, 0 `Unknown` OTAs.**

### 8. One session still has a NULL `start_utc`

`NGC7000_20260616_FRA400-07x_ZWOASI585MCPro_L-Synergy` (2026-06-16) has no
wall-clock span, so it can never match a guide log.

`backfill-times` derives the span from the FITS frames, so this usually means
the frames aren't where the catalog thinks they are. Worth a look at the
folder before re-running the backfill.

---

### 14. Rewrite `INSTRUME` on the 154 April-2023 files

Those frames record `INSTRUME = 'ASCOM Camera Driver'` — the acquisition
software's generic driver string (BackyardEOS or N.I.N.A., not the ASIAir),
not a camera. It is your **Canon 6D**, confirmed 2026-08-31.

`camera` is read from that header, so the string propagates into `session_id`,
folder names and `set_id`. The M 42 2023-04-15 session row has already been
corrected by hand, but its own flats have not — which is why that session
currently matches **none** of its flats.

**Why not just alias it in code.** A `_CAMERA_ALIASES` entry mapping
`ASCOMCameraDriver → Canon6D` was considered and rejected: the string is
generic, so the alias becomes silently wrong the first time the ZWO is driven
through N.I.N.A./ASCOM — and unlike F9's optics, there is no date that could
disambiguate it. Fix the data, not the inference.

**Scope, measured live 2026-08-31 — 154 files in 6 folders:**

| Files | Folder |
|---|---|
| 26 | `00_Calibration/Bias/Canon6D/Raw/2023-04-17` |
| 40 | `00_Calibration/Darks/Canon6D/Raw/20s/2023-04-15` |
| 40 | `00_Calibration/Flats/100mm_Canon6D/2023-04-17` |
| 40 | `01_Deep Sky Objects/M 42/2023-04-15_Canon100mm_Canon6D/Lights/L-Pro` |
| 8 | `01_Deep Sky Objects/M 42/_Processed/2023-04-{17,18}` |

Three of those folders are themselves named `Canon6D`, which is the
corroboration. The 8 `_Processed` files are derived products and can be left.

```bash
# per file, in place:  fits.setval(path, "INSTRUME", value="Canon EOS 6D")
darkroom catalog scan-calibration "$DARKROOM_ARCHIVE/00_Calibration"
```

**Two catches.**

- `camera` is part of `set_id`, so the rescan creates **new** calibration rows
  rather than updating the old ones — unlike `ota`, which now updates in place
  (`c7a5ef7`). The 3 stale `ASCOMCameraDriver` flat rows must be retired by
  hand afterwards; there is no calibration-delete endpoint, so it is an
  `ssh` + `sqlite3` job on the LXC.
- This writes to original archive files. Take a backup of the 154 first — it is
  the only step in this queue that modifies frame data rather than moving it.

---

## 🟡 Decisions with no deadline — but cheaper now than later

### 9. Name the Canon EF 100-400mm zoom ✅ decided and applied

Settled 2026-08-31. Each marked zoom stop is its own OTA — `Canon100mm`,
`Canon135mm`, `Canon200mm`, `Canon300mm`, `Canon400mm` — rather than one
`Canon100-400`, because flat matching keys on OTA and a single name for the
whole range would let a 100mm flat match a 400mm light.

The tie between the zoom and the scopes is broken by **date**: you bought the
FMA180 Pro in January 2023 and the FRA400 in January 2025, so a 394mm frame
from 2023 cannot be an FRA400. `parse_ota` takes an `obs_date` and falls
through to the lens when a scope window matches a night that predates it.

Applied to the whole corpus: **28 sessions and 175 calibration sets**
corrected, no session in the catalog is `Unknown` any more, and the `56mm`
question resolved as the fifty — the flats read `FOCALLEN 59` and it
plate-solves to 51mm, so the window now runs 45–60.

**Still ambiguous going forward:** a zoom night shot *today* at 180 or 400mm is
indistinguishable from the scope, and needs a hand correction in `ingest
review`. The date rule is retrospective only.

### 10. The bulk no-filter backlog

Verified live 2026-08-30: **95 of 240 sessions had no filter recorded** (40% of
the catalog). Recheck — the catalog is now 247 sessions and you have corrected
a batch since.

| | |
|---|---|
| By camera | Canon6D 70, ZWOASI585MCPro 25 |
| By year | 2023: 21 · 2024: 17 · 2025: 57 |

Mostly legacy, and mostly Canon6D — but 57 are from 2025, which is recent
enough to be worth recovering while you still remember. These surface in the
web UI's filter queue. Not urgent; it's a long, low-intensity pass rather than
a blocker, and worth doing in batches by night.

~~Related: 20 sessions sit at `ota='Unknown'`~~ ✅ **all resolved** by F9 —
the catalog now holds zero `Unknown` OTAs. The filter backlog above is
unaffected and still stands.

### 11. Shoot more Canon darks, so F5 can bracket (backlog **F5**)

Session temperature on an uncooled camera is a **range, not a scalar** — 5–6°C
of measured drift against a ±3°C matching window. The fix is bracketing darks
by temperature, but that needs a darks library with enough rungs to bracket
*between*, which is the binding constraint today.

This is a shooting task, not a coding one. F5 waits on it.

### 12. Verify ASIAir site coordinates before any field session

The ASIAir takes GPS from the phone, and WiFi geolocation gives **confidently
wrong** coordinates. Those land in `SITELAT`/`SITELONG` and then in the catalog,
where they feed site matching and the home-equivalent-hours weighting.

Check the coordinates on the tablet before a dark-site trip. Wrong coordinates
are much harder to notice after the fact than to prevent.

### 13. Triage "finalize / promote" workflow

Deferred by you previously. The lights-layout reorganisation and the triage
tool are done; what's missing is the step that promotes a triaged result into
the archive proper. Still deferred — listed here so it doesn't get lost.

---

## Quick status check

```bash
# What's queued for me?
darkroom catalog rescan-archive --archive "$DARKROOM_ARCHIVE"   # dry run
darkroom catalog apply-renames  --archive "$DARKROOM_ARCHIVE"   # dry run

# What does the catalog think?
darkroom catalog list --target "IC 4604"
```

Or open the web UI — `/rescan` for proposals, the session list for the filter
and unknown-OTA queues.
