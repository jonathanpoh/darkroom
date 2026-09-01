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

import json
import re
import time
import urllib.parse
from collections import Counter
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from darkroom import catalog_db
from darkroom.catalog import match_session_calibration
from darkroom.catalog_client import MemoryCalibrationBackend
from darkroom.names import KNOWN_FILTERS, PLACEHOLDERS, PROCESSED_STATES, _normalize_target
from darkroom.sites import annotate_sessions
from darkroom.webapi import auth
from darkroom.webapi.common_names import common_name

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "catalog"
COOKIE_NAME = "darkroom_token"
# 90 days: single-user LAN/tailnet tool, re-logging-in every browser session
# is friction without a threat model to justify it. Sliding window (see
# app.py's cookie-refresh middleware) means this resets on every visit, so it
# only bites a machine that's gone untouched for the full 90 days.
SESSION_MAX_AGE_SECONDS = 90 * 24 * 3600


def set_session_cookie(response: Response, ui_password_hash: str) -> None:
    """Mint a fresh session cookie onto `response` — login and the sliding refresh."""
    response.set_cookie(
        COOKIE_NAME,
        auth.mint_cookie(ui_password_hash, SESSION_MAX_AGE_SECONDS),
        httponly=True, samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )


class LoginRequired(Exception):
    """Raised by the UI router's auth dependency for a missing/invalid cookie.

    `app.py` registers `login_redirect` as its handler, so every protected
    route bounces to /login with the original path as `next`.
    """

    def __init__(self, next_path: str):
        super().__init__(next_path)
        self.next_path = next_path


