# wbpp_finish.py Implementation Plan

> **Historical (2026-05-15).** Implemented. Module now lives at `darkroom/finish.py`; CLI is `darkroom finish`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `wbpp_finish.py` — a script that copies WBPP `master/` and `processed/` output back to the NAS `_Processed/` folder, then interactively cleans up the local WBPP working directory.

**Architecture:** Single standalone script `wbpp_finish.py` at the project root, following the same CLI and config-resolution patterns as `wbpp_prep.py`. Pure filesystem operations — no catalog reads or writes. Pure helper functions are unit-tested; the top-level `cmd_finish` and `main` are wired manually and verified against real data.

**Tech Stack:** Python stdlib only — `argparse`, `shutil`, `pathlib`, `tomllib`, `re`, `datetime`, `os`, `sys`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `wbpp_finish.py` | Create | All logic: helpers, `cmd_finish`, `main`, config resolution |
| `tests/test_wbpp_finish.py` | Create | Unit tests for all pure helper functions |

---

### Task 1: Skeleton + date extraction + destination path

**Files:**
- Create: `wbpp_finish.py`
- Create: `tests/test_wbpp_finish.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_wbpp_finish.py
import pytest
from datetime import date
from pathlib import Path

from wbpp_finish import _find_master_date, _build_dest


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_find_master_date_returns_today(tmp_path):
    master = tmp_path / "master"
    master.mkdir()
    touch(master / "masterLight_BIN-1_3840x2160_FILTER-L-Extreme_RGB.xisf")
    result = _find_master_date(master)
    assert result == date.today().isoformat()


def test_find_master_date_no_file_exits(tmp_path):
    master = tmp_path / "master"
    master.mkdir()
    with pytest.raises(SystemExit):
        _find_master_date(master)


def test_build_dest(tmp_path):
    dest = _build_dest(tmp_path, "M 81", "2026-05-15")
    assert dest == tmp_path / "04_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-15"


def test_build_dest_target_with_spaces(tmp_path):
    dest = _build_dest(tmp_path, "NGC 1499", "2026-03-01")
    assert dest == tmp_path / "04_Deep Sky Objects" / "NGC 1499" / "_Processed" / "2026-03-01"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jpoh/Projects/darkroom-ingest
uv run pytest tests/test_wbpp_finish.py -v
```

Expected: `ModuleNotFoundError: No module named 'wbpp_finish'`

- [ ] **Step 3: Create `wbpp_finish.py` with helpers**

```python
#!/usr/bin/env python3
"""wbpp_finish.py — Copy WBPP stacks back to the NAS archive and clean up working dirs."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path


# ── config resolution (mirrors wbpp_prep.py) ──────────────────────────────────

def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _find_toml() -> dict:
    for candidate in [Path("darkroom.toml"), Path.home() / ".config" / "darkroom" / "darkroom.toml"]:
        cfg = _load_toml(candidate)
        if cfg:
            return cfg
    return {}


def resolve_path(flag_val: str | None, env_var: str, toml_key: str) -> Path | None:
    """Resolve a path: CLI flag → env var → toml."""
    if flag_val:
        return Path(flag_val)
    env = os.environ.get(env_var)
    if env:
        return Path(env)
    cfg = _find_toml()
    if toml_key in cfg:
        return Path(cfg[toml_key])
    return None


def _target_slug(target: str) -> str:
    return target.replace(" ", "")


# ── core helpers ──────────────────────────────────────────────────────────────

def _find_master_date(master_dir: Path) -> str:
    """Return YYYY-MM-DD creation date of the first masterLight_*.xisf in master_dir."""
    candidates = sorted(master_dir.glob("masterLight_*.xisf"))
    if not candidates:
        sys.exit(f"No masterLight_*.xisf found in {master_dir}")
    stat = candidates[0].stat()
    ts = getattr(stat, "st_birthtime", stat.st_mtime)
    return datetime.fromtimestamp(ts).date().isoformat()


def _build_dest(output: Path, target: str, date_str: str) -> Path:
    """Return <output>/04_Deep Sky Objects/<target>/_Processed/<date_str>."""
    return output / "04_Deep Sky Objects" / target / "_Processed" / date_str
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_wbpp_finish.py::test_find_master_date_returns_today \
              tests/test_wbpp_finish.py::test_find_master_date_no_file_exits \
              tests/test_wbpp_finish.py::test_build_dest \
              tests/test_wbpp_finish.py::test_build_dest_target_with_spaces -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add wbpp_finish.py tests/test_wbpp_finish.py
git commit -m "feat: add wbpp_finish skeleton with date extraction and dest path helpers"
```

