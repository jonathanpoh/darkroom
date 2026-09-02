# BLOCKERS — things only Jonathan can do

Work that is waiting on **you**, not on code: decisions nobody else can make,
and commands that need the NAS mounted, the SD card present, or a judgement
call about your own data.

`BACKLOG.md` is the engineering queue. This file is the human queue. If
something here is unblocked, the corresponding backlog item can move.

> Catalog figures below were read from the live server (`darkroom.jpoh.net`) on
> **2026-09-02**. Re-check before trusting them — `darkroom catalog list`, or
> the web UI.

## ⚡ Start here (2026-09-02)

Read live: **244 sessions, 0 pending `/rescan` proposals, 0 pending renames,
0 `Unknown` OTAs, 0 NULL `start_utc`.** Every queue that used to be listed here
is drained. What is left are judgement calls about your own data.

**Actually waiting on you, in order:**

1. **Were the April-2023 flats shot through the L-Pro?** (**#14 residual**)
   All nine `Canon100mm` flat sets still carry `filter = NULL`, so the M 42
   2023-04-15 session matches its darks but **none of its flats**. If they were
   L-Pro, rename `00_Calibration/Flats/100mm_Canon6D/` to
   `Canon100mm_Canon6D_L-Pro` and re-run `scan-calibration`. Only you know.
2. **The M 8 mosaic has no flats** (**#1**) — confirmed live: there is not a
   single `Canon50mm` + `ZWOASI585MCPro` flat set in the catalog, because none
   were shot that night. Decide: shoot a matching set now, or stack the 8 panels
   without flats.
3. **Keep or delete the 2-frame `IC 4604` stray** (**#3**) —
   `IC4604_20250427_FRA400_Canon6D_L-Pro_P1-2`, a 4-minute aborted start past
   the night boundary. It is now correctly registered in its own folder, so this
   is purely "is it worth stacking".
4. **The no-filter backlog** (**#10**) — **69 sessions**, down from 95. Long and
   low-intensity, best in batches by night; not blocking anything.
5. **Push and deploy** (**#7**) — local `main` is **ahead 4** of origin.

**Closed since the last pass, no action needed:** the `/rescan` queue (worked to
zero — 53 proposals lifetime), the trap create/delete pair (**#2b**), the
`Stars` split (**#4** — you chose `NoFilter`; both sessions are live), the
`INSTRUME` rewrite (**#14**), the M 8 mosaic ingest (**#1** — 8 panels
`P1-1`…`P4-2`, 10 frames each), the rename ledger, and the last NULL
`start_utc` (**#8**).

---

## 🔴 Blocking other work

### 1. Ingest the M8 mosaic ✅ DONE — but it has no flats

Ingested. Live as of 2026-09-02: **8 sessions, target `M 8`, 2026-08-12,
panels `1-1` … `4-2`, OTA `Canon50mm`, camera `ZWOASI585MCPro`, filter
`AstronomikL2`, 10 frames each**, under
`.../Lights/AstronomikL2/P1-1/` … `/P4-2/`. The first live exercise of M1's
panel path, and it came out clean.

As predicted, `scan-guiding` lists those 8 as unmatched — no guidescope at
50mm, so there is no PHD2 log for that night. Correct, not a failure.

**⚠️ Still open: there are no flats for it.** Checked live — the catalog holds
**zero** `Canon50mm` + `ZWOASI585MCPro` flat sets, because none were shot that
night. `wbpp` will build all 8 panel trees with an empty `Flats/`. Your call:
shoot a matching set (the adapter makes the optical path repeatable, so a set
shot now is still valid), or stack without and accept the vignetting.

### 2. Work the `/rescan` queue ✅ DONE — the queue is empty

Verified live 2026-09-02: **0 pending proposals.** Lifetime total 53 —
21 `rename`, 18 `update`, 9 `delete` and 4 `create` applied, 1 `create`
dismissed. Both of the holds recorded here are resolved: the `Stars` `create`
(#4) and the trap pair (#2b).

The one durable lesson, for the next time the queue fills:

- **After applying anything that changes a session's `start_utc`/`end_utc`,
  re-run `darkroom catalog scan-guiding --apply`.** `backfill-times` only fills
  NULLs and will not revisit a row, so guiding stats derived from the old span
  stay stale silently.
- A `delete` against an unmounted NAS is indistinguishable from a genuinely
  removed session. Deletes now confirm before removing a row, but still read
  each one.

### 3. Supply the real filter for the IC 4604 mosaic nights ✅ DONE — one stray left to judge

You supplied it: **`L-Pro`**. The five fake targets (`IC 4604_1-1` …) are gone,
folded into one `IC 4604` with `panel` set, and the folders were moved to match.
Live 2026-09-02, catalog and archive agreeing frame-for-frame:

| Session | Panel | Night | Frames |
|---|---|---|---|
| `IC4604_20230715_Canon100mm_Canon6D_L-Pro` | — | 2023-07-15 | 21 |
| `IC4604_20250426_..._P1-1` / `P1-2` / `P2-1` / `P2-2` | 1-1…2-2 | 2025-04-26 | 33, 33, 35, 32 |
| `IC4604_20250427_..._P1-2` | 1-2 | 2025-04-27 | **2** |
| `IC4604_20250524_..._P1-1` / `P1-2` / `P2-1` / `P2-2` | 1-1…2-2 | 2025-05-24 | 19, 30, 30, 28 |

Ten rows, not the nine BACKLOG.md claimed, and **all four** panels were
revisited on 2025-05-24 — the mosaic is two full nights plus a stray.

The bare 2023-07-15 row is a **legitimate single-pointing session**, not a
stray panel; its `panel` is correctly NULL. A target holding both panelled and
non-panelled sessions is the intended design — though note that `wbpp` now
**refuses** to prep both in one go (M3's mixed-target guard), so work the
mosaic by date.

**⚠️ Left to judge: the 2-frame 2025-04-27 stray.** A 4-minute aborted start
past the night boundary. It is now correctly registered in its own folder, so
nothing is broken either way — the only question is whether 2 frames are worth
carrying. Delete the row and the folder, or leave it.

### 2b. One `/rescan` pair would have destroyed a `processed` row ✅ DONE

Resolved as prescribed — the OTA was edited in the web UI rather than the
delete + create pair being applied, so the row kept its `processed` state and
its guiding row and was renamed in place.

```
was:  NGC7000_20230914_Unknown_Canon6D_UnknownFilter     fl=53.0, state=processed
now:  NGC7000_20230914_Canon50mm_Canon6D_UnknownFilter
```

**Keep the pattern, it will recur.** M1's `Canon50mm` window (45–60)
retroactively reclassified that night. `rescan` cannot pair an OTA change
across the `session_id`, so it will always propose delete + create for one —
and applying that drops `processed` state and the guiding row (the B15 failure
mode). Any rescan pair that is the *same session under a new identity* is an
**edit in the UI**, never an apply.

### 4. The `Stars` sub-folder ✅ DONE — decided 2026-09-01, applied

A session folder can contain a sub-folder that is **not** a filter and not a
mosaic panel — `NGC 7000/2025-08-01_FRA400_Canon6D/20250802_FRA400_NoFilter_RGB_Stars/`,
a broadband star layer shot to be composited onto narrowband data.

**Decision: it is an ordinary session in its own right**, separated by filter,
not a new dimension. Filter is already an identity component, so the two runs
produce distinct `session_id`s with no schema change — and filter *must* stay
at session level, because flat matching keys on OTA + camera + filter. Folding
the star layer into the narrowband session would match unfiltered frames to
L-Extreme flats. It needs its own calibration frames, which is exactly what
being its own session gives it.

End state, two sessions as siblings under one night:

```
NGC 7000/2025-08-01_FRA400_Canon6D/
  Lights/L-Extreme/   ← 12 × 300s   (narrowband run)
  Lights/NoFilter/    ← 40 × 10s+30s (star layer)
```

**Done.** Both sessions are live as of 2026-09-02, and you chose **`NoFilter`**
for the star layer:

```
NGC7000_20250801_FRA400_Canon6D_L-Extreme   12 frames  .../Lights/L-Extreme
NGC7000_20250801_FRA400_Canon6D_NoFilter    40 frames  .../Lights/NoFilter
```

Worth keeping from the three-step sequence it took, because the same shape will
recur on any *deepening* rename (a session folder gaining a `Lights/<filter>/`
level it did not have):

- Setting the parent's filter queues a rename that `apply-renames` reports as
  **conflict** while the old frames are still loose in the folder above. Move
  them by hand into the new `Lights/<filter>/`, then it acks.
- A `create` proposal needed `a218e07` deployed — creates were broken before it.
- Re-sending a `filter` **unchanged** via the API is the way to force a
  `lights_path` recompute when the row is right but the folder is not.

Multi-exposure bracketing — the HDR and solar/lunar case this was originally
tangled with — is now **F11**, and is deliberately a separate problem.

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

Verified live **2026-09-02**: the ledger is still **empty**. The 8 IC 4604
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

The `IC4604_20250427` 2-frame row is back and correct — its 2 frames now sit
in their own `2025-04-27_FRA400_Canon6D/Lights/L-Pro/P1-2/` folder, registered.
Whether to keep them at all is #3.

### 7. Push and deploy — ⚠️ 4 commits waiting

Local `main` is **ahead 4** of origin as of 2026-09-02. Push is your step (your
key), then pull + deploy on the LXC:

| | |
|---|---|
| `0682665` | M1's web half — panel rollups, editable panel, panel-aware merge |
| `7a90874` | **B17** — integration time is the per-frame sum, not a product |
| `c3fd30c` | BACKLOG: B17 done; M3 was already done |
| `f3af12a` | BLOCKERS: 2b done |

Nothing here needs a schema migration. B17 changes what **new** ingests write;
the 12 live rows that disagree heal on the next `rescan-archive` as `safe`-tier
updates, and ingest no longer flips them back.

**Previous deploy: 2026-08-31, 23:45 WEST, prod on `1d232db`** — `acc9bc7`
(nested rename classification), all of **M3** (panel-aware `wbpp` prep,
two-stage `finish`, mixed-target guard, picker fix) and **F9** (Canon lens OTAs
+ the acquisition-date rule), including the calibration-upsert fix that lets a
rescan correct an `ota` at all.

Rollback backups on the server, newest last:
`astro_catalog-pre-M1-20260831-090848.db`, `-pre-F9-20260831-222645.db`,
`-pre-F9cal-20260831-224730.db`, `-pre-deploy-20260831-225233.db`.

Post-deploy state, read from the server 2026-09-02: **244 sessions, 1050
calibration sets, 0 pending renames, 0 pending rescan proposals, 0 `Unknown`
OTAs.**

### 8. One session still has a NULL `start_utc` ✅ DONE

Verified live 2026-09-02: **no session has a NULL `start_utc`.**
`NGC7000_20260616_FRA400-07x_ZWOASI585MCPro_L-Synergy` now carries a span, so
every row in the catalog can match a guide log.

---

### 14. Rewrite `INSTRUME` on the April-2023 files ✅ DONE 2026-09-01

`ASCOM Camera Driver` was the acquisition software's generic driver string
(BackyardEOS or N.I.N.A., not the ASIAir), not a camera. Rewritten to
`Canon EOS 6D` — Jonathan did the 40 light frames, then the 106 calibration
frames followed:

| Files | Folder |
|---|---|
| 40 | `01_Deep Sky Objects/M 42/2023-04-15_Canon100mm_Canon6D/Lights/L-Pro` |
| 40 | `00_Calibration/Darks/Canon6D/Raw/20s/2023-04-15` |
| 26 | `00_Calibration/Bias/Canon6D/Raw/2023-04-17` |
| 40 | `00_Calibration/Flats/100mm_Canon6D/2023-04-17` |

`scan-calibration` then re-registered all 106 under `Canon6D` (8 new sets,
frame counts matching the old ones 1:1), and the 8 superseded
`ASCOMCameraDriver` rows were deleted. **The M 42 session now matches its
darks — 3 sets, where it previously matched none.**

Byte-for-byte backups of the 106 originals, with checksums and a README, are at
`_backups/2026-09-01_INSTRUME-rewrite/` — deliberately at archive root rather
than beside the originals, because `_SKIP_DIR_NAMES_LOWER` only guards the
*lights* walk (`find_lights_folders`); `scan-calibration` walks whatever root it
is given, so a backup folder inside `00_Calibration` would have registered as
duplicate calibration sets.

**⚠️ Residual, still open — the flats still don't match.** Re-checked live
2026-09-02: all nine `Canon100mm` flat sets carry `filter = NULL`. They now
carry the right
`ota='Canon100mm'` and `camera='Canon6D'`, but `filter` is NULL while the
session is `L-Pro`, and flat matching keys on OTA + camera + filter. The folder
`Flats/100mm_Canon6D/` has no filter component and ASIAir writes no FILTER
header, so that value is recorded nowhere. **Only you know whether those flats
were shot through the L-Pro.** If they were, rename the folder to
`Canon100mm_Canon6D_L-Pro` and re-run `scan-calibration`.

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

Verified live 2026-09-02: **69 of 244 sessions have no filter recorded** (28% of
the catalog) — down from 95 of 240, so a batch of 26 has been recovered since.

| | |
|---|---|
| By camera | Canon6D 44, ZWOASI585MCPro 25 |

Mostly legacy and mostly Canon6D. These surface in the web UI's filter queue.
Not urgent; it's a long, low-intensity pass rather than a blocker, and worth
doing in batches by night — the closer to the shoot you can still remember, the
more recoverable it is.

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
