"""darkroom.webapi.ui — Jinja2 browser UI for the catalog web API (W9 phase 2).

Sits alongside the bearer-token `/api` routes in `darkroom.webapi.app`, as a
separate router mounted on the same app. Auth is a separate password (not the
API bearer token) checked once at /login, which mints an HMAC-signed,
stateless session cookie (see `darkroom.webapi.auth`) — this is a convenience
layer for humans in a browser, not a new trust boundary: it must never grant
access to the `/api` routes, which stay bearer-only.

Like `darkroom.webapi.app`, this module keeps its own import light:
`darkroom.cataloger` (astropy) is only imported lazily, inside handlers that
actually need it.
"""

from __future__ import annotations

import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from darkroom import catalog_db
from darkroom.webapi import auth
from darkroom.webapi.common_names import common_name

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "catalog"
COOKIE_NAME = "darkroom_token"
# 90 days: single-user LAN/tailnet tool, re-logging-in every browser session
# is friction without a threat model to justify it. Sliding window (see
# app.py's cookie-refresh middleware) means this resets on every visit, so it
# only bites a machine that's gone untouched for the full 90 days.
SESSION_MAX_AGE_SECONDS = 90 * 24 * 3600

# Login rate limiting: module-level, in-memory, per-client-IP. Window and
# limit are small and single-user-appropriate — this is a brake on brute
# force from one source, not a distributed defence.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, deque[float]] = {}


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


def _throttled(ip: str) -> bool:
    now = time.time()
    attempts = _LOGIN_FAILURES.get(ip)
    if not attempts:
        return False
    while attempts and now - attempts[0] > _RATE_LIMIT_WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts) >= _RATE_LIMIT_MAX_FAILURES


def _record_failure(ip: str) -> None:
    now = time.time()
    attempts = _LOGIN_FAILURES.setdefault(ip, deque())
    attempts.append(now)
    while attempts and now - attempts[0] > _RATE_LIMIT_WINDOW_SECONDS:
        attempts.popleft()


def reset_login_rate_limit() -> None:
    """Clear all recorded login failures. Test-only helper."""
    _LOGIN_FAILURES.clear()


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


def _build_aggregate(rows: list[dict]) -> list[dict]:
    """Group session rows by target into the shape the safelight JS expects.

    Mirrors the mock's `catalog_agg` structure: one entry per target with
    integration hours broken down by filter, processed-state counts, the most
    recent obs_date, and a `nights` list (one per session) that the client-side
    renderer groups by rig (OTA + camera) and sorts/filters interactively.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["target"], []).append(row)

    aggregate: list[dict] = []
    for tgt, sessions in groups.items():
        nights = []
        hours: dict[str, float] = {}
        states: dict[str, int] = {}
        for s in sessions:
            h = (s["total_integration_sec"] or 0) / 3600.0
            filt = s["filter"] or "None"
            hours[filt] = hours.get(filt, 0.0) + h
            state = s["processed_state"] or "unprocessed"
            states[state] = states.get(state, 0) + 1
            nights.append({
                "date": s["obs_date"],
                "ota": s["ota"],
                "camera": s["camera"],
                "filter": s["filter"],
                "exp": s["exposure_sec"],
                "gain": s["gain"],
                "frames": s["frame_count"],
                "h": h,
                "state": state,
                "sid": s["session_id"],
            })
        total_h = sum(hours.values())
        last = max((s["obs_date"] for s in sessions if s["obs_date"]), default=None)
        aggregate.append({
            "target": tgt,
            "cname": common_name(tgt),
            "n": len(sessions),
            "hours": hours,
            "total_h": total_h,
            "states": states,
            "last": last,
            "nights": nights,
        })
    return aggregate


def build_ui_router(db_path: Path, ui_password_hash: str) -> APIRouter:
    """Build the Jinja2 UI router, bound to the DB + UI password hash."""
    db_path = Path(db_path)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    router = APIRouter()

    def _get_conn():
        return catalog_db.open_db(db_path)

    def _authed(cookie_value: str | None) -> bool:
        return auth.verify_cookie(ui_password_hash, cookie_value)

    def _require_auth(request: Request, darkroom_token: str | None) -> RedirectResponse | None:
        """Return a redirect-to-login response if the cookie is missing/wrong, else None."""
        if not _authed(darkroom_token):
            return RedirectResponse(
                f"/login?next={request.url.path}", status_code=303
            )
        return None

    def _login_redirect(next_: str) -> RedirectResponse:
        resp = RedirectResponse(_safe_next(next_), status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            auth.mint_cookie(ui_password_hash, SESSION_MAX_AGE_SECONDS),
            httponly=True, samesite="lax",
            max_age=SESSION_MAX_AGE_SECONDS,
        )
        return resp

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return templates.TemplateResponse(
            request, "login.html", {"error": None, "next": _safe_next(next)}
        )

    @router.post("/login")
    def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
        ip = _client_ip(request)
        if _throttled(ip):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Too many attempts — try again in a minute",
                    "next": _safe_next(next),
                },
                status_code=429,
            )
        if not auth.verify_password(password, ui_password_hash):
            _record_failure(ip)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid password", "next": _safe_next(next)},
                status_code=400,
            )
        return _login_redirect(next)

    @router.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @router.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        conn = _get_conn()
        try:
            rows = catalog_db.query_sessions(conn)
        finally:
            conn.close()

        return templates.TemplateResponse(
            request,
            "index.html",
            {"data": _build_aggregate(rows)},
        )

    @router.get("/targets/{target}", response_class=HTMLResponse)
    def target_detail(
        request: Request,
        target: str,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        conn = _get_conn()
        try:
            rows = catalog_db.query_sessions(conn, target=target)
        finally:
            conn.close()
        if not rows:
            raise HTTPException(status_code=404, detail="target not found")

        aggregate = _build_aggregate(rows)
        # query_sessions normalises `target` case/spacing-insensitively, so
        # aggregate[0]["target"] is the canonical form even if the URL segment
        # wasn't (e.g. "m81" -> "M 81") — scope strictly to that one entry.
        return templates.TemplateResponse(
            request,
            "target.html",
            {"data": aggregate, "target": aggregate[0]["target"]},
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

    @router.post("/sessions/{session_id}/delete")
    def delete_submit(
        request: Request,
        session_id: str,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        conn = _get_conn()
        try:
            rows = catalog_db.query_sessions(conn, session_id=session_id)
            if not rows:
                raise HTTPException(status_code=404, detail="session not found")
            target = rows[0]["target"]

            catalog_db.delete_session(conn, session_id)

            remaining = catalog_db.query_sessions(conn, target=target)
        finally:
            conn.close()

        if remaining:
            return RedirectResponse(
                f"/targets/{urllib.parse.quote(target)}", status_code=303
            )
        return RedirectResponse("/", status_code=303)

    return router
