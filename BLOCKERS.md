# BLOCKERS — things only Jonathan can do

Work that is waiting on **you**, not on code: decisions nobody else can make,
and commands that need the NAS mounted, the SD card present, or a judgement
call about your own data.

`BACKLOG.md` is the engineering queue. This file is the human queue. If
something here is unblocked, the corresponding backlog item can move.

> Catalog figures below were read from the live server (`darkroom.jpoh.net`) on
> **2026-08-31**. Re-check before trusting them — `darkroom catalog list`, or
> the web UI.

## ⚡ Start here (2026-08-31)

1. **One `/rescan` pair is a trap — do not apply it as-is.** See #2.
2. **Deploy** — 11 local commits are unpushed (M3 + the rename fix). See #7.
3. **The camera-lens / focal-length decision (F9)** — you said you'd come back
   to this; the data you need is in #9.

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

### 5. Run `darkroom logs import` — it has never been run

The ASIAir logs still exist **only on the Mac**. The SD card gets rotated and
cleared, so anything not yet copied off is at risk, and `scan-guiding` can only
see what has been archived.

```bash
darkroom logs import --source <asiair-log-dir>            # dry run first
darkroom logs import --source <asiair-log-dir> --apply
```

Read-only on the source; skips `*_CHN.txt` and anything already archived at the
same size.

### 6. Drain the rename ledger — 12 pending, 8 of them the IC 4604 panel moves

Verified live **2026-08-31**: **12 pending**, dry run reports
`8 applied, 1 already_done, 2 conflict, 1 missing`.

**The 8 IC 4604 panel folder moves are queued and ready** — they collapse the
four fake top-level targets (`IC 4604_1-1` …) back under `IC 4604` with proper
`P1-1` … `P2-2` panel dirs. Nothing is wrong with them; they just have not been
applied yet. Note the rename ledger is a **separate queue from `/rescan`** —
`/rescan` holds proposals, folder moves only ever show up here.

```bash
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE"           # dry run
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE" --apply
```

The leftovers, all diagnosed 2026-08-31:

- **2 × `Sh2-101` conflicts — deferred by you, a known one-off.** `SH2-101` and
  `Sh2-101` are the *same inode* on the case-insensitive SMB mount, so the
  "both old and new exist" guard is trivially true for any case-only rename.
  The move is already done in substance. Left as-is deliberately; revisit only
  if case normalisation comes up again.
- **1 `missing` — `IC4604_20250427_..._P1-2`, the 2-frame tail. Your call.**
  Its frames were swept into the 2025-04-26 folder during the consolidation
  (disk holds 35 frames there against the catalog's 30, and 30 vs 23 on
  05-24). That row is orphaned: delete it, then re-run `rescan-archive` to
  true up the two frame counts.
- ~~1 `conflict` on `NGC 7380/2025-09-13`~~ ✅ **fixed in code** (`acc9bc7`) —
  it was a false alarm, not a data problem. See below.

Verified live 2026-08-30: **13 pending renames**.

Every one is a folder move the server owes but cannot perform — the LXC has no
NAS mount, so a web-UI identity edit updates the catalog immediately and
records the move for the Mac to execute.

```bash
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE"           # dry run
darkroom catalog apply-renames --archive "$DARKROOM_ARCHIVE" --apply
```

Until this runs, the catalog and the archive disagree about those 13 folders.

### 7. Push and deploy — 11 commits waiting

Deployed to the LXC (prod on `718f3cd`); the `panel` migration applied cleanly,
240 sessions and 151 guiding rows intact. Rollback backup on the server at
`/var/lib/darkroom/backups/astro_catalog-pre-M1-20260831-090848.db`.

**Not yet pushed or deployed (11 commits on local `main`):** `acc9bc7` (nested
rename classification) and all of **M3** — panel-aware `wbpp` prep, the
two-stage `finish`, the mixed-target guard and the picker fix.

None of it is urgent for the server: `wbpp`, `finish` and `apply-renames` all
run on the Mac, and the `panel` column the web UI needs is already deployed. Push
when convenient.

### 8. One session still has a NULL `start_utc`

`NGC7000_20260616_FRA400-07x_ZWOASI585MCPro_L-Synergy` (2026-06-16) has no
wall-clock span, so it can never match a guide log.

`backfill-times` derives the span from the FITS frames, so this usually means
the frames aren't where the catalog thinks they are. Worth a look at the
folder before re-running the backfill.

---

## 🟡 Decisions with no deadline — but cheaper now than later

### 9. Name the Canon EF 100-400mm zoom (backlog **F9**)

`parse_ota` infers the optic from `FOCALLEN` alone, and the zoom **collides
with your actual telescopes**:

| Zoom shot at | Catalogued as | Actually |
|---|---|---|
| 180mm | `FMA180` | Canon 100-400 |
| 400mm | `FRA400` | Canon 100-400 |
| anything else | `Unknown` | Canon 100-400 |

The 180/400 cases are **silently wrong rather than unknown**, which is the
dangerous half: an `Unknown` OTA gets a badge in the web UI, a ⚠ in `ingest
review`, and the cursor defaulting to "Change OTA / camera". A confident
`FRA400` gets none of that — it sails through review, bakes a false optic into
the session_id and folder name, and then **matches flats belonging to a
different telescope**.

No header disambiguates them (`TELESCOP` is the mount, `ZWO AM5N`), so this
must be a human correction at review time — which needs a name in
`parse.KNOWN_OTAS` first, since today the zoom cannot be picked at all.

The naming convention is already settled by the 50mm decision: **lenses keep
the brand** (`Canon50mm`), telescopes drop it, and focal ratio is never
encoded because the mechanical EF adapter can't stop down. What's open is how
to express a *variable* focal length. Sketch: `Canon100-400mm` plus the
existing `focal_length` column, vs. a per-focal-length name.

**Cheap now, expensive later** — changing this later is a rename of rows plus
folders.

**The data you need, read live 2026-08-31.** 20 sessions sit at
`ota='Unknown'`; here are their focal lengths:

| FOCALLEN | rows | probably |
|---|---|---|
| 53 | 1 | the 50mm — **now auto-resolves to `Canon50mm`**, see #2b |
| 56 | 1 | the same 50mm reporting high? If so widen the window, don't add a name |
| 100, 104 | 1 + 7 | the 100-400 zoom, wide end |
| 136 | 3 | zoom |
| 200, 202 | 3 + 1 | zoom |
| 301 | 1 | zoom |
| 386 | 2 | zoom — and uncomfortably close to `FRA400`'s 390–410 window |

So **18 of the 20 are the zoom across its range**, and they all get names the
moment this is decided. Two things that decision should settle: whether `56` is
the fifty (a window widening) or something else, and how a zoom is named at all
given the focal length varies per session — `Canon100-400mm` plus the existing
`focal_length` column, or a per-focal-length name like `Canon200mm`.

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

Related: **20 sessions sit at `ota='Unknown'`** (19 Canon6D + 1
`ASCOMCameraDriver`). Those are the Canon-lens sessions the `Canon<focal>mm`
convention was designed for — once #9 settles the zoom naming, they can be
corrected in the same style.

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
