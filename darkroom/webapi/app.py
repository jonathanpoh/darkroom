"""darkroom.webapi.app — always-on catalog API server (W9).

Deployment: `uvicorn --factory darkroom.webapi.app:create_app_from_env`

This is a thin transport wrapper: request/response handling only. All catalog
logic (schema, upserts, queries, field validation) lives in
`darkroom.cataloger` and `darkroom.catalog_db` — this module never duplicates
it. `darkroom.cataloger` imports astropy, so it's imported lazily inside
route handlers/`create_app`, never at module import time, keeping this
module's own import light (mirrors the convention documented in
`darkroom/catalog_db.py`).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from darkroom import catalog_db, config
from darkroom.webapi import auth
from darkroom.webapi.ui import COOKIE_NAME, SESSION_MAX_AGE_SECONDS, build_ui_router

_STATIC_DIR = Path(__file__).parent / "static"


class SessionIn(BaseModel):
    session_id: str
    target: str | None = None
    obs_date: str | None = None
    ota: str | None = None
    camera: str | None = None
    filter: str | None = None
    gain: int | None = None
    temperature_c: float | None = None
    exposure_sec: float | None = None
    focal_length: float | None = None
    frame_count: int | None = None
    total_integration_sec: int | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    site_lat: float | None = None
    site_lon: float | None = None
    lights_path: str | None = None
    notes: str | None = None


class CalibrationSetIn(BaseModel):
    set_id: str
    frame_type: str | None = None
    camera: str | None = None
    ota: str | None = None
    filter: str | None = None
    gain: int | None = None
    exposure_sec: float | None = None
    temperature_c: float | None = None
    frame_count: int | None = None
    capture_date: str | None = None
    folder_path: str | None = None
    is_master: int | None = None


class StateIn(BaseModel):
    state: str
    processed_date: str | None = None
    processed_path: str | None = None
    notes: str | None = None


class TargetRenameIn(BaseModel):
    old_target: str
    new_target: str


class SiteIn(BaseModel):
    name: str
    lat: float
    lon: float
    radius_m: float = 1000.0
    bortle: int | None = None
    sqm: float | None = None
    is_home: bool = False


def create_app(db_path: Path, api_token: str, ui_password_hash: str) -> FastAPI:
    """Build the catalog API FastAPI app, bound to a single SQLite catalog file.

    The server owns the schema: `cataloger.init_db` runs once here, at app
    construction, so the file/tables exist before any route is hit.
    """
    db_path = Path(db_path)

    from darkroom import cataloger

    cataloger.init_db(db_path)

    app = FastAPI(title="darkroom catalog API")

    def _check_auth(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {api_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def _get_conn():
        conn = catalog_db.open_db(db_path)
        try:
            yield conn
        finally:
            conn.close()

    auth_dep = Depends(_check_auth)

    @app.post("/api/sessions", status_code=204, dependencies=[auth_dep])
    def post_session(body: SessionIn) -> None:
        from darkroom import cataloger

        cataloger.upsert_session(db_path, body.model_dump())

    @app.post("/api/calibration-sets", status_code=204, dependencies=[auth_dep])
    def post_calibration_set(body: CalibrationSetIn) -> None:
        from darkroom import cataloger

        data = body.model_dump()
        if data.get("is_master") is None:
            # cataloger.upsert_calibration_set uses setdefault("is_master", 0),
            # which won't replace an explicit None — drop the key so the
            # server-side default actually applies.
            data.pop("is_master", None)
        cataloger.upsert_calibration_set(db_path, data)

    @app.patch("/api/sessions/{session_id}", dependencies=[auth_dep])
    def patch_session(session_id: str, body: dict[str, Any], conn=Depends(_get_conn)):
        try:
            updated = catalog_db.update_session_fields(conn, session_id, **body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")
        return {"updated": True}

    @app.delete("/api/sessions/{session_id}", dependencies=[auth_dep])
    def delete_session(session_id: str, conn=Depends(_get_conn)):
        deleted = catalog_db.delete_session(conn, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="session not found")
        return {"deleted": True}

    @app.post("/api/targets/rename", dependencies=[auth_dep])
    def post_target_rename(body: TargetRenameIn, conn=Depends(_get_conn)):
        try:
            result = catalog_db.rename_target(conn, body.old_target, body.new_target)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if result["total"] == 0:
            raise HTTPException(status_code=404, detail="no sessions match old_target")
        return result

    @app.post("/api/sessions/{session_id}/state", dependencies=[auth_dep])
    def post_session_state(session_id: str, body: StateIn):
        from darkroom import cataloger

        try:
            updated = cataloger.set_processed_state(
                db_path,
                session_id,
                state=body.state,
                processed_date=body.processed_date,
                processed_path=body.processed_path,
                notes=body.notes,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")
        return {"updated": True}

    @app.get("/api/sessions", dependencies=[auth_dep])
    def get_sessions(
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
        conn=Depends(_get_conn),
    ):
        return catalog_db.query_sessions(
            conn,
            target=target,
            obs_date=obs_date,
            session_id=session_id,
            camera=camera,
            ota=ota,
            filter=filter,
            date_from=date_from,
            date_to=date_to,
            processed_state=processed_state,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/sessions/count", dependencies=[auth_dep])
    def get_sessions_count(
        target: str | None = None,
        obs_date: str | None = None,
        session_id: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        processed_state: str | None = None,
        conn=Depends(_get_conn),
    ):
        count = catalog_db.count_sessions(
            conn,
            target=target,
            obs_date=obs_date,
            session_id=session_id,
            camera=camera,
            ota=ota,
            filter=filter,
            date_from=date_from,
            date_to=date_to,
            processed_state=processed_state,
        )
        return {"count": count}

    @app.get("/api/pending-renames", dependencies=[auth_dep])
    def get_pending_renames(conn=Depends(_get_conn)):
        return catalog_db.list_pending_renames(conn)

    @app.delete("/api/pending-renames/{rename_id}", dependencies=[auth_dep])
    def delete_pending_rename(rename_id: int, conn=Depends(_get_conn)):
        acked = catalog_db.ack_pending_rename(conn, rename_id)
        if not acked:
            raise HTTPException(status_code=404, detail="pending rename not found")
        return {"deleted": True}

    @app.get("/api/sites", dependencies=[auth_dep])
    def get_sites(conn=Depends(_get_conn)):
        return catalog_db.list_sites(conn)

    @app.post("/api/sites", status_code=201, dependencies=[auth_dep])
    def post_site(body: SiteIn, conn=Depends(_get_conn)):
        try:
            site_id = catalog_db.add_site(conn, **body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"id": site_id}

    @app.patch("/api/sites/{name}", dependencies=[auth_dep])
    def patch_site(name: str, body: dict[str, Any], conn=Depends(_get_conn)):
        try:
            updated = catalog_db.update_site_fields(conn, name, **body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not updated:
            raise HTTPException(status_code=404, detail="site not found")
        return {"updated": True}

    @app.get("/api/calibration-sets", dependencies=[auth_dep])
    def get_calibration_sets(
        frame_type: str | None = None,
        camera: str | None = None,
        ota: str | None = None,
        filter: str | None = None,
        gain: int | None = None,
        exposure_sec: float | None = None,
        conn=Depends(_get_conn),
    ):
        return catalog_db.query_calibration_sets(
            conn,
            frame_type=frame_type,
            camera=camera,
            ota=ota,
            filter=filter,
            gain=gain,
            exposure_sec=exposure_sec,
        )

    app.include_router(build_ui_router(db_path, ui_password_hash))
    # No auth on static assets (CSS/JS/fonts) — nothing sensitive lives here,
    # and the login page itself needs the CSS before logging in.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _refresh_session_cookie(request, call_next):
        # Sliding session: any authenticated hit resets the 90-day clock, so a
        # machine used at least once per window never sees the login page.
        response = await call_next(request)
        cookie_token = request.cookies.get(COOKIE_NAME)
        if auth.verify_cookie(ui_password_hash, cookie_token):
            response.set_cookie(
                COOKIE_NAME,
                auth.mint_cookie(ui_password_hash, SESSION_MAX_AGE_SECONDS),
                httponly=True, samesite="lax",
                max_age=SESSION_MAX_AGE_SECONDS,
            )
        return response

    return app


def create_app_from_env() -> FastAPI:
    """Build the app from environment: DARKROOM_API_TOKEN + DARKROOM_UI_PASSWORD_HASH
    + DARKROOM_CATALOG.

    Used by the uvicorn factory deployment (see module docstring). Raises
    RuntimeError if DARKROOM_API_TOKEN or DARKROOM_UI_PASSWORD_HASH is unset
    or empty — the server must not start without both an API auth token and
    a UI password hash (see `darkroom.webapi.passwd` to generate the latter).
    """
    token = os.environ.get("DARKROOM_API_TOKEN")
    if not token:
        raise RuntimeError(
            "DARKROOM_API_TOKEN environment variable must be set to a "
            "non-empty value to start the darkroom catalog API server."
        )
    ui_password_hash = os.environ.get("DARKROOM_UI_PASSWORD_HASH")
    if not ui_password_hash:
        raise RuntimeError(
            "DARKROOM_UI_PASSWORD_HASH environment variable must be set to a "
            "non-empty value to start the darkroom catalog API server."
        )
    db_path = config.resolve_catalog(None)
    return create_app(db_path, token, ui_password_hash)
