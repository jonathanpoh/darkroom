import subprocess
import sys
from pathlib import Path

import pytest

from darkroom.catalog_client import LocalBackend, HttpBackend, resolve_backend


def _session(
    session_id,
    target="M 81",
    obs_date="2026-02-19",
    ota="FRA400",
    camera="ZWOASI585MCPro",
    filter="L-Pro",
    gain=200,
    frame_count=100,
    **extra,
):
    base = {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": ota,
        "camera": camera,
        "filter": filter,
        "gain": gain,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": frame_count,
        "total_integration_sec": frame_count * 180,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": f"01_Deep Sky Objects/{target}/{obs_date}_{ota}_{camera}/Lights/{filter}",
        "notes": "",
    }
    base.update(extra)
    return base


def _cal_set(set_id, frame_type="Dark", camera="ZWOASI585MCPro", ota="FRA400", **extra):
    base = {
        "set_id": set_id,
        "frame_type": frame_type,
        "camera": camera,
        "ota": ota,
        "filter": None,
        "gain": 200,
        "exposure_sec": 180.0,
        "temperature_c": -20.0,
        "frame_count": 30,
        "capture_date": "2026-02-19",
        "folder_path": "00_Calibration/Darks/ZWOASI585MCPro",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# LocalBackend round-trip
# ---------------------------------------------------------------------------

def test_local_backend_upsert_and_query_sessions(tmp_path):
    backend = LocalBackend(tmp_path / "cat.db")
    backend.upsert_session(_session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))

    rows = backend.query_sessions(target="M 81")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"

    assert backend.count_sessions(target="M 81") == 1
    assert backend.count_sessions(target="M 999") == 0


def test_local_backend_upsert_and_query_calibration_sets(tmp_path):
    backend = LocalBackend(tmp_path / "cat.db")
    backend.upsert_calibration_set(_cal_set("Dark_ZWOASI585MCPro_180s_200g_-20C_20260219"))
    backend.upsert_calibration_set(_cal_set(
        "Flat_FRA400_ZWOASI585MCPro_L-Pro_20260220",
        frame_type="Flat", filter="L-Pro",
    ))

    darks = backend.query_calibration_sets(frame_type="Dark")
    assert len(darks) == 1
    assert darks[0]["set_id"] == "Dark_ZWOASI585MCPro_180s_200g_-20C_20260219"

    flats = backend.query_calibration_sets(frame_type="Flat", filter="L-Pro")
    assert len(flats) == 1

    none_match = backend.query_calibration_sets(frame_type="Flat", filter="L-Extreme")
    assert none_match == []


def test_local_backend_set_processed_state(tmp_path):
    backend = LocalBackend(tmp_path / "cat.db")
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    backend.upsert_session(_session(sid))

    ok = backend.set_processed_state(
        sid, state="processed", processed_date="2026-03-01",
        processed_path="01_Deep Sky Objects/M 81/_Processed/2026-03-01",
        notes="looks great",
    )
    assert ok is True

    rows = backend.query_sessions(session_id=sid)
    assert rows[0]["processed_state"] == "processed"
    assert rows[0]["processed_date"] == "2026-03-01"
    assert rows[0]["notes"] == "looks great"

    assert backend.set_processed_state("does_not_exist", state="processed") is False

    with pytest.raises(ValueError):
        backend.set_processed_state(sid, state="not_a_real_state")


def test_local_backend_update_session_fields(tmp_path):
    backend = LocalBackend(tmp_path / "cat.db")
    sid = "M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"
    backend.upsert_session(_session(sid))

    ok = backend.update_session_fields(sid, notes="updated note")
    assert ok is True
    rows = backend.query_sessions(session_id=sid)
    assert rows[0]["notes"] == "updated note"

    with pytest.raises(ValueError):
        backend.update_session_fields(sid, bogus_field=1)

    assert backend.update_session_fields("does_not_exist", notes="x") is False


def test_local_backend_creates_schema_on_nonexistent_db(tmp_path):
    db_path = tmp_path / "fresh" / "new_cat.db"
    assert not db_path.exists()
    backend = LocalBackend(db_path)

    # First op (a read) should create the schema, not crash.
    rows = backend.query_sessions()
    assert rows == []
    assert db_path.exists()


def test_local_backend_creates_schema_on_first_write(tmp_path):
    db_path = tmp_path / "fresh2" / "new_cat.db"
    assert not db_path.exists()
    backend = LocalBackend(db_path)

    backend.upsert_session(_session("M81_20260219_FRA400_ZWOASI585MCPro_L-Pro"))
    assert db_path.exists()
    assert backend.count_sessions() == 1


