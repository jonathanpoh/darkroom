"""Shared config resolution: CLI flag → env var → darkroom.toml."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def find_toml() -> dict:
    """Return contents of darkroom.toml from cwd or ~/.config/darkroom/, or {}."""
    for candidate in [
        Path("darkroom.toml"),
        Path.home() / ".config" / "darkroom" / "darkroom.toml",
    ]:
        cfg = _load_toml(candidate)
        if cfg:
            # Accept either flat top-level keys or `[darkroom]` table form.
            return cfg.get("darkroom", cfg)
    return {}


def resolve_path(
    flag_val: str | None, env_var: str, toml_key: str
) -> Path | None:
    """Resolve a path: CLI flag → env var → darkroom.toml key."""
    if flag_val:
        return Path(flag_val).expanduser()
    env = os.environ.get(env_var)
    if env:
        return Path(env).expanduser()
    cfg = find_toml()
    if toml_key in cfg:
        return Path(cfg[toml_key]).expanduser()
    return None


_DEFAULT_CATALOG = Path.home() / ".config" / "darkroom" / "astro_catalog.db"


def resolve_catalog(flag_val: str | None) -> Path:
    """Resolve catalog path, defaulting to ~/.config/darkroom/astro_catalog.db."""
    path = resolve_path(flag_val, "DARKROOM_CATALOG", "catalog_path") or _DEFAULT_CATALOG
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_str(
    flag_val: str | None, env_var: str, toml_key: str
) -> str | None:
    """Resolve a raw string value: CLI flag → env var → darkroom.toml key."""
    if flag_val:
        return flag_val
    env = os.environ.get(env_var)
    if env:
        return env
    cfg = find_toml()
    if toml_key in cfg:
        return cfg[toml_key]
    return None


def resolve_catalog_url(flag_val: str | None = None) -> str | None:
    """Resolve the catalog API base URL (W9): flag → DARKROOM_CATALOG_URL → toml.

    Unset (None) at every level means "no server configured" — callers should
    fall back to the local SQLite file backend in that case.
    """
    return resolve_str(flag_val, "DARKROOM_CATALOG_URL", "catalog_url")


def resolve_api_token(flag_val: str | None = None) -> str | None:
    """Resolve the catalog API bearer token (W9): flag → DARKROOM_API_TOKEN → toml."""
    return resolve_str(flag_val, "DARKROOM_API_TOKEN", "api_token")