---

### Task 2: Flat file copy

**Files:**
- Modify: `wbpp_finish.py` — add `_copy_flat`
- Modify: `tests/test_wbpp_finish.py` — add copy tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_wbpp_finish.py`. First, replace the existing import line at the top with:

```python
from wbpp_finish import _find_master_date, _build_dest, _copy_flat
```

Then add these test functions:

```python
def test_copy_flat_copies_files(tmp_path):
    src = tmp_path / "master"
    src.mkdir()
    touch(src / "masterLight.xisf")
    touch(src / "masterDark.xisf")
    dest = tmp_path / "dest" / "master"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 2
    assert (dest / "masterLight.xisf").exists()
    assert (dest / "masterDark.xisf").exists()


def test_copy_flat_skips_existing(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir(); dest.mkdir()
    touch(src / "file.xisf")
    touch(dest / "file.xisf")
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 0


def test_copy_flat_empty_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=False)
    assert count == 0
    assert not dest.exists()


def test_copy_flat_dry_run_does_not_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    touch(src / "file.xisf")
    dest = tmp_path / "dest"
    count = _copy_flat(src, dest, dry_run=True)
    assert count == 1
    assert not dest.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_wbpp_finish.py -k "copy_flat" -v
```

Expected: `ImportError` — `_copy_flat` not defined yet

- [ ] **Step 3: Add `_copy_flat` to `wbpp_finish.py`**

Add after `_build_dest`:

```python
def _copy_flat(src_dir: Path, dest_dir: Path, *, dry_run: bool) -> int:
    """Copy all files from src_dir into dest_dir (flat, no subdirs). Returns count copied."""
    files = sorted(f for f in src_dir.iterdir() if f.is_file())
    if not files:
        return 0
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in files:
        dest = dest_dir / f.name
        if dest.exists():
            print(f"  skip (exists): {f.name}")
            continue
        if dry_run:
            print(f"  [dry-run] {f} → {dest}")
        else:
            shutil.copy2(f, dest)
            print(f"  {f.name} → {dest}")
        count += 1
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_wbpp_finish.py -k "copy_flat" -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add wbpp_finish.py tests/test_wbpp_finish.py
git commit -m "feat: add _copy_flat helper with dry-run and skip-existing support"
```

---

### Task 3: Cleanup helpers

**Files:**
- Modify: `wbpp_finish.py` — add `_list_intermediates`, `_list_outputs`, `_confirm_and_delete`
- Modify: `tests/test_wbpp_finish.py` — add cleanup tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_wbpp_finish.py`. First, replace the existing import lines at the top with:

```python
from unittest.mock import patch
from wbpp_finish import (
    _find_master_date, _build_dest, _copy_flat,
    _list_intermediates, _list_outputs, _confirm_and_delete,
)
```

Then add these test functions:

