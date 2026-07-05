"""darkroom.webapi.ui — Jinja2 browser UI for the catalog web API (W9 phase 2).

Sits alongside the bearer-token `/api` routes in `darkroom.webapi.app`, as a
separate router mounted on the same app. Auth is cookie-based (same token
value as the API, entered once via /login) — this is a convenience layer for
humans in a browser, not a new trust boundary: it must never grant access to
the `/api` routes, which stay bearer-only.

Like `darkroom.webapi.app`, this module keeps its own import light:
`darkroom.cataloger` (astropy) is only imported lazily, inside handlers that
actually need it.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from darkroom import catalog_db

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "catalog"
_COOKIE_NAME = "darkroom_token"

_EDIT_FIELDS = (
    "target", "obs_date", "ota", "camera", "filter",
    "gain", "temperature_c", "exposure_sec", "focal_length",
    "ra_deg", "dec_deg", "notes",
    "processed_state", "processed_path", "processed_date",
)
_NUMERIC_FIELDS = {
    "gain": int,
    "temperature_c": float,
    "exposure_sec": float,
    "focal_length": float,
    "ra_deg": float,
    "dec_deg": float,
}
_PROCESSED_STATES = ("unprocessed", "in_progress", "processed", "skipped")


def _safe_next(next_: str | None) -> str:
    """Only allow redirecting back to a relative in-app path (open-redirect guard)."""
    if next_ and next_.startswith("/") and not next_.startswith("//"):
        return next_
    return "/"


def build_ui_router(db_path: Path, api_token: str) -> APIRouter:
    """Build the Jinja2 UI router, bound to the same DB + token as the API."""
    db_path = Path(db_path)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    router = APIRouter()

    def _get_conn():
        return catalog_db.open_db(db_path)

    def _authed(token: str | None) -> bool:
        return token is not None and secrets.compare_digest(token, api_token)

    def _require_auth(request: Request, darkroom_token: str | None) -> RedirectResponse | None:
        """Return a redirect-to-login response if the cookie is missing/wrong, else None."""
        if not _authed(darkroom_token):
            return RedirectResponse(
                f"/login?next={request.url.path}", status_code=303
            )
        return None

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return templates.TemplateResponse(
            request, "login.html", {"error": None, "next": _safe_next(next)}
        )

    @router.post("/login")
    def login_submit(request: Request, token: str = Form(...), next: str = Form("/")):
        if not _authed(token):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid token", "next": _safe_next(next)},
                status_code=400,
            )
        resp = RedirectResponse(_safe_next(next), status_code=303)
        resp.set_cookie(_COOKIE_NAME, token, httponly=True, samesite="lax")
        return resp

    @router.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_COOKIE_NAME)
        return resp

    @router.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        darkroom_token: str | None = Cookie(default=None),
        processed_state: str | None = None,
        target: str | None = None,
        camera: str | None = None,
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        conn = _get_conn()
        try:
            rows = catalog_db.query_sessions(
                conn,
                processed_state=processed_state or None,
                target=target or None,
                camera=camera or None,
            )
            all_cameras = sorted(
                {r["camera"] for r in catalog_db.query_sessions(conn) if r["camera"]}
            )
        finally:
            conn.close()

        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(row["target"], []).append(row)
        target_groups = []
        for tgt in sorted(groups):
            sessions = sorted(groups[tgt], key=lambda r: r["obs_date"], reverse=True)
            total_hours = sum(s["total_integration_sec"] or 0 for s in sessions) / 3600.0
            target_groups.append({
                "target": tgt,
                "sessions": sessions,
                "count": len(sessions),
                "total_hours": total_hours,
            })

        query_string = str(request.url.query)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "target_groups": target_groups,
                "processed_states": _PROCESSED_STATES,
                "cameras": all_cameras,
                "filter_processed_state": processed_state or "",
                "filter_target": target or "",
                "filter_camera": camera or "",
                "query_string": query_string,
            },
        )

    @router.post("/sessions/{session_id}/state")
    def set_state(
        request: Request,
        session_id: str,
        darkroom_token: str | None = Cookie(default=None),
        state: str = Form(...),
        next: str = Form("/"),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        from darkroom import cataloger

        try:
            updated = cataloger.set_processed_state(db_path, session_id, state=state)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")
        return RedirectResponse(_safe_next(next), status_code=303)

    @router.get("/sessions/{session_id}", response_class=HTMLResponse)
    def edit_form(
        request: Request,
        session_id: str,
        darkroom_token: str | None = Cookie(default=None),
        error: str | None = None,
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        conn = _get_conn()
        try:
            rows = catalog_db.query_sessions(conn, session_id=session_id)
        finally:
            conn.close()
        if not rows:
            raise HTTPException(status_code=404, detail="session not found")

        return templates.TemplateResponse(
            request,
            "session.html",
            {
                "session": rows[0],
                "processed_states": _PROCESSED_STATES,
                "error": error,
            },
        )

    @router.post("/sessions/{session_id}")
    async def edit_submit(
        request: Request,
        session_id: str,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        form_data = await request.form()

        conn = _get_conn()
        try:
            current_rows = catalog_db.query_sessions(conn, session_id=session_id)
            if not current_rows:
                raise HTTPException(status_code=404, detail="session not found")
            current = current_rows[0]
            row_id = current["id"]
        finally:
            conn.close()

        # Convert form strings ('' -> None, numeric strings -> int/float) and
        # only pass fields that actually changed vs the current row, so an
        # untouched identity field never triggers a spurious session_id rename.
        changed: dict[str, Any] = {}
        for key in _EDIT_FIELDS:
            if key not in form_data:
                continue
            raw = form_data[key]
            raw = raw.strip() if isinstance(raw, str) else raw
            value: Any = raw if raw != "" else None
            if value is not None and key in _NUMERIC_FIELDS:
                try:
                    value = _NUMERIC_FIELDS[key](value)
                except ValueError:
                    conn = _get_conn()
                    try:
                        rows = catalog_db.query_sessions(conn, session_id=session_id)
                    finally:
                        conn.close()
                    return templates.TemplateResponse(
                        request,
                        "session.html",
                        {
                            "session": rows[0] if rows else current,
                            "processed_states": _PROCESSED_STATES,
                            "error": f"Invalid numeric value for {key!r}: {raw!r}",
                        },
                        status_code=400,
                    )
            if current.get(key) != value:
                changed[key] = value

        conn = _get_conn()
        try:
            if changed:
                try:
                    catalog_db.update_session_fields(conn, session_id, **changed)
                except ValueError as e:
                    rows = catalog_db.query_sessions(conn, session_id=session_id)
                    return templates.TemplateResponse(
                        request,
                        "session.html",
                        {
                            "session": rows[0] if rows else current,
                            "processed_states": _PROCESSED_STATES,
                            "error": str(e),
                        },
                        status_code=400,
                    )

            new_row = conn.execute(
                "SELECT session_id FROM sessions WHERE id = ?", (row_id,)
            ).fetchone()
        finally:
            conn.close()

        new_session_id = new_row["session_id"] if new_row else session_id
        return RedirectResponse(f"/sessions/{new_session_id}", status_code=303)

    return router