# ---------------------------------------------------------------------------
# import isolation
# ---------------------------------------------------------------------------

def test_importing_catalog_client_does_not_pull_in_astropy_or_httpx():
    result = subprocess.run(
        [sys.executable, "-c",
         "import darkroom.catalog_client, sys; "
         "sys.exit(0 if ('astropy' not in sys.modules and 'httpx' not in sys.modules) else 1)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# ---------------------------------------------------------------------------
# resolve_backend selection
# ---------------------------------------------------------------------------

def test_resolve_backend_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKROOM_CATALOG_URL", raising=False)
    monkeypatch.delenv("DARKROOM_API_TOKEN", raising=False)
    monkeypatch.delenv("DARKROOM_CATALOG", raising=False)
    monkeypatch.setattr("darkroom.config.find_toml", lambda: {})

    backend = resolve_backend(str(tmp_path / "cat.db"))
    assert isinstance(backend, LocalBackend)
    assert backend.db_path == tmp_path / "cat.db"


def test_resolve_backend_uses_http_when_url_env_set(monkeypatch):
    monkeypatch.setenv("DARKROOM_CATALOG_URL", "http://homelab:8000")
    monkeypatch.delenv("DARKROOM_API_TOKEN", raising=False)
    monkeypatch.setattr("darkroom.config.find_toml", lambda: {})

    backend = resolve_backend()
    assert isinstance(backend, HttpBackend)
    assert backend.base_url == "http://homelab:8000"
    backend.close()


def test_resolve_backend_url_flag_beats_env(monkeypatch):
    monkeypatch.setenv("DARKROOM_CATALOG_URL", "http://env-host:8000")
    monkeypatch.setattr("darkroom.config.find_toml", lambda: {})

    backend = resolve_backend(url_flag="http://flag-host:8000")
    assert isinstance(backend, HttpBackend)
    assert backend.base_url == "http://flag-host:8000"
    backend.close()


# ---------------------------------------------------------------------------
# config.resolve_catalog_url / resolve_api_token precedence
# ---------------------------------------------------------------------------

def test_resolve_catalog_url_precedence(monkeypatch):
    from darkroom import config

    monkeypatch.delenv("DARKROOM_CATALOG_URL", raising=False)
    monkeypatch.setattr(config, "find_toml", lambda: {})
    assert config.resolve_catalog_url() is None

    monkeypatch.setattr(config, "find_toml", lambda: {"catalog_url": "http://toml-host:8000"})
    assert config.resolve_catalog_url() == "http://toml-host:8000"

    monkeypatch.setenv("DARKROOM_CATALOG_URL", "http://env-host:8000")
    assert config.resolve_catalog_url() == "http://env-host:8000"

    assert config.resolve_catalog_url("http://flag-host:8000") == "http://flag-host:8000"


def test_resolve_api_token_precedence(monkeypatch):
    from darkroom import config

    monkeypatch.delenv("DARKROOM_API_TOKEN", raising=False)
    monkeypatch.setattr(config, "find_toml", lambda: {})
    assert config.resolve_api_token() is None

    monkeypatch.setattr(config, "find_toml", lambda: {"api_token": "toml-token"})
    assert config.resolve_api_token() == "toml-token"

    monkeypatch.setenv("DARKROOM_API_TOKEN", "env-token")
    assert config.resolve_api_token() == "env-token"

    assert config.resolve_api_token("flag-token") == "flag-token"


# ---------------------------------------------------------------------------
# HttpBackend request/response mapping (MockTransport, no real server)
# ---------------------------------------------------------------------------

def test_http_backend_query_sessions_maps_response():
    import httpx

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json=[{"session_id": "abc"}])

    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://test", client=client)

    rows = backend.query_sessions(target="M 81", limit=10, offset=5)
    assert rows == [{"session_id": "abc"}]
    assert captured["method"] == "GET"
    assert "target=M+81" in captured["url"] or "target=M%2081" in captured["url"]
    assert "limit=10" in captured["url"]
    assert "offset=5" in captured["url"]
    backend.close()


def test_http_backend_set_processed_state_404_returns_false():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://test", client=client)

    assert backend.set_processed_state("missing", state="processed") is False
    backend.close()


def test_http_backend_set_processed_state_400_raises_value_error():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad state"})

    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://test", client=client)

    with pytest.raises(ValueError):
        backend.set_processed_state("sid", state="not_a_real_state")
    backend.close()


def test_http_backend_401_raises_runtime_error():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://test", client=client)

    with pytest.raises(RuntimeError):
        backend.query_sessions()
    backend.close()