```python
def test_list_intermediates_returns_existing(tmp_path):
    (tmp_path / "calibrated").mkdir()
    (tmp_path / "debayered").mkdir()
    (tmp_path / "SESSION_1").mkdir()
    (tmp_path / "SESSION_2").mkdir()
    (tmp_path / "master").mkdir()        # should NOT appear
    (tmp_path / "processed").mkdir()     # should NOT appear
    result = _list_intermediates(tmp_path)
    names = {p.name for p in result}
    assert "calibrated" in names
    assert "debayered" in names
    assert "SESSION_1" in names
    assert "SESSION_2" in names
    assert "master" not in names
    assert "processed" not in names


def test_list_intermediates_skips_missing(tmp_path):
    (tmp_path / "SESSION_1").mkdir()
    result = _list_intermediates(tmp_path)
    assert len(result) == 1
    assert result[0].name == "SESSION_1"


def test_list_outputs_returns_master_and_processed(tmp_path):
    (tmp_path / "master").mkdir()
    (tmp_path / "processed").mkdir()
    result = _list_outputs(tmp_path)
    names = {p.name for p in result}
    assert names == {"master", "processed"}


def test_list_outputs_skips_missing(tmp_path):
    (tmp_path / "master").mkdir()
    result = _list_outputs(tmp_path)
    assert len(result) == 1
    assert result[0].name == "master"


def test_confirm_and_delete_dry_run_does_not_delete(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    _confirm_and_delete([d], "Intermediates", dry_run=True)
    assert d.exists()


def test_confirm_and_delete_yes_deletes(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    with patch("builtins.input", return_value="yes"):
        _confirm_and_delete([d], "Intermediates", dry_run=False)
    assert not d.exists()


def test_confirm_and_delete_no_skips(tmp_path):
    d = tmp_path / "calibrated"
    d.mkdir()
    with patch("builtins.input", return_value=""):
        _confirm_and_delete([d], "Intermediates", dry_run=False)
    assert d.exists()


def test_confirm_and_delete_empty_list(tmp_path):
    _confirm_and_delete([], "Intermediates", dry_run=False)  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_wbpp_finish.py -k "list_intermediates or list_outputs or confirm_and_delete" -v
```

Expected: `ImportError` — helpers not defined yet

- [ ] **Step 3: Add cleanup helpers to `wbpp_finish.py`**

Add after `_copy_flat`:

```python
_INTERMEDIATE_NAMES = {"calibrated", "debayered", "fastIntegration", "logs"}


def _list_intermediates(wbpp_target_dir: Path) -> list[Path]:
    """Return existing intermediate dirs (named dirs + SESSION_N dirs) inside wbpp_target_dir."""
    result = []
    for p in wbpp_target_dir.iterdir():
        if p.is_dir() and (p.name in _INTERMEDIATE_NAMES or re.fullmatch(r"SESSION_\d+", p.name)):
            result.append(p)
    return sorted(result)


def _list_outputs(wbpp_target_dir: Path) -> list[Path]:
    """Return existing master/ and processed/ dirs inside wbpp_target_dir."""
    result = []
    for name in ("master", "processed"):
        p = wbpp_target_dir / name
        if p.exists():
            result.append(p)
    return result


def _confirm_and_delete(dirs: list[Path], label: str, *, dry_run: bool) -> None:
    """List dirs, prompt for confirmation, delete if confirmed. No-op if dirs is empty."""
    if not dirs:
        return
    print(f"\n{label}:")
    for d in dirs:
        print(f"  {d}")
    if dry_run:
        print("  [dry-run] would delete above")
        return
    answer = input("Delete these directories? [yes/N] ").strip()
    if answer != "yes":
        print("  Skipped.")
        return
    for d in dirs:
        shutil.rmtree(d)
        print(f"  Deleted: {d.name}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_wbpp_finish.py -k "list_intermediates or list_outputs or confirm_and_delete" -v
```

Expected: 8 passed

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
uv run pytest -v
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add wbpp_finish.py tests/test_wbpp_finish.py
git commit -m "feat: add cleanup helpers — list_intermediates, list_outputs, confirm_and_delete"
```

---

### Task 4: CLI wiring — `cmd_finish` and `main`

**Files:**
- Modify: `wbpp_finish.py` — add `cmd_finish`, `build_parser`, `main`

No new tests for this task — `cmd_finish` orchestrates the helpers already tested. Verify manually against real data.

- [ ] **Step 1: Add `cmd_finish`, `build_parser`, `main` to `wbpp_finish.py`**

Add at the bottom of `wbpp_finish.py`:

```python
# ── main command ──────────────────────────────────────────────────────────────

