# B3 + B4 + B5 + DSO-rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three correctness bugs from `BACKLOG.md` — B3 (`triage scan` looks in the wrong DSO root folder), B4 (`check_ra_dec` crashes the whole triage scan on sexagesimal RA/DEC headers), B5 (`wbpp` symlinks both a master calibration file AND raw sub-frames instead of preferring the master) — and rename every remaining `04_Deep Sky Objects` reference in production code and user-facing docs to `01_Deep Sky Objects`, the confirmed-current name on both the user's work SSD and NAS (this also closes out B6).

**Architecture:** Each bug fix is independently testable and touches a different module (`triage/scanner.py`, `triage/checks.py`, `prep.py`), so the three are separate tasks. The doc/help-text rename is a fourth, low-risk task bundling every remaining literal `04_Deep Sky Objects` string in code comments, CLI help text, and `README.md`/`CHEATSHEET.md`/`CLAUDE.md`.

**Tech Stack:** Python 3.13, pytest, `uv`.

## Global Constraints

- The canonical DSO root folder name is `01_Deep Sky Objects` — confirmed by the user as the actual current name on both their work SSD and the NAS. There is no dual-support requirement; `04_` is simply wrong everywhere it still appears.
- Do NOT touch `docs/superpowers/plans/*.md` or `docs/superpowers/specs/*.md` — these are historical implementation-plan/spec records from when `04_` actually was current; rewriting them would falsify the historical record, the same reason you wouldn't rewrite old commit messages.
- Do NOT touch the literal `"04_Deep Sky Objects"` strings used as test fixture path components in `tests/triage/test_scanner.py`, `tests/triage/test_suggest.py`, `tests/triage/test_server.py`, `tests/test_cataloger.py`, `tests/test_catalog.py` — in every one of those tests the literal is just an arbitrary placeholder folder name passed consistently to both the fixture setup and the function under test (the functions take the DSO path as an explicit parameter, they don't hardcode it), so the literal value doesn't affect correctness. Changing dozens of these is pure unrequested churn. The one exception is **Task 1**, which adds new tests in that same file using `01_Deep Sky Objects` (because that test specifically exercises the hardcoded-constant bug) — don't edit the pre-existing tests around it.
- Do NOT touch `darkroom/cataloger.py:120` (`_target_from_path`'s docstring: `"(e.g. '01_Deep Sky Objects', '04_Deep Sky Objects')"`) — this deliberately documents that the function's prefix-matching logic supports either name; it's correct as-is, not a bug.
- Keep all currently-passing tests passing; run `uv run pytest` after every task.

---

### Task 1: B3 — fix `triage scan`'s hardcoded DSO root, add regression coverage

**Files:**
- Modify: `darkroom/triage/scanner.py:274`
- Modify: `tests/triage/test_scanner.py` (add a new test class)

**Interfaces:**
- Consumes: `darkroom.triage.scanner.scan_archive(archive_root: Path) -> list[TriageCandidate]` (existing function, signature unchanged).

**Context:** `scan_archive` is the single entry point `darkroom triage scan` calls (`darkroom/triage/cli.py:51,57`). It hardcodes `dso = archive_root / "04_Deep Sky Objects"` and only runs the four DSO-side sub-scanners (`scan_calibration_in_target`, `scan_processed_dirs`, `scan_legacy_sessions`, `scan_fits_headers`) `if dso.exists()`. Against the real archive (root is `01_Deep Sky Objects`), `dso.exists()` is always `False`, so those four scanners silently never run — `triage scan` only ever finds calibration-restructure and thumbnail-cleanup issues, never anything DSO-side. `scan_archive` currently has **zero test coverage** (it's imported in `tests/triage/test_scanner.py:17` but never called), which is exactly why this bug went unnoticed.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage/test_scanner.py`, after the existing `archive` fixture (around line 36) — place it anywhere after the fixture, e.g. right before `class TestScanFlatRestructure:`:

```python
class TestScanArchive:
    def test_finds_legacy_session_under_01_dso_root(self, archive):
        """Regression for B3: scan_archive hardcoded '04_Deep Sky Objects' but
        the real archive root is '01_Deep Sky Objects' — the DSO-side scanners
        (legacy_session, calibration_in_target, processed_dir, fits_headers)
        silently never ran."""
        session = archive / "01_Deep Sky Objects" / "M 42" / "2023-11-23"
        make_fits(session / "Lights" / "frame.fit")
        candidates = scan_archive(archive)
        assert any(c.category == "legacy_session" for c in candidates)

    def test_ignores_dso_dir_under_stale_04_prefix(self, archive):
        """The old '04_' prefix is no longer the canonical name anywhere —
        scan_archive should not find anything under it."""
        session = archive / "04_Deep Sky Objects" / "M 42" / "2023-11-23"
        make_fits(session / "Lights" / "frame.fit")
        candidates = scan_archive(archive)
        assert candidates == []
```

`scan_archive` is already imported at `tests/triage/test_scanner.py:17`; `make_fits` and the `archive` fixture are already defined in the same file (lines 25-36) — no new imports needed.

- [ ] **Step 2: Run tests to verify the first one fails**

Run: `uv run pytest tests/triage/test_scanner.py::TestScanArchive -v`
Expected: `test_finds_legacy_session_under_01_dso_root` FAILS (`scan_archive` returns `[]` because `dso.exists()` is `False` for the `04_`-hardcoded path against a `01_`-named directory). `test_ignores_dso_dir_under_stale_04_prefix` PASSES already (it's not exercising the bug, just documenting the boundary) — that's expected, not a problem.

- [ ] **Step 3: Fix the hardcoded root**

In `darkroom/triage/scanner.py`, change line 274:

```python
    dso = archive_root / "04_Deep Sky Objects"
```

to:

```python
    dso = archive_root / "01_Deep Sky Objects"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage/test_scanner.py::TestScanArchive -v`
Expected: both tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add darkroom/triage/scanner.py tests/triage/test_scanner.py
git commit -m "fix(triage): scan the correct 01_Deep Sky Objects root (B3)"
```

---

### Task 2: B4 — `check_ra_dec` must not crash on sexagesimal RA/DEC

**Files:**
- Modify: `darkroom/triage/checks.py`
- Modify: `tests/triage/test_checks.py` (add two tests to `class TestCheckRaDec`)

**Interfaces:**
- Consumes: `darkroom.names._parse_coords(ra, dec) -> tuple[float | None, float | None]` — already exists (added by the earlier R6 refactor), handles both float-degree and sexagesimal-string FITS header values, returns `(None, None)` when parsing fails. Source: `darkroom/names.py`.

**Context:** `check_ra_dec` (`darkroom/triage/checks.py:29-77`) does `SkyCoord(ra=float(ra), dec=float(dec), unit="deg")` at line 63 — `float()` raises `ValueError` on a sexagesimal string like `"09 55 33"` (which older rigs write into the `RA`/`DEC` FITS headers instead of float degrees), and that exception is *not* caught anywhere in this function, so it propagates up through `scan_fits_headers` → `scan_archive`, aborting the **entire** triage scan on the first frame with sexagesimal coordinates. The same `float(ra)`/`float(dec)` bug is duplicated at lines 71-72, in the dict this function returns on a mismatch.

- [ ] **Step 1: Write the failing tests**

Add to `tests/triage/test_checks.py`, inside `class TestCheckRaDec` (after `test_simbad_unknown_target_returns_none`, currently ending at line 108):

```python
    def test_sexagesimal_coords_do_not_crash(self, tmp_path):
        """Regression for B4: float(ra)/float(dec) crashed with ValueError on
        sexagesimal strings ("09 55 33" / "+69 03 55") — older rigs write RA/DEC
        that way instead of float degrees. That ValueError used to propagate all
        the way out of scan_archive and abort the whole triage scan.

        "09 55 33" hourangle == 148.8875 deg, "+69 03 55" == 69.065278 deg —
        same M 81 coordinates already used in float-degree form elsewhere in
        this file (148.888 / 69.065), so the mock SIMBAD position below matches.
        """
        f = make_fits(tmp_path / "sexagesimal.fit", RA="09 55 33", DEC="+69 03 55")
        mock_table = self._make_mock_table(148.888, 69.065)

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 81", threshold_deg=5.0)

        assert result is None

    def test_sexagesimal_mismatch_returns_degree_values(self, tmp_path):
        """Same sexagesimal input, but mismatched against a distant target — the
        returned dict's frame_ra/frame_dec (line 71-72 of checks.py) must also
        not crash, and must report parsed degree values, not the raw strings."""
        f = make_fits(tmp_path / "wrong.fit", RA="09 55 33", DEC="+69 03 55")  # M 81
        mock_table = self._make_mock_table(10.685, 41.269)  # M 31, far away

        with patch("darkroom.triage.checks.Simbad") as mock_simbad:
            mock_simbad.query_object.return_value = mock_table
            result = check_ra_dec(f, "M 31", threshold_deg=5.0)

        assert result is not None
        assert result["frame_ra"] == pytest.approx(148.8875, abs=1e-3)
        assert result["frame_dec"] == pytest.approx(69.065278, abs=1e-3)
        assert result["separation_deg"] > 5.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage/test_checks.py::TestCheckRaDec -v -k sexagesimal`
Expected: both FAIL with `ValueError: could not convert string to float: '09 55 33'` (raised from `darkroom/triage/checks.py:63`, uncaught).

- [ ] **Step 3: Fix `check_ra_dec` to use `_parse_coords`**

In `darkroom/triage/checks.py`, add the import after the existing `astroquery` import (line 7):

```python
from darkroom.names import _parse_coords
```

Then replace the body of `check_ra_dec` (currently lines 29-77) with:

```python
def check_ra_dec(
    fits_path: Path,
    target_name: str,
    threshold_deg: float = 5.0,
    simbad_cache: dict | None = None,
) -> dict | None:
    """
    Return a dict with mismatch details if RA/DEC is > threshold_deg from the
    SIMBAD position for target_name. Returns None if coords agree or can't be checked.
    """
    try:
        with fits.open(fits_path) as hdul:
            hdr = hdul[0].header
            ra = hdr.get("RA") or hdr.get("OBJCTRA")
            dec = hdr.get("DEC") or hdr.get("OBJCTDEC")
    except Exception:
        return None

    if ra is None or dec is None:
        return None

    ra_deg, dec_deg = _parse_coords(ra, dec)
    if ra_deg is None or dec_deg is None:
        return None

    if simbad_cache and "ra" in simbad_cache:
        simbad_ra = simbad_cache["ra"]
        simbad_dec = simbad_cache["dec"]
    else:
        table = Simbad.query_object(target_name)
        if table is None or len(table) == 0:
            return None
        # astroquery >= 0.4.7 returns lowercase column names
        ra_col = "ra" if "ra" in table.colnames else "RA"
        dec_col = "dec" if "dec" in table.colnames else "DEC"
        simbad_ra = float(table[ra_col][0])
        simbad_dec = float(table[dec_col][0])

    frame_coord = SkyCoord(ra=ra_deg, dec=dec_deg, unit="deg")
    simbad_coord = SkyCoord(ra=simbad_ra, dec=simbad_dec, unit="deg")
    sep = frame_coord.separation(simbad_coord).deg

    if sep <= threshold_deg:
        return None

    return {
        "frame_ra": ra_deg,
        "frame_dec": dec_deg,
        "simbad_ra": simbad_ra,
        "simbad_dec": simbad_dec,
        "separation_deg": sep,
        "target_name": target_name,
    }
```

This is the same function, with `float(ra)`/`float(dec)` (line 63) and `float(ra)`/`float(dec)` (lines 71-72) both replaced by a single `_parse_coords(ra, dec)` call right after the existing `ra is None or dec is None` guard, and an early `return None` when parsing fails instead of letting `SkyCoord`/`float()` raise.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage/test_checks.py -v`
Expected: all tests in the file PASS, including the two new ones.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add darkroom/triage/checks.py tests/triage/test_checks.py
git commit -m "fix(triage): check_ra_dec no longer crashes on sexagesimal RA/DEC (B4)"
```

---

### Task 3: B5 — `wbpp` must symlink master calibration files instead of mixing them with raw subs

**Files:**
- Modify: `darkroom/prep.py:131-163` (`_build_night`'s Darks and Bias loops)
- Modify: `tests/test_wbpp_finish.py` (add two tests)

**Interfaces:**
- Consumes: `darkroom.catalog.find_darks(db, *, camera, gain, exposure_sec) -> list[dict]`, `darkroom.catalog.find_bias(db, *, camera, gain) -> list[dict]` — both already return masters first (`ORDER BY is_master DESC`), each row has an `is_master` key (existing, unchanged).

**Context:** Per the commit that introduced master-calibration support (`5c8936d`): *"Masters are stored with is_master=1 and returned first by find_darks/find_bias so wbpp prefers them when available, falling back to raw subs."* — i.e. the intent is: if a master exists for this camera/gain(/exposure), symlink **only** the master(s); only fall back to raw sub-frames when no master exists at all. The current code (`darkroom/prep.py:137-143` for darks, `:152-159` for bias) loops over **every** matching row regardless of whether it's a master, so when both a master `.xisf` and raw sub-frames match, both get symlinked into the same `Darks/`/`Bias/` folder — handing WBPP a mixed master+raw set, the opposite of "prefer". Note there can legitimately be more than one master row for the same camera/gain/exposure (e.g. different capture temperatures) — `find_darks`/`find_bias` don't filter on temperature — so the fix must keep *all* master rows when any exist, not just the first one.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_wbpp_finish.py`, after `test_build_night_symlinks_flat_darks_dated_next_morning` (end of file, currently ending at line 258):

```python
# ── B5: wbpp prefers master calibration files over raw subs ───────────────────

def test_build_night_prefers_master_dark_over_raw_subs(tmp_path):
    """Regression for B5: when both a master dark and raw sub-frames match the
    same camera/gain/exposure, only the master should be symlinked into Darks/
    — not a mix of both.
    """
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    master_rel = "00_Calibration/Darks/ZWOASI585MCPro/Masters/masterDark_180s_gain200_-20C.xisf"
    touch(archive / master_rel)
    upsert_calibration_set(catalog, {
        "set_id": "dark_master", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": master_rel,
        "is_master": 1,
    })

    raw_rel = "00_Calibration/Darks/ZWOASI585MCPro/Raw/2026-02-19"
    touch(archive / raw_rel / "Dark_180.0s_Bin1_585MC_gain200_20260219-090000_-20.0C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "dark_raw", "frame_type": "Dark", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": 180.0, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, catalog=catalog,
                 session_dir=session_dir, flat_window=3)

    dark_links = list((session_dir / "Darks").glob("*"))
    assert len(dark_links) == 1
    assert dark_links[0].resolve().name == "masterDark_180s_gain200_-20C.xisf"


def test_build_night_prefers_master_bias_over_raw_subs(tmp_path):
    """Regression for B5 (Bias half — same bug, separate loop in prep.py)."""
    archive = tmp_path / "archive"
    catalog = tmp_path / "cat.db"
    init_db(catalog)

    cam = "ZWOASI585MCPro"

    master_rel = "00_Calibration/Bias/ZWOASI585MCPro/Masters/masterBias_gain200.xisf"
    touch(archive / master_rel)
    upsert_calibration_set(catalog, {
        "set_id": "bias_master", "frame_type": "Bias", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": None, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": master_rel,
        "is_master": 1,
    })

    raw_rel = "00_Calibration/Bias/ZWOASI585MCPro/Raw/2026-02-19"
    touch(archive / raw_rel / "Bias_0.001s_Bin1_585MC_gain200_20260219-090000_-20.0C_0001.fit")
    upsert_calibration_set(catalog, {
        "set_id": "bias_raw", "frame_type": "Bias", "camera": cam, "ota": None,
        "filter": None, "gain": 200, "exposure_sec": None, "temperature_c": -20.0,
        "frame_count": 1, "capture_date": "2026-02-19", "folder_path": raw_rel,
        "is_master": 0,
    })

    lights_rel = "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    touch(archive / lights_rel / "Light_M81_180.0s_L-Pro_20260219-230000_-20C_0001.fit")
    session = {
        "lights_path": lights_rel, "filter": "L-Pro", "camera": cam, "gain": 200,
        "exposure_sec": 180.0, "ota": "FRA400", "obs_date": "2026-02-19", "frame_count": 1,
    }

    session_dir = tmp_path / "WBPP" / "M81" / "SESSION_1"
    _build_night([session], output=archive, catalog=catalog,
                 session_dir=session_dir, flat_window=3)

    bias_links = list((session_dir / "Bias").glob("*"))
    assert len(bias_links) == 1
    assert bias_links[0].resolve().name == "masterBias_gain200.xisf"
```

(`init_db`, `upsert_calibration_set`, `touch`, `_build_night` are already imported/defined at the top of `tests/test_wbpp_finish.py` — no new imports needed.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_wbpp_finish.py -v -k "prefers_master"`
Expected: both FAIL with `assert 2 == 1` (both the master `.xisf` and the raw `.fit` got symlinked).

- [ ] **Step 3: Fix the Darks and Bias loops**

In `darkroom/prep.py`, replace lines 131-163 (`_build_night`'s Darks and Bias sections) with:

```python
    # Darks — camera/gain/exposure from first session (all sessions same night share params)
    s0 = sessions[0]
    dark_rows = find_darks(
        catalog, camera=s0["camera"], gain=s0["gain"], exposure_sec=s0["exposure_sec"]
    )
    master_dark_rows = [r for r in dark_rows if r.get("is_master")]
    dark_count = 0
    for row in master_dark_rows or dark_rows:
        if row.get("is_master"):
            master_path = output / row["folder_path"]
            files = [master_path] if master_path.exists() else []
        else:
            files = discover_darks(output / row["folder_path"], exposure_sec=s0["exposure_sec"])
        dark_count += make_symlinks(files, session_dir / "Darks")
    if dark_count == 0:
        print("  Darks/                    0 symlinks  [no darks found]")
    else:
        print(f"  Darks/                    {dark_count} symlinks")

    # Bias — camera/gain only (exposure irrelevant for bias)
    bias_rows = find_bias(catalog, camera=s0["camera"], gain=s0["gain"])
    master_bias_rows = [r for r in bias_rows if r.get("is_master")]
    bias_count = 0
    for row in master_bias_rows or bias_rows:
        if row.get("is_master"):
            master_path = output / row["folder_path"]
            files = [master_path] if master_path.exists() else []
        else:
            p = output / row["folder_path"]
            files = fits_files(p) if p.exists() else []
        bias_count += make_symlinks(files, session_dir / "Bias")
    if bias_count == 0:
        print("  Bias/                     0 symlinks  [no bias found]")
    else:
        print(f"  Bias/                     {bias_count} symlinks")
```

This is the same two loops, with one new line before each (`master_dark_rows = [...]` / `master_bias_rows = [...]`) and the loop now iterating `master_dark_rows or dark_rows` / `master_bias_rows or bias_rows` instead of the full row list unconditionally — when any master rows exist, only they are iterated (and the `if row.get("is_master")` branch inside the loop is then always true for every row); when none exist, the fallback `or` clause iterates the original full (now guaranteed all-raw) list exactly as before.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_wbpp_finish.py -v -k "prefers_master"`
Expected: both PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions. Pay attention to any other `test_wbpp_finish.py`/`test_wbpp.py` tests that exercise `_build_night`'s Darks/Bias output — confirm none of them relied on the old (buggy) mixed-master-and-raw behavior.

- [ ] **Step 6: Commit**

```bash
git add darkroom/prep.py tests/test_wbpp_finish.py
git commit -m "fix(wbpp): symlink only master calibration files when a master exists (B5)"
```

---

### Task 4: B6 — rename remaining `04_Deep Sky Objects` references to `01_Deep Sky Objects`

**Files:**
- Modify: `darkroom/catalog_cli.py:83`
- Modify: `darkroom/cataloger.py:895, 896, 980, 1012, 1037, 1038`
- Modify: `darkroom/finish.py:255`
- Modify: `CLAUDE.md:55, 73, 96`
- Modify: `CHEATSHEET.md:139, 172`
- Modify: `README.md:55, 128`

**Interfaces:** None — this task only changes string literals in help text, docstrings, and markdown prose. No function signatures change.

**Context:** Per the **Global Constraints** above: this task does NOT touch `docs/superpowers/plans/*.md`, `docs/superpowers/specs/*.md` (historical records), test fixture literals (cosmetic, behaviorally inert), or `darkroom/cataloger.py:120` (deliberately documents both prefixes are supported by the matching logic). Every location below is a literal `04_Deep Sky Objects` string in help text or prose that should simply say `01_Deep Sky Objects`, since that's confirmed the actual current name. There is no test to write for this task — it's pure text replacement with no behavioral surface; verify with the grep command in Step 2.

- [ ] **Step 1: Make the replacements**

In `darkroom/catalog_cli.py`, line 83:
```python
    sl.add_argument("root_path", help="Root folder to scan (e.g. '04_Deep Sky Objects')")
```
→
```python
    sl.add_argument("root_path", help="Root folder to scan (e.g. '01_Deep Sky Objects')")
```

In `darkroom/cataloger.py`, lines 895-896 (inside `migrate_archive_command`'s docstring):
```python
    Old: 04_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>_<Filter>/Lights/*.fit
    New: 04_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>/Lights/<Filter>/*.fit
```
→
```python
    Old: 01_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>_<Filter>/Lights/*.fit
    New: 01_Deep Sky Objects/<Target>/<Date>_<OTA>_<Camera>/Lights/<Filter>/*.fit
```

In `darkroom/cataloger.py`, line 980 (epilog example):
```python
  %(prog)s scan-all "/Volumes/Astrophotography/04_Deep Sky Objects"
```
→
```python
  %(prog)s scan-all "/Volumes/Astrophotography/01_Deep Sky Objects"
```

In `darkroom/cataloger.py`, line 1012:
```python
    p_all.add_argument("root_path", help="Root folder to scan (e.g. '04_Deep Sky Objects')")
```
→
```python
    p_all.add_argument("root_path", help="Root folder to scan (e.g. '01_Deep Sky Objects')")
```

In `darkroom/cataloger.py`, lines 1037-1038:
```python
            "NAS archive root — navigates to "
            "<archive>/04_Deep Sky Objects/<target>/_Processed/ to detect the date "
            "(targets outside 04_Deep Sky Objects/ should use --date instead)"
```
→
```python
            "NAS archive root — navigates to "
            "<archive>/01_Deep Sky Objects/<target>/_Processed/ to detect the date "
            "(targets outside 01_Deep Sky Objects/ should use --date instead)"
```

In `darkroom/finish.py`, line 255:
```python
        description="Copy master/ and processed/ to <archive>/04_Deep Sky Objects/<target>/_Processed/<date>/, then mark each session as processed in the catalog.",
```
→
```python
        description="Copy master/ and processed/ to <archive>/01_Deep Sky Objects/<target>/_Processed/<date>/, then mark each session as processed in the catalog.",
```

In `CLAUDE.md`, line 55:
```
darkroom ingest ──→ NAS: 04_Deep Sky Objects/<Target>/<Session>/Lights/
```
→
```
darkroom ingest ──→ NAS: 01_Deep Sky Objects/<Target>/<Session>/Lights/
```

In `CLAUDE.md`, line 73:
```
darkroom finish ──→ NAS: 04_Deep Sky Objects/<Target>/_Processed/<date>/
```
→
```
darkroom finish ──→ NAS: 01_Deep Sky Objects/<Target>/_Processed/<date>/
```

In `CLAUDE.md`, line 96 (start of the "Light frames" example block):
```
04_Deep Sky Objects/
```
→
```
01_Deep Sky Objects/
```

In `CHEATSHEET.md`, line 139:
```
`<archive>/04_Deep Sky Objects/<target>/_Processed/<date>/` and marks every session under
```
→
```
`<archive>/01_Deep Sky Objects/<target>/_Processed/<date>/` and marks every session under
```

In `CHEATSHEET.md`, line 172:
```
darkroom catalog scan-lights "/Volumes/Astrophotography/04_Deep Sky Objects"
```
→
```
darkroom catalog scan-lights "/Volumes/Astrophotography/01_Deep Sky Objects"
```

In `README.md`, line 55:
```
darkroom catalog scan-lights "/Volumes/Astrophotography/04_Deep Sky Objects"
```
→
```
darkroom catalog scan-lights "/Volumes/Astrophotography/01_Deep Sky Objects"
```

In `README.md`, line 128:
```
Copies stacks to `<output>/04_Deep Sky Objects/<target>/_Processed/<date>/`,
```
→
```
Copies stacks to `<output>/01_Deep Sky Objects/<target>/_Processed/<date>/`,
```

- [ ] **Step 2: Verify no unintended occurrences remain in the in-scope file set**

Run:
```bash
grep -n "04_Deep Sky Objects" darkroom/catalog_cli.py darkroom/cataloger.py darkroom/finish.py darkroom/triage/scanner.py CLAUDE.md CHEATSHEET.md README.md
```
Expected: no output, **except** `darkroom/cataloger.py:120` (the deliberate dual-prefix docstring — must still be present, do not remove it). If grep returns nothing at all (including that line), something else was accidentally changed — check `git diff darkroom/cataloger.py` for that line.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions (this task changes no executable logic, only string literals in help text/docstrings/prose).

- [ ] **Step 4: Commit**

```bash
git add darkroom/catalog_cli.py darkroom/cataloger.py darkroom/finish.py CLAUDE.md CHEATSHEET.md README.md
git commit -m "docs(B6): rename remaining 04_Deep Sky Objects references to 01_"
```

---

## Self-review notes

- **Spec coverage:** B3 (Task 1), B4 (Task 2), B5 (Task 3), B6 (Task 4) — all four covered. B5's fix matches the intent documented in commit `5c8936d`'s message verbatim ("prefers them when available, falling back to raw subs"), not just the backlog's tentative "break after first row" suggestion — the actual fix (partition into master/raw, use masters if any exist) is more correct than that suggestion because it preserves multiple legitimate master rows (e.g. different capture temperatures) instead of arbitrarily keeping only the first.
- **Out-of-scope guardrails are explicit:** the Global Constraints section and Task 4's file list both name exactly what NOT to touch (historical plan/spec docs, test fixture literals, the one deliberate dual-prefix docstring) so an implementer isn't tempted to "complete" the rename into those locations.
- **Type/name consistency:** `_parse_coords` signature in Task 2 matches its actual definition in `darkroom/names.py` (added by the earlier R6 task in this repo's history) — `(ra, dec) -> tuple[float | None, float | None]`. `master_dark_rows`/`master_bias_rows` in Task 3 are local variables, no cross-task interface to keep consistent.
</content>
