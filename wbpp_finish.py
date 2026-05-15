#!/usr/bin/env python3
"""wbpp_finish.py — Copy WBPP stacks back to the NAS archive and clean up working dirs."""
from __future__ import annotations

import os
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
