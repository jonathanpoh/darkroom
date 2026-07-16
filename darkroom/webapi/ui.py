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

import re
import time
import urllib.parse
from collections import Counter, deque
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from darkroom import catalog_db
from darkroom.names import KNOWN_FILTERS, _normalize_target
from darkroom.sites import home_sqm, resolve_site, session_weight
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


def _build_aggregate(rows: list[dict], sites: list[dict] | None = None) -> list[dict]:
    """Group session rows by target into the shape the safelight JS expects.

    Mirrors the mock's `catalog_agg` structure: one entry per target with
    integration hours broken down by filter, processed-state counts, the most
    recent obs_date, and a `nights` list (one per session) that the client-side
    renderer groups by rig (OTA + camera) and sorts/filters interactively.

    `sites` (from `catalog_db.list_sites`) drives SQM-based weighting: each
    night's raw hours `h` are scaled by `session_weight(site, home)` into
    `wh` ("home-equivalent hours"), where `site` is resolved from the
    session's site_lat/site_lon and `home` is the is_home site's sqm. With no
    sites, no home sqm, or NULL session coords, weight is always 1.0 and
    `wh`/`total_wh` equal `h`/`total_h` exactly — this keeps the aggregate
    unchanged for callers/fixtures that don't pass `sites`.
    """
    home = home_sqm(sites) if sites else None

    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["target"], []).append(row)

    aggregate: list[dict] = []
    for tgt, sessions in groups.items():
        nights = []
        hours: dict[str, float] = {}
        states: dict[str, int] = {}
        total_wh = 0.0
        for s in sessions:
            h = (s["total_integration_sec"] or 0) / 3600.0
            filt = s["filter"] or "None"
            hours[filt] = hours.get(filt, 0.0) + h
            state = s["processed_state"] or "unprocessed"
            states[state] = states.get(state, 0) + 1
            site = (
                resolve_site(s.get("site_lat"), s.get("site_lon"), sites)
                if sites else None
            )
            w = session_weight(site, home)
            wh = h * w
            total_wh += wh
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
                "site": site["name"] if site else None,
                "w": round(w, 3),
                "wh": wh,
            })
        total_h = sum(hours.values())
        last = max((s["obs_date"] for s in sessions if s["obs_date"]), default=None)
        aggregate.append({
            "target": tgt,
            "cname": common_name(tgt),
            "n": len(sessions),
            "hours": hours,
            "total_h": total_h,
            "total_wh": total_wh,
            "states": states,
            "last": last,
            "nights": nights,
        })
    return aggregate


def _date_diff(a: str | None, b: str | None) -> int | None:
    """Return |days between two ISO date strings|, or None if either is missing/unparseable."""
    if not a or not b:
        return None
    try:
        return abs((date_cls.fromisoformat(a) - date_cls.fromisoformat(b)).days)
    except ValueError:
        return None


def _is_unknown_ota(ota: str | None) -> bool:
    return ota is None or ota == "" or ota == "Unknown"


def _neighbour_filters(row: dict, all_rows: list[dict], limit: int = 3) -> list[dict]:
    """Other sessions of the same target with a known filter, nearest date first.

    Same-camera matches rank ahead of other-camera matches at equal date
    distance. Each hint carries `camera` only when it differs from `row`'s,
    so the template can show it just for the cases where it matters.
    """
    candidates = []
    for other in all_rows:
        if other["session_id"] == row["session_id"]:
            continue
        if other["target"] != row["target"]:
            continue
        if other["filter"] not in KNOWN_FILTERS:
            continue
        dist = _date_diff(row["obs_date"], other["obs_date"])
        if dist is None:
            continue
        same_camera = other["camera"] == row["camera"]
        candidates.append((dist, 0 if same_camera else 1, other))
    candidates.sort(key=lambda c: (c[0], c[1]))
    hints = []
    for dist, _, other in candidates[:limit]:
        hints.append({
            "filter": other["filter"],
            "camera": None if other["camera"] == row["camera"] else other["camera"],
            "obs_date": other["obs_date"],
            "dist": dist,
        })
    return hints


def _flat_hints(row: dict, flat_sets: list[dict], window_days: int = 7, limit: int = 3) -> list[dict]:
    """Calibration Flat sets near this session's date, same camera (+ OTA if known)."""
    candidates = []
    for cal in flat_sets:
        if cal["camera"] != row["camera"]:
            continue
        if not _is_unknown_ota(row["ota"]) and cal["ota"] != row["ota"]:
            continue
        dist = _date_diff(row["obs_date"], cal["capture_date"])
        if dist is None or dist > window_days:
            continue
        candidates.append((dist, cal))
    candidates.sort(key=lambda c: c[0])
    return [
        {"filter": cal["filter"], "capture_date": cal["capture_date"], "dist": dist}
        for dist, cal in candidates[:limit]
    ]