def cmd_finish(
    *,
    output: Path,
    wbpp_root: Path,
    target: str,
    dry_run: bool,
) -> None:
    slug = _target_slug(target)
    wbpp_target = wbpp_root / slug
    master_dir = wbpp_target / "master"
    processed_dir = wbpp_target / "processed"

    if not master_dir.exists():
        sys.exit(f"master/ not found in {wbpp_target}")
    if not processed_dir.exists():
        sys.exit(f"processed/ not found in {wbpp_target}")

    date_str = _find_master_date(master_dir)
    dest = _build_dest(output, target, date_str)

    print(f"Destination: {dest}")

    print("\nCopying master/")
    _copy_flat(master_dir, dest / "master", dry_run=dry_run)

    processed_files = [f for f in processed_dir.iterdir() if f.is_file()]
    if not processed_files:
        print("\nWarning: processed/ is empty — skipping")
    else:
        print("\nCopying processed/")
        _copy_flat(processed_dir, dest / "processed", dry_run=dry_run)

    print(f'\nDone. Remember to mark "{target}" as processed in darkroom-catalog once that command is implemented.')

    _confirm_and_delete(
        _list_intermediates(wbpp_target),
        "Intermediate directories to delete",
        dry_run=dry_run,
    )
    _confirm_and_delete(
        _list_outputs(wbpp_target),
        "Working output directories to delete (master/ and processed/)",
        dry_run=dry_run,
    )


# ── argument parsing ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Copy WBPP stacks to the NAS archive and clean up working dirs."
    )
    p.add_argument("--target", metavar="NAME", required=True, help='Target name (e.g. "M 81")')
    p.add_argument("--output", metavar="PATH",
                   help="Archive root — same value as wbpp_prep.py --output")
    p.add_argument("--wbpp", metavar="PATH", default="./WBPP",
                   help="Root for WBPP target dirs (default: ./WBPP)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be copied/deleted without making changes")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    output = resolve_path(args.output, "DARKROOM_OUTPUT", "output_path")
    if output is None:
        sys.exit("Error: --output / DARKROOM_OUTPUT / darkroom.toml output_path required")

    cmd_finish(
        output=output,
        wbpp_root=Path(args.wbpp),
        target=args.target,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass

- [ ] **Step 3: Dry-run against real data**

```bash
uv run python wbpp_finish.py \
  --target "M 81" \
  --output ./staging \
  --wbpp ./WBPP \
  --dry-run
```

Expected output (no files created or deleted):
```
Destination: staging/04_Deep Sky Objects/M 81/_Processed/2026-05-15

Copying master/
  [dry-run] WBPP/M81/master/masterLight_*.xisf → staging/04_Deep Sky Objects/M 81/_Processed/2026-05-15/master/masterLight_*.xisf
  ...

Copying processed/
  [dry-run] ...

Done. Remember to mark "M 81" as processed in darkroom-catalog once that command is implemented.

Intermediate directories to delete:
  WBPP/M81/calibrated
  WBPP/M81/debayered
  WBPP/M81/fastIntegration
  WBPP/M81/logs
  WBPP/M81/SESSION_1
  [dry-run] would delete above

Working output directories to delete (master/ and processed/):
  WBPP/M81/master
  WBPP/M81/processed
  [dry-run] would delete above
```

- [ ] **Step 4: Run for real**

```bash
uv run python wbpp_finish.py \
  --target "M 81" \
  --output ./staging \
  --wbpp ./WBPP
```

Verify:
- `staging/04_Deep Sky Objects/M 81/_Processed/<date>/master/` contains `masterLight_*.xisf`, `masterDark_*.xisf`, `masterFlat_*.xisf`
- `staging/04_Deep Sky Objects/M 81/_Processed/<date>/processed/` contains the processed image
- After confirming `yes` to each prompt, intermediate dirs are gone from `WBPP/M81/`

- [ ] **Step 5: Commit**

```bash
git add wbpp_finish.py
git commit -m "feat: add wbpp_finish.py — copy stacks to NAS and clean up WBPP working dirs"
```
