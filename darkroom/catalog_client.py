"""darkroom.catalog_client — backend abstraction for the catalog (W9).

Selects between a local in-process SQLite backend and a future HTTP backend
talking to the FastAPI catalog server (darkroom/webapi/, built separately),
based on config: `catalog_url` set (flag/env/toml) -> HttpBackend, unset ->
LocalBackend (today's default, in-process SQLite file). Existing offline
workflows and tests never set the URL, so they get LocalBackend unchanged.

Deliberately astropy- and httpx-free at import time: this module only
imports stdlib + darkroom.config at module load, mirroring the constraint
documented in darkroom/catalog_db.py. darkroom.cataloger (which pulls in
astropy) is imported lazily inside LocalBackend's write methods; httpx is
imported lazily inside HttpBackend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from darkroom import config


@runtime_checkable
class CatalogBackend(Protocol):
    def upsert_session(self, session: dict) -> None: ...

    def upsert_calibration_set(self, cal_set: dict) -> None: ...

    def set_processed_state(
        self,
        session_id: str,
        *,
        state: str,
        processed_date: str | None = None,
        processed_path: str | None = None,
        notes: str | None = None,
    ) -> bool: ...

    def update_session_fields(self, session_id: str, **fields) -> bool: ...

    def query_sessions(
        self,
        *,
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]: ...

    def count_sessions(
        self,
        *,
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
    ) -> int: ...

    def query_calibration_sets(
        self,
        *,
        frame_type: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        gain: int | None = None,
        exposure_sec: float | None = None,
    ) -> list[dict]: ...


class LocalBackend:
    """In-process SQLite backend — today's behaviour, unchanged.

    Reads open a fresh connection per call via darkroom.catalog_db.open_db
    (which also guarantees the schema exists) and close it in a finally.
    Writes lazily import darkroom.cataloger (astropy-heavy) and ensure the
    schema via init_db, called once per instance before the first write.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        if not self._schema_ready:
            from darkroom.cataloger import init_db

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            init_db(self.db_path)
            self._schema_ready = True

    def _open(self):
        """open_db, ensuring the parent directory exists first (open_db's
        own lazy init_db call assumes the directory is already there)."""
        from darkroom.catalog_db import open_db

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return open_db(self.db_path)

    # -- writes --------------------------------------------------------

    def upsert_session(self, session: dict) -> None:
        from darkroom.cataloger import upsert_session

        self._ensure_schema()
        upsert_session(self.db_path, session)

    def upsert_calibration_set(self, cal_set: dict) -> None:
        from darkroom.cataloger import upsert_calibration_set

        self._ensure_schema()
        upsert_calibration_set(self.db_path, cal_set)

    def set_processed_state(
        self,
        session_id: str,
        *,
        state: str,
        processed_date: str | None = None,
        processed_path: str | None = None,
        notes: str | None = None,
    ) -> bool:
        from darkroom.cataloger import set_processed_state

        self._ensure_schema()
        return set_processed_state(
            self.db_path,
            session_id,
            state=state,
            processed_date=processed_date,
            processed_path=processed_path,
            notes=notes,
        )

    # -- reads / in-place field updates ---------------------------------

    def update_session_fields(self, session_id: str, **fields) -> bool:
        from darkroom.catalog_db import update_session_fields

        conn = self._open()
        try:
            return update_session_fields(conn, session_id, **fields)
        finally:
            conn.close()

    def query_sessions(
        self,
        *,
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        from darkroom.catalog_db import query_sessions

        conn = self._open()
        try:
            return query_sessions(
                conn,
                target=target, obs_date=obs_date, session_id=session_id,
                camera=camera, ota=ota, filter=filter,
                date_from=date_from, date_to=date_to,
                processed_state=processed_state,
                limit=limit, offset=offset,
            )
        finally:
            conn.close()

    def count_sessions(
        self,
        *,
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
    ) -> int:
        from darkroom.catalog_db import count_sessions

        conn = self._open()
        try:
            return count_sessions(
                conn,
                target=target, obs_date=obs_date, session_id=session_id,
                camera=camera, ota=ota, filter=filter,
                date_from=date_from, date_to=date_to,
                processed_state=processed_state,
            )
        finally:
            conn.close()

    def query_calibration_sets(
        self,
        *,
        frame_type: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        gain: int | None = None,
        exposure_sec: float | None = None,
    ) -> list[dict]:
        from darkroom.catalog_db import query_calibration_sets

        conn = self._open()
        try:
            return query_calibration_sets(
                conn,
                frame_type=frame_type, camera=camera, ota=ota,
                filter=filter, gain=gain, exposure_sec=exposure_sec,
            )
        finally:
            conn.close()


class HttpBackend:
    """Thin httpx client for the catalog webapi server (darkroom/webapi/).

    httpx is imported lazily so `import darkroom.catalog_client` never pays
    its cost when no server is configured (the LocalBackend default path).
    """

    def __init__(self, base_url: str, token: str | None = None, *, client=None):
        self.base_url = base_url
        self.token = token
        if client is None:
            import httpx

            headers = {"Authorization": f"Bearer {token}"} if token else {}
            client = httpx.Client(base_url=base_url, timeout=30.0, headers=headers)
        self._client = client

    def close(self) -> None:
        self._client.close()

    def _check(self, resp) -> None:
        if resp.status_code == 401:
            raise RuntimeError(
                "catalog API rejected token (401) — check DARKROOM_API_TOKEN"
            )

    @staticmethod
    def _params(**kwargs) -> dict:
        return {k: v for k, v in kwargs.items() if v is not None}

    # -- writes --------------------------------------------------------

    def upsert_session(self, session: dict) -> None:
        resp = self._client.post("/api/sessions", json=session)
        self._check(resp)
        resp.raise_for_status()

    def upsert_calibration_set(self, cal_set: dict) -> None:
        resp = self._client.post("/api/calibration-sets", json=cal_set)
        self._check(resp)
        resp.raise_for_status()

    def set_processed_state(
        self,
        session_id: str,
        *,
        state: str,
        processed_date: str | None = None,
        processed_path: str | None = None,
        notes: str | None = None,
    ) -> bool:
        resp = self._client.post(
            f"/api/sessions/{session_id}/state",
            json={
                "state": state,
                "processed_date": processed_date,
                "processed_path": processed_path,
                "notes": notes,
            },
        )
        self._check(resp)
        if resp.status_code == 404:
            return False
        if resp.status_code == 400:
            raise ValueError(resp.json()["detail"])
        resp.raise_for_status()
        return resp.json()["updated"]

    def update_session_fields(self, session_id: str, **fields) -> bool:
        resp = self._client.patch(f"/api/sessions/{session_id}", json=fields)
        self._check(resp)
        if resp.status_code == 404:
            return False
        if resp.status_code == 400:
            raise ValueError(resp.json()["detail"])
        resp.raise_for_status()
        return True

    # -- reads -----------------------------------------------------------

    def query_sessions(
        self,
        *,
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        params = self._params(
            target=target, obs_date=obs_date, session_id=session_id,
            camera=camera, ota=ota, filter=filter,
            date_from=date_from, date_to=date_to,
            processed_state=processed_state,
        )
        if limit is not None:
            params["limit"] = limit
            params["offset"] = offset
        resp = self._client.get("/api/sessions", params=params)
        self._check(resp)
        resp.raise_for_status()
        return resp.json()

    def count_sessions(
        self,
        *,
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
    ) -> int:
        params = self._params(
            target=target, obs_date=obs_date, session_id=session_id,
            camera=camera, ota=ota, filter=filter,
            date_from=date_from, date_to=date_to,
            processed_state=processed_state,
        )
        resp = self._client.get("/api/sessions/count", params=params)
        self._check(resp)
        resp.raise_for_status()
        return resp.json()["count"]

    def query_calibration_sets(
        self,
        *,
        frame_type: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        gain: int | None = None,
        exposure_sec: float | None = None,
    ) -> list[dict]:
        params = self._params(
            frame_type=frame_type, camera=camera, ota=ota,
            filter=filter, gain=gain, exposure_sec=exposure_sec,
        )
        resp = self._client.get("/api/calibration-sets", params=params)
        self._check(resp)
        resp.raise_for_status()
        return resp.json()


def resolve_backend(
    catalog_flag: str | None = None,
    *,
    url_flag: str | None = None,
    token_flag: str | None = None,
) -> CatalogBackend:
    """Select a CatalogBackend: catalog_url configured -> HttpBackend, else LocalBackend.

    This is the whole point of W9's client/server split: existing local/
    offline workflows (and every test that doesn't set the URL) are
    unaffected, since url unset -> LocalBackend using the same path
    resolution as before (darkroom.config.resolve_catalog).
    """
    url = config.resolve_catalog_url(url_flag)
    if url:
        return HttpBackend(url, token=config.resolve_api_token(token_flag))
    return LocalBackend(config.resolve_catalog(catalog_flag))
