"""Shared test fixtures.

Autouse hermeticity guard: prevents the test suite from ever touching the
developer's real ~/.config/darkroom/darkroom.toml or real DARKROOM_* env vars.
Without this, in-process CLI tests that go through darkroom.config resolution
(e.g. resolve_backend) can pick up a real catalog_url from the machine's toml
and make live network calls against the production catalog server.

Implementation notes:
- We redirect HOME to a fresh per-test tmp_path rather than monkeypatching
  darkroom.config.find_toml directly. Several tests (tests/test_ingest.py's
  find_toml()/resolve_path() tests) intentionally exercise real config
  resolution and already do their own chdir()/HOME isolation inside the test
  body. Since those run after this fixture's setup and use the same
  monkeypatch instance, their own monkeypatch.setenv("HOME", ...) /
  monkeypatch.chdir(...) calls simply take precedence — patching find_toml
  itself would instead short-circuit those tests' real logic and break them.
- darkroom.config._DEFAULT_CATALOG is computed once at import time from the
  real Path.home(), before this fixture ever runs, so redirecting HOME here
  has no effect on that constant.
"""
from __future__ import annotations

import pytest

_DARKROOM_ENV_VARS = (
    "DARKROOM_CATALOG",
    "DARKROOM_CATALOG_URL",
    "DARKROOM_API_TOKEN",
    "DARKROOM_ARCHIVE",
    "DARKROOM_WBPP",
)


@pytest.fixture(autouse=True)
def _isolate_from_real_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in _DARKROOM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