def _build_queue(conn) -> tuple[list[dict], list[dict]]:
    """Return (unknown_filter_rows, suspicious_value_rows), each obs_date-desc.

    'unknown filter' = filter IS NULL or 'UnknownFilter' (never parsed).
    'suspicious value' = filter is set but isn't one of KNOWN_FILTERS (the
    panel-name-in-filter-column garbage rows). Every row also carries an
    `unknown_ota` badge flag and context hints (neighbour sessions, nearby
    flats) to jog the user's memory when fixing it inline.
    """
    all_rows = catalog_db.query_sessions(conn)
    flat_sets = catalog_db.query_calibration_sets(conn, frame_type="Flat")

    unknown_rows: list[dict] = []
    suspicious_rows: list[dict] = []
    for row in all_rows:
        filt = row["filter"]
        if filt is None or filt == "UnknownFilter":
            section = unknown_rows
        elif filt not in KNOWN_FILTERS:
            section = suspicious_rows
        else:
            continue

        entry = dict(row)
        entry["unknown_ota"] = _is_unknown_ota(row["ota"])
        entry["neighbour_filters"] = _neighbour_filters(row, all_rows)
        entry["flat_hints"] = _flat_hints(row, flat_sets)
        section.append(entry)

    unknown_rows.sort(key=lambda r: r["obs_date"] or "", reverse=True)
    suspicious_rows.sort(key=lambda r: r["obs_date"] or "", reverse=True)
    return unknown_rows, suspicious_rows


def _known_otas(conn) -> list[str]:
    """Distinct non-null, non-'Unknown' OTA values on record — for the fix form's select."""
    rows = conn.execute(
        "SELECT DISTINCT ota FROM sessions "
        "WHERE ota IS NOT NULL AND ota != '' AND ota != 'Unknown' ORDER BY ota"
    ).fetchall()
    return [r[0] for r in rows]


def _all_targets(conn) -> list[str]:
    """Every session's target value, one entry per session (repeats expected).

    Feeds `_target_suggestions` (which needs per-target session counts) and
    the manual merge form's target dropdown (session counts there come from
    the same list via Counter).
    """
    rows = conn.execute(
        "SELECT target FROM sessions WHERE target IS NOT NULL"
    ).fetchall()
    return [r[0] for r in rows]


# Mosaic panel suffix, e.g. "IC 4604_1-1" -> base "IC 4604" (U2 phase 3
# heuristic a). Suggested even when the base isn't itself an existing target.
_PANEL_SUFFIX_RE = re.compile(r"^(.*)_\d+-\d+$")

# Two catalog-style designations back to back with nothing else, e.g.
# "M 82 M 82" (duplicated) or "M 81 M 82" (two different designations,
# ambiguous unless the first is itself an existing target) — heuristic b.
_DOUBLE_DESIGNATION_RE = re.compile(r"^([A-Za-z]+\s*\d+[\w-]*)\s+([A-Za-z]+\s*\d+[\w-]*)$")