def login_redirect(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(f"/login?next={exc.next_path}", status_code=303)


# Login rate limiting: module-level, in-memory, per-client-IP. Window and
# limit are small and single-user-appropriate — this is a brake on brute
# force from one source, not a distributed defence.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


def _recent_failures(ip: str) -> list[float]:
    """This IP's failure timestamps inside the window, pruned in place."""
    cutoff = time.time() - _RATE_LIMIT_WINDOW_SECONDS
    attempts = _LOGIN_FAILURES.setdefault(ip, [])
    attempts[:] = [t for t in attempts if t > cutoff]
    return attempts


def _throttled(ip: str) -> bool:
    return len(_recent_failures(ip)) >= _RATE_LIMIT_MAX_FAILURES


def _record_failure(ip: str) -> None:
    _recent_failures(ip).append(time.time())


def reset_login_rate_limit() -> None:
    """Clear all recorded login failures. Test-only helper."""
    _LOGIN_FAILURES.clear()


# The manual edit form's fields, in form order. A subset of
# catalog_db._EDITABLE_FIELDS: panel, start/end_utc, frame_count and
# total_integration_sec are catalog-derived and only reachable via the JSON
# API and the rescan apply path.
_EDIT_FIELDS = (
    "target", "obs_date", "ota", "camera", "filter",
    "gain", "temperature_c", "exposure_sec", "focal_length",
    "ra_deg", "dec_deg", "site_lat", "site_lon", "notes",
    "processed_state", "processed_path", "processed_date",
)
_NUMERIC_FIELDS = {
    "gain": int,
    "temperature_c": float,
    "exposure_sec": float,
    "focal_length": float,
    "ra_deg": float,
    "dec_deg": float,
    "site_lat": float,
    "site_lon": float,
}


def _safe_next(next_: str | None) -> str:
    """Only allow redirecting back to a relative in-app path (open-redirect guard)."""
    if next_ and next_.startswith("/") and not next_.startswith("//"):
        return next_
    return "/"


# ── guiding presentation (F4) ─────────────────────────────────────────────
# Every rule that turns a session_guiding row into a verdict lives here, once,
# and is carried on the row as a field: the night chip (app.js) and the
# session page (session.html) only render what they are handed.

# Guided seconds ÷ session wall span below this is a partial log, and the UI
# says so rather than letting the number read as the verdict on the night.
PARTIAL_COVERAGE_BELOW = 0.8


def _guide_band(rms: float) -> str:
    """good / fair / poor by total RMS. Absolute arcsec bands, not relative to
    the imaging scale: 1″ means something different at FRA400 than at FMA180,
    but the repo has no camera pixel-size table yet, and absolute bands are
    good enough to rank nights."""
    return "good" if rms < 1.0 else "fair" if rms <= 2.0 else "poor"


def _is_partial_coverage(coverage: float | None) -> bool:
    return coverage is not None and coverage < PARTIAL_COVERAGE_BELOW


def _is_spike_dominated(rms: float | None, p95: float | None) -> bool:
    """True when the total RMS is carried by a few catastrophic frames.

    RMS squares each error, so ten wrecked subs out of fifty can push an
    otherwise excellent night into the `poor` band. p95 is what a typical frame
    actually did, so `rms >= 2 * p95` separates the two cases: measured across
    the live catalog, clean nights sit at or below 1.0, a uniformly bad night
    (M 45 2025-09-22, rms 35.3″ / p95 28.3″) at ~1.2, and spike-dominated
    nights (NGC 6888 2026-07-20, rms 19.18″ / p95 2.11″) at 6–12.

    Presentation only — nothing here changes the stored numbers or the band.
    """
    if rms is None or p95 is None or p95 <= 0:
        return False
    return rms >= 2 * p95


def _decode_logs(row: dict) -> list:
    """`source_logs` is stored as a JSON array of log basenames; tolerate junk."""
    try:
        logs = json.loads(row.get("source_logs") or "[]")
    except (TypeError, ValueError):
        return []
    return logs if isinstance(logs, list) else []


def _guiding_summary(row: dict | None) -> dict | None:
    """Compact a `session_guiding` row into the shape the safelight JS reads.

    Short keys, like the rest of the night dict (`h`, `wh`, `sid`). Returns
    None for a missing row *or* one with no total RMS: both mean "not
    measured", which the UI shows as an em-dash rather than a number.
    """
    if row is None or row.get("rms_total_arcsec") is None:
        return None
    rms = row["rms_total_arcsec"]
    return {
        "rms": rms,
        "band": _guide_band(rms),
        "ra": row.get("rms_ra_arcsec"),
        "dec": row.get("rms_dec_arcsec"),
        "peak": row.get("peak_arcsec"),
        "p95": row.get("p95_arcsec"),
        "cov": row.get("coverage"),
        "partial": _is_partial_coverage(row.get("coverage")),
        "spike": _is_spike_dominated(rms, row.get("p95_arcsec")),
        "frames": row.get("guide_frames"),
        "lost": row.get("star_lost_events"),
        "dropped": row.get("dropped_frames"),
        "logs": _decode_logs(row),
    }


def _build_aggregate(
    rows: list[dict],
    sites: list[dict] | None = None,
    cal_rows: list[dict] | None = None,
    guiding_rows: list[dict] | None = None,
) -> list[dict]:
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

    `cal_rows` (every row from `catalog_db.query_calibration_sets`, in that
    function's order — see MemoryCalibrationBackend's row-order contract) adds a
    `cal` key to each night: what `wbpp` would find for darks/flats/flat-darks.
    Optional for the same reason `sites` is — omit it and the night dicts keep
    their previous shape, which is what `/` does since the overview shows no
    calibration state and shouldn't pay to compute it.

    `guiding_rows` (from `catalog_db.query_session_guiding`) adds a `guiding`
    key per night (F4), None where that session has no row — the common case,
    since guide logs only cover part of the archive's history. Optional on the
    same terms as `cal_rows`.

    SQM weighting is delegated to `darkroom.sites.annotate_sessions` (S2),
    shared with the JSON API's `GET /api/sessions`. That helper returns copies
    rather than mutating, so `rows` below is a local rebind and the caller's
    list is left alone. It names its fields `weight`/`weighted_hours` for the
    JSON API's benefit; the night dicts keep the short `w`/`wh` the embedded
    dashboard JS already reads. With `sites` empty/None or no home SQM,
    `annotate_sessions` is a no-op (weight=1.0, weighted_hours=h), so the
    aggregate is unchanged for callers/fixtures that don't pass `sites`.
    """
    rows = annotate_sessions(rows, sites or [])
    # One backend for the whole page: the matchers hit it several times per
    # session, and LocalBackend would open a SQLite connection for each.
    cal_backend = MemoryCalibrationBackend(cal_rows) if cal_rows is not None else None
    guiding_by_session = (
        {g["session_id"]: g for g in guiding_rows} if guiding_rows is not None else None
    )

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
            total_wh += s["weighted_hours"]
            night = {
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
                "site": s["site"],
                "w": s["weight"],
                "wh": s["weighted_hours"],
            }
            if cal_backend is not None:
                night["cal"] = match_session_calibration(cal_backend, s)
            if guiding_by_session is not None:
                night["guiding"] = _guiding_summary(
                    guiding_by_session.get(s["session_id"])
                )
            nights.append(night)
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


def _session_calibration(conn, session: dict) -> dict:
    """The calibration match for one session, for the session detail page."""
    backend = MemoryCalibrationBackend(catalog_db.query_calibration_sets(conn))
    return match_session_calibration(backend, session)


def _session_guiding(conn, session: dict) -> dict | None:
    """One session's guiding row (F4) for the session detail page, or None.

    The raw row, plus `logs` decoded from the stored JSON array and the same
    presentation verdicts (`band`, `partial`, `spike`) the night chip gets —
    the panel shows more of it than the chip does (pixel scale, guide camera,
    guided seconds), so it isn't the compacted `_guiding_summary` shape.
    """
    rows = catalog_db.query_session_guiding(conn, session_id=session["session_id"])
    if not rows:
        return None
    row = dict(rows[0])
    rms = row.get("rms_total_arcsec")
    row["logs"] = _decode_logs(row)
    row["band"] = _guide_band(rms) if rms is not None else None
    row["partial"] = _is_partial_coverage(row.get("coverage"))
    row["spike"] = _is_spike_dominated(rms, row.get("p95_arcsec"))
    return row


def _date_diff(a: str | None, b: str | None) -> int | None:
    """Return |days between two ISO date strings|, or None if either is missing/unparseable."""
    if not a or not b:
        return None
    try:
        return abs((date_cls.fromisoformat(a) - date_cls.fromisoformat(b)).days)
    except ValueError:
        return None


def _is_unknown_ota(ota: str | None) -> bool:
    return ota in PLACEHOLDERS


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
    """Calibration Flat sets near this session's date, same camera (+ OTA if known).

    Not `catalog.find_flats`: the row's filter is exactly what's unknown here,
    so the hint deliberately spans every filter and is a memory jog, not the
    set `wbpp` would pick.
    """
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


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r["obs_date"] or "", reverse=True)


def _build_queue(
    all_rows: list[dict], flat_sets: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (unknown_filter_rows, suspicious_value_rows, missing_site_rows), each obs_date-desc.

    'unknown filter' = filter IS NULL or 'UnknownFilter' (never parsed).
    'suspicious value' = filter is set but isn't one of KNOWN_FILTERS (the
    panel-name-in-filter-column garbage rows). Every row also carries an
    `unknown_ota` badge flag and context hints (neighbour sessions, nearby
    flats) to jog the user's memory when fixing it inline.

    'missing site coordinates' = site_lat or site_lon IS NULL. Mostly
    pre-ASIAir (Canon) frames that never had GPS headers, but also catches
    ASIAir sessions the header parse missed — surfaced so coordinates can be
    added by hand via the session edit screen rather than assumed.

    Pure: `all_rows` is every session (`catalog_db.query_sessions`) and
    `flat_sets` every Flat calibration set; the caller owns the queries.
    """
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

    missing_site_rows = [
        dict(row) for row in all_rows
        if row["site_lat"] is None or row["site_lon"] is None
    ]
    return (
        _newest_first(unknown_rows),
        _newest_first(suspicious_rows),
        _newest_first(missing_site_rows),
    )


def _known_otas(conn) -> list[str]:
    """Distinct non-null, non-'Unknown' OTA values on record — for the fix form's select."""
    rows = conn.execute(
        "SELECT DISTINCT ota FROM sessions "
        "WHERE ota IS NOT NULL AND ota != '' AND ota != 'Unknown' ORDER BY ota"
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


def _group_rescan_proposals(proposals: list[dict]) -> list[dict]:
    """Group decoded rescan proposals by target, safe-tier first within each group.

    A 'delete' proposal for a session whose target the contract leaves as
    None is grouped under the literal 'Unknown' rather than dropped, so it
    never silently disappears from the queue.
    """
    groups: dict[str, list[dict]] = {}
    for p in proposals:
        key = p.get("target") or "Unknown"
        groups.setdefault(key, []).append(p)

    result = []
    for tgt in sorted(groups):
        # Named "proposals", not "items" — a dict key called "items" would
        # shadow dict.items() and break `group.proposals` in the template
        # (Jinja's attribute lookup finds the bound method first).
        proposals = sorted(
            groups[tgt], key=lambda p: (p["tier"] != "safe", p.get("obs_date") or "")
        )
        result.append({
            "target": tgt,
            "proposals": proposals,
            "safe_count": sum(1 for p in proposals if p["tier"] == "safe"),
            "review_count": sum(1 for p in proposals if p["tier"] == "review"),
        })
    return result


def _rescan_context(conn) -> dict:
    pending = catalog_db.list_rescan_proposals(conn, status="pending")
    return {
        "groups": _group_rescan_proposals(pending),
        "total_count": len(pending),
        "safe_count": sum(1 for p in pending if p["tier"] == "safe"),
        "review_count": sum(1 for p in pending if p["tier"] == "review"),
    }


def _queue_context(conn) -> dict:
    all_rows = catalog_db.query_sessions(conn)
    flat_sets = catalog_db.query_calibration_sets(conn, frame_type="Flat")
    unknown_rows, suspicious_rows, missing_site_rows = _build_queue(all_rows, flat_sets)
    # One target entry per session (repeats expected): _target_suggestions
    # and the manual merge form's dropdown both want per-target counts.
    all_targets = [r["target"] for r in all_rows if r["target"] is not None]
    return {
        "unknown_rows": unknown_rows,
        "suspicious_rows": suspicious_rows,
        "missing_site_rows": missing_site_rows,
        "total_count": len(unknown_rows) + len(suspicious_rows),
        "known_filters": KNOWN_FILTERS,
        "known_otas": _known_otas(conn),
        "pending_renames_count": len(catalog_db.list_pending_renames(conn)),
        "target_suggestions": _target_suggestions(all_targets),
        "target_counts": sorted(Counter(all_targets).items()),
    }


def _form_str(form_data, key: str) -> str:
    """A form field as a stripped string ('' when absent or not a text field)."""
    raw = form_data.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def build_ui_router(db_path: Path, ui_password_hash: str) -> APIRouter:
    """Build the Jinja2 UI router, bound to the DB + UI password hash.

    Two routers: `public` carries /login and /logout; everything else sits on
    `protected`, whose router-level dependency raises `LoginRequired` (see
    `login_redirect`) so no handler has to check the cookie itself.
    """
    db_path = Path(db_path)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    def _get_conn():
        conn = catalog_db.open_db(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _require_login(
        request: Request, darkroom_token: str | None = Cookie(default=None)
    ) -> None:
        if not auth.verify_cookie(ui_password_hash, darkroom_token):
            raise LoginRequired(request.url.path)

    public = APIRouter()
    protected = APIRouter(dependencies=[Depends(_require_login)])

    def _render(request: Request, name: str, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def _render_login(request: Request, next_: str, error: str | None, status_code: int = 200):
        return _render(
            request, "login.html", {"error": error, "next": _safe_next(next_)}, status_code
        )

    def _render_queue(request: Request, conn, *, error=None, success=None, status_code=200):
        ctx = _queue_context(conn)
        ctx["error"] = error
        ctx["success"] = success
        return _render(request, "queue.html", ctx, status_code)

    def _render_rescan(request: Request, conn, *, error=None, success=None, status_code=200):
        ctx = _rescan_context(conn)
        ctx["error"] = error
        ctx["success"] = success
        return _render(request, "rescan.html", ctx, status_code)

    def _render_session(request: Request, conn, session: dict, *, error=None, status_code=200):
        return _render(
            request,
            "session.html",
            {
                "session": session,
                "processed_states": PROCESSED_STATES,
                "cal": _session_calibration(conn, session),
                "guiding": _session_guiding(conn, session),
                "error": error,
            },
            status_code,
        )

    def _session_or_404(conn, session_id: str) -> dict:
        rows = catalog_db.query_sessions(conn, session_id=session_id)
        if not rows:
            raise HTTPException(status_code=404, detail="session not found")
        return rows[0]

    @public.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return _render_login(request, next, None)

    @public.post("/login")
    def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
        ip = _client_ip(request)
        if _throttled(ip):
            return _render_login(
                request, next, "Too many attempts — try again in a minute", 429
            )
        if not auth.verify_password(password, ui_password_hash):
            _record_failure(ip)
            return _render_login(request, next, "Invalid password", 400)
        resp = RedirectResponse(_safe_next(next), status_code=303)
        set_session_cookie(resp, ui_password_hash)
        return resp

    @public.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @protected.get("/", response_class=HTMLResponse)
    def index(request: Request, conn=Depends(_get_conn)):
        rows = catalog_db.query_sessions(conn)
        sites = catalog_db.list_sites(conn)
        return _render(
            request,
            "index.html",
            {"data": _build_aggregate(rows, sites), "processed_states": PROCESSED_STATES},
        )

    @protected.get("/targets/{target}", response_class=HTMLResponse)
    def target_detail(request: Request, target: str, conn=Depends(_get_conn)):
        rows = catalog_db.query_sessions(conn, target=target)
        if not rows:
            raise HTTPException(status_code=404, detail="target not found")
        sites = catalog_db.list_sites(conn)
        cal_rows = catalog_db.query_calibration_sets(conn)
        guiding_rows = catalog_db.query_session_guiding(conn)

        aggregate = _build_aggregate(rows, sites, cal_rows, guiding_rows)
        # query_sessions normalises `target` case/spacing-insensitively, so
        # aggregate[0]["target"] is the canonical form even if the URL segment
        # wasn't (e.g. "m81" -> "M 81") — scope strictly to that one entry.
        return _render(
            request,
            "target.html",
            {
                "data": aggregate,
                "target": aggregate[0]["target"],
                "processed_states": PROCESSED_STATES,
            },
        )

    @protected.get("/queue", response_class=HTMLResponse)
    def queue(request: Request, conn=Depends(_get_conn)):
        return _render_queue(request, conn)

    @protected.post("/queue/{session_id}/fix")
    async def queue_fix(request: Request, session_id: str, conn=Depends(_get_conn)):
        form_data = await request.form()
        filt = form_data.get("filter")
        ota = _form_str(form_data, "ota")

        if filt not in KNOWN_FILTERS:
            return _render_queue(
                request, conn, status_code=400,
                error=f"{session_id}: filter must be one of {', '.join(KNOWN_FILTERS)}",
            )

        changed: dict[str, Any] = {"filter": filt}
        if ota:
            changed["ota"] = ota

        try:
            updated = catalog_db.update_session_fields(conn, session_id, **changed)
        except ValueError as e:
            return _render_queue(request, conn, error=f"{session_id}: {e}", status_code=400)
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")

        return RedirectResponse("/queue", status_code=303)

    @protected.post("/queue/{session_id}/fix-site")
    async def queue_fix_site(request: Request, session_id: str, conn=Depends(_get_conn)):
        form_data = await request.form()
        try:
            lat = float(form_data.get("site_lat"))
            lon = float(form_data.get("site_lon"))
        except (TypeError, ValueError):
            return _render_queue(
                request, conn, status_code=400,
                error=f"{session_id}: latitude/longitude must be numeric",
            )

        updated = catalog_db.update_session_fields(
            conn, session_id, site_lat=lat, site_lon=lon
        )
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")

        return RedirectResponse("/queue", status_code=303)

    @protected.post("/queue/targets/rename")
    async def queue_targets_rename(request: Request, conn=Depends(_get_conn)):
        form_data = await request.form()
        old_target = _form_str(form_data, "old_target")
        new_target = _form_str(form_data, "new_target")

        try:
            result = catalog_db.rename_target(conn, old_target, new_target)
        except ValueError as e:
            return _render_queue(request, conn, error=str(e), status_code=400)

        if result["total"] == 0:
            return _render_queue(
                request, conn, status_code=404,
                error=f"No sessions found for target {old_target!r}",
            )

        success = (
            f"renamed {result['renamed']} session"
            f"{'' if result['renamed'] == 1 else 's'} of {old_target} → {new_target}"
            if result["renamed"] else None
        )
        error = None
        status_code = 200
        if result["errors"]:
            details = "; ".join(
                f"{e['session_id']}: {e['error']}" for e in result["errors"]
            )
            error = (
                f"{len(result['errors'])} session"
                f"{'' if len(result['errors']) == 1 else 's'} failed to merge: {details}"
            )
            status_code = 200 if result["renamed"] else 400
        return _render_queue(
            request, conn, error=error, success=success, status_code=status_code
        )

    @protected.post("/sessions/{session_id}/state")
    def set_state(session_id: str, state: str = Form(...), next: str = Form("/")):
        from darkroom import cataloger

        try:
            updated = cataloger.set_processed_state(db_path, session_id, state=state)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")
        return RedirectResponse(_safe_next(next), status_code=303)

    @protected.get("/sessions/{session_id}", response_class=HTMLResponse)
    def edit_form(
        request: Request,
        session_id: str,
        error: str | None = None,
        conn=Depends(_get_conn),
    ):
        return _render_session(request, conn, _session_or_404(conn, session_id), error=error)

    @protected.post("/sessions/{session_id}")
    async def edit_submit(request: Request, session_id: str, conn=Depends(_get_conn)):
        form_data = await request.form()
        current = _session_or_404(conn, session_id)

        # Convert form strings ('' -> None, numeric strings -> int/float) and
        # only pass fields that actually changed vs the current row, so an
        # untouched identity field never triggers a spurious session_id rename.
        changed: dict[str, Any] = {}
        for key in _EDIT_FIELDS:
            if key not in form_data:
                continue
            raw = _form_str(form_data, key)
            value: Any = raw if raw != "" else None
            if value is not None and key in _NUMERIC_FIELDS:
                try:
                    value = _NUMERIC_FIELDS[key](value)
                except ValueError:
                    return _render_session(
                        request, conn, current, status_code=400,
                        error=f"Invalid numeric value for {key!r}: {raw!r}",
                    )
            if current.get(key) != value:
                changed[key] = value

        if changed:
            try:
                catalog_db.update_session_fields(conn, session_id, **changed)
            except ValueError as e:
                return _render_session(request, conn, current, error=str(e), status_code=400)

        # An identity edit renames session_id on the same row — follow it.
        new_row = conn.execute(
            "SELECT session_id FROM sessions WHERE id = ?", (current["id"],)
        ).fetchone()
        new_session_id = new_row["session_id"] if new_row else session_id
        return RedirectResponse(f"/sessions/{new_session_id}", status_code=303)

    @protected.post("/sessions/{session_id}/delete")
    def delete_submit(session_id: str, conn=Depends(_get_conn)):
        target = _session_or_404(conn, session_id)["target"]
        catalog_db.delete_session(conn, session_id)
        if catalog_db.query_sessions(conn, target=target):
            return RedirectResponse(
                f"/targets/{urllib.parse.quote(target)}", status_code=303
            )
        return RedirectResponse("/", status_code=303)

    @protected.get("/rescan", response_class=HTMLResponse)
    def rescan_review(request: Request, conn=Depends(_get_conn)):
        return _render_rescan(request, conn)

    @protected.post("/rescan/{proposal_id}/apply")
    def rescan_apply(request: Request, proposal_id: int, conn=Depends(_get_conn)):
        try:
            applied = catalog_db.apply_pending_rescan_proposal(conn, db_path, proposal_id)
        except ValueError as e:
            return _render_rescan(request, conn, error=str(e), status_code=400)
        if applied is None:
            raise HTTPException(status_code=404, detail="pending rescan proposal not found")
        return RedirectResponse("/rescan", status_code=303)

    @protected.post("/rescan/{proposal_id}/dismiss")
    def rescan_dismiss(proposal_id: int, conn=Depends(_get_conn)):
        dismissed = catalog_db.resolve_rescan_proposal(conn, proposal_id, "dismissed")
        if not dismissed:
            raise HTTPException(status_code=404, detail="pending rescan proposal not found")
        return RedirectResponse("/rescan", status_code=303)

    @protected.post("/rescan/apply-all-safe")
    def rescan_apply_all_safe(request: Request, conn=Depends(_get_conn)):
        pending = catalog_db.list_rescan_proposals(conn, status="pending")
        applied = 0
        errors: list[str] = []
        for p in pending:
            if p["tier"] != "safe":
                continue
            try:
                catalog_db.apply_pending_rescan_proposal(conn, db_path, p["id"])
            except ValueError as e:
                errors.append(str(e))
                continue
            applied += 1

        return _render_rescan(
            request, conn,
            success=(
                f"applied {applied} safe proposal{'' if applied == 1 else 's'}"
                if applied else None
            ),
            error="; ".join(errors) if errors else None,
            status_code=400 if errors and not applied else 200,
        )

    public.include_router(protected)
    return public
