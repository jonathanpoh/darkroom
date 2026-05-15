# wbpp_finish.py Design

> **Historical (2026-05-15).** Module now lives at `darkroom/finish.py`; CLI is `darkroom finish`. See top-level `README.md` and `CLAUDE.md`.

## Goal

After PixInsight WBPP completes and the user has processed the stack, copy the stacked masters and processed output back to the NAS archive, then clean up the local WBPP working directory.

## Architecture

New standalone script `wbpp_finish.py`, filesystem-only (no catalog reads or writes). Follows the same CLI and config-resolution patterns as `wbpp_prep.py`. Takes `--target "M 81"` as its primary input, derives everything else from the filesystem and config.

## CLI Interface

```
wbpp_finish.py --target "M 81" [--output PATH] [--wbpp PATH] [--dry-run]
```

| Flag | Required | Default | Resolution order |
|---|---|---|---|
| `--target` | yes | — | CLI only |
| `--output` | yes | — | CLI → `DARKROOM_OUTPUT` env → `darkroom.toml` `output_path` |
| `--wbpp` | no | `./WBPP` | CLI only |
| `--dry-run` | no | false | CLI only |

## File Discovery

1. Derive slug: `"M 81"` → `"M81"` (strip spaces, same as `wbpp_prep.py`)
2. WBPP target dir: `<wbpp>/<slug>/` (e.g. `./WBPP/M81/`)
3. Fail early (with a clear message) if either `master/` or `processed/` is missing from the WBPP target dir
4. Locate date: find first `masterLight_*.xisf` in `master/`, read its `st_birthtime` (macOS creation time), extract date as `YYYY-MM-DD`
5. Fail early if no `masterLight_*.xisf` is found

## Copy Behaviour

Destination root: `<output>/04_Deep Sky Objects/<target>/_Processed/<YYYY-MM-DD>/`

Copy two directories:
- `master/` → `<dest>/master/`
- `processed/` → `<dest>/processed/`

Rules:
- Use `shutil.copy2` (preserves timestamps)
- Copy files only — no subdirectory recursion needed (both `master/` and `processed/` are flat)
- Skip files already present at destination (idempotent; print a note for each skipped file)
- Print each file copied

In `--dry-run` mode: print all source → destination paths without creating any files.

## Cleanup Behaviour

After a successful copy (or in `--dry-run`), offer cleanup in two separate prompts:

**Prompt 1 — Intermediates:**
Lists and offers to delete: `calibrated/`, `debayered/`, `fastIntegration/`, `logs/`, and all `SESSION_N/` dirs.

**Prompt 2 — Working outputs:**
Lists and offers to delete: `master/` and `processed/`.

Both prompts require exact input `yes` to proceed; anything else skips that group without error.

In `--dry-run` mode: list both groups without prompting.

## Catalog Reminder

After the copy step completes (regardless of cleanup choices), print:

```
Done. Remember to mark "<target>" as processed in darkroom-catalog once that command is implemented.
```

## Error Handling

- Missing `master/` or `processed/` directory: exit with a clear message, do nothing
- `processed/` exists but is empty: warn and skip copying processed files, continue with master copy
- Missing `masterLight_*.xisf` for date derivation: exit with a clear message
- Destination already exists (files present): skip per-file, continue — do not abort
- NAS not mounted / output path not reachable: `shutil.copy2` will raise; let it propagate with the OS error message

## File Structure

| File | Change |
|---|---|
| `wbpp_finish.py` | Create — main script |
| `tests/test_wbpp_finish.py` | Create — unit tests |
| `darkroom/wbpp.py` | No change needed (finish uses `shutil` directly; no new library functions required) |