def _target_suggestions(targets: list[str]) -> list[dict]:
    """Suggest merge targets for suspect duplicate/variant target names (U2 phase 3).

    Pure — no DB access. `targets` is every session's target value, one
    entry per session (repeats expected and used to compute each
    suggestion's `count`); candidate names are the distinct values within it.

    Heuristics are tried in priority order per target, first match wins:
      a. Mosaic panel suffix (`_N-M` at the end) -> strip it, normalize.
      b. Duplicated designation ("M 82 M 82" -> "M 82") or two distinct
         designations where the first is itself an existing target
         ("M 81 M 82" -> "M 81", but ONLY if "M 81" already exists —
         otherwise it's ambiguous and no suggestion is made).
      c. Normalization drift: `_normalize_target(target) != target`.

    A target that matches nothing, or whose only candidate suggestion is
    itself (a self-map), gets no entry in the result.
    """
    counts = Counter(targets)
    distinct = sorted(counts)
    distinct_normalized = {_normalize_target(t) for t in distinct}

    suggestions: list[dict] = []
    for target in distinct:
        suggested: str | None = None

        m = _PANEL_SUFFIX_RE.match(target)
        if m:
            base = _normalize_target(m.group(1).strip())
            if base:
                suggested = base

        if suggested is None:
            m = _DOUBLE_DESIGNATION_RE.match(target)
            if m:
                d1 = _normalize_target(m.group(1).strip())
                d2 = _normalize_target(m.group(2).strip())
                if d1 == d2:
                    suggested = d1
                elif d1 in distinct_normalized:
                    suggested = d1
                # else: two different designations and the first isn't a
                # known target — ambiguous, no suggestion from this rule.

        if suggested is None:
            norm = _normalize_target(target)
            if norm != target:
                suggested = norm

        if suggested is None or suggested == target:
            continue

        suggestions.append({
            "target": target,
            "suggested": suggested,
            "count": counts[target],
        })

    return suggestions


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
            sites = catalog_db.list_sites(conn)
        finally:
            conn.close()

        return templates.TemplateResponse(
            request,
            "index.html",
            {"data": _build_aggregate(rows, sites)},
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
            sites = catalog_db.list_sites(conn)
        finally:
            conn.close()
        if not rows:
            raise HTTPException(status_code=404, detail="target not found")

        aggregate = _build_aggregate(rows, sites)
        # query_sessions normalises `target` case/spacing-insensitively, so
        # aggregate[0]["target"] is the canonical form even if the URL segment
        # wasn't (e.g. "m81" -> "M 81") — scope strictly to that one entry.
        return templates.TemplateResponse(
            request,
            "target.html",
            {"data": aggregate, "target": aggregate[0]["target"]},
        )

    def _queue_context() -> dict:
        conn = _get_conn()
        try:
            unknown_rows, suspicious_rows = _build_queue(conn)
            known_otas = _known_otas(conn)
            pending_renames = catalog_db.list_pending_renames(conn)
            all_targets = _all_targets(conn)
        finally:
            conn.close()
        return {
            "unknown_rows": unknown_rows,
            "suspicious_rows": suspicious_rows,
            "total_count": len(unknown_rows) + len(suspicious_rows),
            "known_filters": KNOWN_FILTERS,
            "known_otas": known_otas,
            "pending_renames_count": len(pending_renames),
            "target_suggestions": _target_suggestions(all_targets),
            "target_counts": sorted(Counter(all_targets).items()),
        }

    @router.get("/queue", response_class=HTMLResponse)
    def queue(
        request: Request,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        ctx = _queue_context()
        ctx["error"] = None
        ctx["success"] = None
        return templates.TemplateResponse(request, "queue.html", ctx)

    @router.post("/queue/{session_id}/fix")
    async def queue_fix(
        request: Request,
        session_id: str,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        form_data = await request.form()
        filt = form_data.get("filter")
        ota_raw = form_data.get("ota")
        ota = ota_raw.strip() if isinstance(ota_raw, str) else ota_raw

        if filt not in KNOWN_FILTERS:
            ctx = _queue_context()
            ctx["error"] = (
                f"{session_id}: filter must be one of {', '.join(KNOWN_FILTERS)}"
            )
            return templates.TemplateResponse(
                request, "queue.html", ctx, status_code=400
            )

        changed: dict[str, Any] = {"filter": filt}
        if ota:
            changed["ota"] = ota

        conn = _get_conn()
        try:
            try:
                updated = catalog_db.update_session_fields(conn, session_id, **changed)
            except ValueError as e:
                ctx = _queue_context()
                ctx["error"] = f"{session_id}: {e}"
                return templates.TemplateResponse(
                    request, "queue.html", ctx, status_code=400
                )
            if not updated:
                raise HTTPException(status_code=404, detail="session not found")
        finally:
            conn.close()

        return RedirectResponse("/queue", status_code=303)

    @router.post("/queue/targets/rename")
    async def queue_targets_rename(
        request: Request,
        darkroom_token: str | None = Cookie(default=None),
    ):
        redirect = _require_auth(request, darkroom_token)
        if redirect:
            return redirect

        form_data = await request.form()
        old_target = form_data.get("old_target") or ""
        new_target = form_data.get("new_target") or ""
        old_target = old_target.strip() if isinstance(old_target, str) else old_target
        new_target = new_target.strip() if isinstance(new_target, str) else new_target

        conn = _get_conn()
        try:
            try:
                result = catalog_db.rename_target(conn, old_target, new_target)
            except ValueError as e:
                ctx = _queue_context()
                ctx["error"] = str(e)
                ctx["success"] = None
                return templates.TemplateResponse(
                    request, "queue.html", ctx, status_code=400
                )
        finally:
            conn.close()

        if result["total"] == 0:
            ctx = _queue_context()
            ctx["error"] = f"No sessions found for target {old_target!r}"
            ctx["success"] = None
            return templates.TemplateResponse(
                request, "queue.html", ctx, status_code=404
            )

        ctx = _queue_context()
        ctx["success"] = (
            f"renamed {result['renamed']} session"
            f"{'' if result['renamed'] == 1 else 's'} of {old_target} → {new_target}"
            if result["renamed"] else None
        )
        if result["errors"]:
            details = "; ".join(
                f"{e['session_id']}: {e['error']}" for e in result["errors"]
            )
            ctx["error"] = (
                f"{len(result['errors'])} session"
                f"{'' if len(result['errors']) == 1 else 's'} failed to merge: {details}"
            )
            status_code = 200 if result["renamed"] else 400
        else:
            ctx["error"] = None
            status_code = 200
        return templates.TemplateResponse(
            request, "queue.html", ctx, status_code=status_code
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
