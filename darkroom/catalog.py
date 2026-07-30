# darkroom/catalog.py
"""darkroom.catalog — client-side calibration matching over a CatalogBackend.

This module is the astropy-free *matching* layer: it holds only the logic
that can't be expressed as a server-side equality filter (date proximity,
exposure tolerance, null-filter matching) and consumes rows fetched through
a darkroom.catalog_client.CatalogBackend (LocalBackend or HttpBackend), so
the same matching logic works whether the catalog lives in a local SQLite
file or behind the future webapi server (W9). Deliberately import-light:
only stdlib + darkroom.catalog_client (itself astropy/httpx-free at import
time) at module load.
"""
from __future__ import annotations

from datetime import date, timedelta

from darkroom.catalog_client import CatalogBackend


def query_all_sessions(backend: CatalogBackend) -> list[dict]:
    """Return all sessions ordered by target then obs_date."""
    return sorted(backend.query_sessions(), key=lambda r: (r["target"], r["obs_date"]))


DEFAULT_DARK_TEMP_TOLERANCE = 3.0
# Shared by find_flats and `wbpp --flat-window`, so the web UI's calibration
# indicator can't silently predict a different window than the prep run.
DEFAULT_FLAT_WINDOW_DAYS = 3


def dark_temp_sort_key(set_temp: float | None, session_temp: float) -> tuple[int, float]:
    """Rank a dark set against a session's sensor temperature: nearest first.

    Sets with no recorded temperature sort last — they match anything, but only
    as a fallback once every temperatured set has been considered.
    """
    if set_temp is None:
        return (1, 0.0)
    return (0, abs(set_temp - session_temp))


def dark_temp_ties(rows: list[dict], temperature_c: float) -> list[dict]:
    """Rows tied for nearest temperature at *different* set-points.

    Returns [] unless the pick is genuinely ambiguous — i.e. two or more distinct
    temperatures sit exactly the same distance from the session. A session at
    22.5C with masters on the 20C/25C rungs of an uncooled ladder is the real
    case: neither is more correct than the other, so the caller warns rather
    than silently taking row order. Sets at the same temperature are not a tie;
    either is equally right.
    """
    temped = [r for r in rows if r["temperature_c"] is not None]
    if not temped:
        return []
    best = min(abs(r["temperature_c"] - temperature_c) for r in temped)
    tied = [r for r in temped if abs(r["temperature_c"] - temperature_c) == best]
    if len({r["temperature_c"] for r in tied}) < 2:
        return []
    return tied


def find_darks(
    backend: CatalogBackend, *, camera: str, gain: int, exposure_sec: float,
    temperature_c: float | None = None,
    temp_tolerance: float = DEFAULT_DARK_TEMP_TOLERANCE,
) -> list[dict]:
    """Return Dark sets matching camera+gain+exposure, masters first.

    When `temperature_c` is given, sets further than `temp_tolerance` degrees
    from it are dropped and the rest are ranked nearest-first within the
    masters/raws split (see dark_temp_sort_key). Passing None skips temperature
    matching entirely — the pre-B11 behaviour, kept for callers that only want
    to know what darks exist at a given gain/exposure.

    Nearest-within-tolerance is applied to *every* camera, not just uncooled
    ones. Exact matching looks right for a cooled camera with deliberate
    set-points, but session `temperature_c` is the raw CCD-TEMP of the session's
    *first* frame (cataloger._read_header), taken while the sensor may still be
    settling, whereas calibration sets round to the nearest degree
    (scanner.py). On the live catalog that leaves 13 of 111 ZWOASI585MCPro
    sessions on values like -19.5, -16.5 or -15.0 that no master would match
    exactly. One rule with a tolerance handles both regimes and needs no
    per-camera cooled/uncooled registry, which the codebase does not have.
    """
    rows = backend.query_calibration_sets(
        frame_type="Dark", camera=camera, gain=gain, exposure_sec=exposure_sec
    )
    if temperature_c is None:
        return rows
    rows = [
        r for r in rows
        if r["temperature_c"] is None
        or abs(r["temperature_c"] - temperature_c) <= temp_tolerance
    ]
    # Masters stay ahead of raws (the query's contract, which _build_night's
    # partition-then-fallback relies on); temperature only orders within each.
    rows.sort(
        key=lambda r: (
            not r.get("is_master"),
            dark_temp_sort_key(r["temperature_c"], temperature_c),
        )
    )
    return rows


def nearest_dark(
    backend: CatalogBackend, *, camera: str, gain: int, exposure_sec: float,
    temperature_c: float,
) -> dict | None:
    """Nearest dark set at this gain/exposure *ignoring* the temperature window.

    The near-miss case reads very differently from having no darks at all, and
    the fix differs too (raise the tolerance vs. go and shoot darks). Callers
    phrase it for their own surface — prep._no_darks_note names the CLI flag,
    the web UI's indicator doesn't. Returns None when nothing at this
    gain/exposure carries a temperature.
    """
    rows = [
        r for r in find_darks(
            backend, camera=camera, gain=gain, exposure_sec=exposure_sec,
        )
        if r["temperature_c"] is not None
    ]
    if not rows:
        return None
    return min(rows, key=lambda r: abs(r["temperature_c"] - temperature_c))


def find_bias(backend: CatalogBackend, *, camera: str, gain: int) -> list[dict]:
    """Return Bias calibration sets matching camera+gain, masters first."""
    return backend.query_calibration_sets(frame_type="Bias", camera=camera, gain=gain)


def flat_offset_days(capture_date: str, obs_date: str) -> int:
    """Signed days from a session's night to a flat set's capture date.

    0 = shot the same evening (before midnight), +1 = the following morning.
    Both are "this run"; see flat_sort_key.
    """
    return (date.fromisoformat(capture_date) - date.fromisoformat(obs_date)).days


def flat_sort_key(capture_date: str, obs_date: str) -> tuple[int, int, int]:
    """Rank a flat set for a session night — the flat-morning rule, directional.

    Flats belonging to the session's own run come first: offset 0 (shot that
    evening, e.g. either side of a mid-session filter change) or +1 (the usual
    case, shot the morning after). Within that group the morning-after set wins,
    since that is the habitual workflow.

    Everything else in the window is a fallback — flats from a different
    occasion entirely — ranked by proximity, preferring later over earlier on a
    tie for the same reason.

    Plain proximity used to rank these, which made ±1 a tie broken by whatever
    order the backend happened to return: a session on night N would routinely
    be handed the *previous* night's flats over the ones shot the morning after.
    The two sets can differ a lot (sky brightness changes the flat exposure), so
    this is not cosmetic. `infer_flat_filter` and `find_flat_darks` already
    encoded the same directional 0..+1 rule; this brings find_flats in line.
    """
    delta = flat_offset_days(capture_date, obs_date)
    in_run = 0 if 0 <= delta <= 1 else 1
    # Third element prefers the later date; second is inert for in-run rows so
    # the tuple stays comparable across both groups.
    return (in_run, abs(delta) if in_run else 0, -delta)


def flat_offset_label(capture_date: str, obs_date: str) -> str:
    """e.g. '+1 day (morning after)', 'same evening', '-2 days'.

    Makes the ranking checkable at a glance: the whole point of the flat-morning
    rule is that ±1 day are *not* equivalent, which a bare date doesn't show.
    """
    delta = flat_offset_days(capture_date, obs_date)
    if delta == 0:
        return "same evening"
    if delta == 1:
        return "+1 day (morning after)"
    return f"{delta:+d} days"


def find_flats(
    backend: CatalogBackend, *, camera: str, ota: str, filter_: str | None,
    obs_date: str, window_days: int = DEFAULT_FLAT_WINDOW_DAYS,
) -> list[dict]:
    """Return Flat calibration sets within ±window_days, best match first.

    Archived flats may have been taken on a different occasion than the session,
    so the window is date proximity (default ±3 days) rather than an exact date
    — but ordering within it follows the flat-morning rule (see flat_sort_key),
    not raw proximity.
    """
    d = date.fromisoformat(obs_date)
    lo = d - timedelta(days=window_days)
    hi = d + timedelta(days=window_days)
    rows = backend.query_calibration_sets(frame_type="Flat", camera=camera, ota=ota)
    rows = [r for r in rows if r["filter"] == filter_]
    # NULL capture_date never matches, same as the old SQL BETWEEN.
    rows = [r for r in rows if r["capture_date"] is not None]
    rows = [r for r in rows if lo <= date.fromisoformat(r["capture_date"]) <= hi]
    rows.sort(key=lambda r: flat_sort_key(r["capture_date"], obs_date))
    return rows


def find_flat_darks(
    backend: CatalogBackend, *, camera: str, flat_exposure_sec: float,
    flat_capture_date: str,
) -> list[dict]:
    """Return FlatDark sets matching camera + exposure (±10%) + date (flat_date or flat_date+1)."""
    lo = flat_exposure_sec * 0.9
    hi = flat_exposure_sec * 1.1
    d = date.fromisoformat(flat_capture_date)
    d1 = (d + timedelta(days=1)).isoformat()
    rows = backend.query_calibration_sets(frame_type="FlatDark", camera=camera)
    return [
        r for r in rows
        if lo <= r["exposure_sec"] <= hi and r["capture_date"] in (flat_capture_date, d1)
    ]


# ── per-session calibration summary (F3) ─────────────────────────────────────
#
# One shared answer to "what would `wbpp` find for this session?", so the web
# UI's indicator and the prep run can't disagree. Everything here is a thin
# orchestration of the find_* matchers above plus presentation — no new matching
# rules, and the defaults are wbpp's defaults.
#
# Two deliberate divergences from prep._build_night, both toward being *more*
# correct than it is today:
#
#   * Per session, not per night. _build_night takes dark params from
#     sessions[0] for the whole night (open bug B13); matching each session on
#     its own camera/gain/exposure/temperature is what B13 will make it do.
#   * No disk check. _build_night only uses a set whose folder_path exists on
#     disk; a caller with no archive mount (the webapi host has none) can't
#     check that, so this reports catalog-level truth only. A stale folder_path
#     can still leave the prep run with nothing.

CAL_OK = "ok"            # a set matched
CAL_MISSING = "missing"  # no match, but this camera does use this frame type
CAL_NA = "na"            # this camera has no sets of this type at all
CAL_UNKNOWN = "unknown"  # the session lacks the fields needed to match


def _temp(value: float | None) -> str:
    return "unknown temperature" if value is None else f"{value:g}C"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _cal_set(row: dict) -> dict:
    """The identifying fields of a matched set, JSON-safe for a template/JS."""
    return {
        key: row.get(key) for key in (
            "set_id", "frame_type", "camera", "ota", "filter", "gain",
            "exposure_sec", "temperature_c", "capture_date", "frame_count",
            "folder_path", "is_master",
        )
    }


def _cal(status: str, detail: str, *, label: str | None = None, sets=()) -> dict:
    return {
        "status": status,
        "label": label,
        "detail": detail,
        "sets": [_cal_set(r) for r in sets],
    }


def _camera_uses(backend: CatalogBackend, frame_type: str, camera: str) -> bool:
    """Does this camera have any set of this frame type at all?

    Distinguishes "missing" from "not part of this camera's workflow" —
    ZWOASI585MCPro needs no flat darks, Canon6D does — without a per-camera
    registry, which the codebase does not have. Data-driven, so it corrects
    itself the day a camera's first set of that type is ingested.
    """
    return bool(backend.query_calibration_sets(frame_type=frame_type, camera=camera))


def _match_darks(
    backend: CatalogBackend, session: dict, tolerance: float
) -> dict:
    camera = session.get("camera")
    gain = session.get("gain")
    exposure_sec = session.get("exposure_sec")
    # gain 0 is a legitimate value — check for None, not falsiness.
    if not camera or gain is None or exposure_sec is None:
        return _cal(CAL_UNKNOWN, "session has no camera, gain or exposure to match on")

    session_temp = session.get("temperature_c")
    rows = find_darks(
        backend, camera=camera, gain=gain, exposure_sec=exposure_sec,
        temperature_c=session_temp, temp_tolerance=tolerance,
    )
    params = f"{exposure_sec:g}s gain{gain}"

    masters = [r for r in rows if r.get("is_master")]
    if masters:
        best = masters[0]
        bits = [f"master dark, {params}, {_temp(best['temperature_c'])}"]
        if session_temp is None:
            bits.append("session has no recorded temperature — wbpp takes the first master")
        elif best["temperature_c"] is not None:
            delta = abs(best["temperature_c"] - session_temp)
            bits.append(f"{delta:g}C from the session's {session_temp:g}C")
            tied = dark_temp_ties(masters, session_temp)
            if tied:
                temps = ", ".join(_temp(r["temperature_c"]) for r in tied)
                bits.append(f"ambiguous — masters at {temps} are equally near")
        else:
            # A set with no recorded temperature matches anything, but only once
            # every temperatured set has been ruled out (dark_temp_sort_key).
            # This is the common Canon6D case — its masters carry no CCD-TEMP.
            bits.append(
                f"set has no recorded temperature — accepted as a fallback for"
                f" the session's {session_temp:g}C"
            )
        return _cal(
            CAL_OK, "; ".join(bits),
            label=f"master · {_temp(best['temperature_c'])}", sets=[best],
        )

    if rows:
        # No master at this temperature — wbpp falls back to raw subs, and
        # combines every matching set rather than choosing one.
        return _cal(
            CAL_OK,
            f"no master — {_plural(len(rows), 'raw set')} at {params} would be combined",
            label=f"raw · {_plural(len(rows), 'set')}", sets=rows,
        )

    if not _camera_uses(backend, "Dark", camera):
        return _cal(CAL_NA, f"no dark sets for {camera} in the catalog")

    if session_temp is not None:
        near = nearest_dark(
            backend, camera=camera, gain=gain, exposure_sec=exposure_sec,
            temperature_c=session_temp,
        )
        if near is not None:
            delta = abs(near["temperature_c"] - session_temp)
            kind = "master" if near.get("is_master") else "raw set"
            return _cal(
                CAL_MISSING,
                f"no darks within ±{tolerance:g}C of {session_temp:g}C — nearest"
                f" {kind} is {_temp(near['temperature_c'])}, {delta:g}C away",
            )
    return _cal(CAL_MISSING, f"no darks at {params}")


def _match_flats(
    backend: CatalogBackend, session: dict, window_days: int
) -> tuple[dict, dict | None]:
    """Returns (summary, chosen row) — flat darks are matched off the chosen flat."""
    camera = session.get("camera")
    ota = session.get("ota")
    obs_date = session.get("obs_date")
    # ota=None would reach query_calibration_sets as "no OTA constraint" and
    # quietly match flats from every scope, so an unknown OTA is unmatchable
    # rather than a wildcard. A None *filter* is different: find_flats compares
    # it client-side, where None correctly means "filter IS NULL".
    if not camera or not ota or ota == "Unknown" or not obs_date:
        return _cal(CAL_UNKNOWN, "session has no camera, OTA or date to match on"), None
    try:
        date.fromisoformat(obs_date)
    except ValueError:
        return _cal(CAL_UNKNOWN, f"session date {obs_date!r} is not a valid date"), None

    filter_name = session.get("filter") or "NoFilter"
    rows = find_flats(
        backend, camera=camera, ota=ota, filter_=session.get("filter"),
        obs_date=obs_date, window_days=window_days,
    )
    if rows:
        best = rows[0]
        bits = [
            f"{filter_name} flats, {best['capture_date']}"
            f" ({flat_offset_label(best['capture_date'], obs_date)})",
            f"{best.get('frame_count') or '?'} frames",
        ]
        if best.get("exposure_sec") is not None:
            bits.append(f"{best['exposure_sec']:g}s")
        if len(rows) > 1:
            bits.append(
                f"{len(rows) - 1} other set(s) in the ±{window_days}d window rank lower"
            )
        return _cal(
            CAL_OK, "; ".join(bits),
            label=f"{best['capture_date']}", sets=[best],
        ), best

    if not _camera_uses(backend, "Flat", camera):
        return _cal(CAL_NA, f"no flat sets for {camera} in the catalog"), None
    return _cal(
        CAL_MISSING,
        f"no {filter_name} flats for {ota} within ±{window_days} days of {obs_date}",
    ), None


def _match_flat_darks(
    backend: CatalogBackend, session: dict, chosen_flat: dict | None
) -> dict:
    camera = session.get("camera")
    if not camera:
        return _cal(CAL_UNKNOWN, "session has no camera to match on")
    if chosen_flat is None:
        return _cal(CAL_UNKNOWN, "no matched flat to take an exposure and date from")
    if chosen_flat.get("exposure_sec") is None or not chosen_flat.get("capture_date"):
        return _cal(CAL_UNKNOWN, "the matched flat set has no exposure or capture date")

    rows = find_flat_darks(
        backend, camera=camera,
        flat_exposure_sec=chosen_flat["exposure_sec"],
        flat_capture_date=chosen_flat["capture_date"],
    )
    if rows:
        dates = ", ".join(sorted({r["capture_date"] for r in rows}))
        return _cal(
            CAL_OK,
            f"{_plural(len(rows), 'set')} matching the flats'"
            f" {chosen_flat['exposure_sec']:g}s (±10%) on {dates}",
            label=_plural(len(rows), "set"), sets=rows,
        )

    if not _camera_uses(backend, "FlatDark", camera):
        return _cal(CAL_NA, f"{camera} has no flat darks in the catalog — not used")
    return _cal(
        CAL_MISSING,
        f"no flat darks near {chosen_flat['exposure_sec']:g}s"
        f" on {chosen_flat['capture_date']} (or the morning after)",
    )


def match_session_calibration(
    backend: CatalogBackend,
    session: dict,
    *,
    flat_window: int = DEFAULT_FLAT_WINDOW_DAYS,
    dark_temp_tolerance: float = DEFAULT_DARK_TEMP_TOLERANCE,
) -> dict:
    """What darks/flats/flat-darks `wbpp` would find for one session.

    Returns {"darks": …, "flats": …, "flat_darks": …}, each a dict of
    {status, label, detail, sets} where status is one of CAL_OK / CAL_MISSING /
    CAL_NA / CAL_UNKNOWN. Defaults match `wbpp`'s, so the answer predicts a prep
    run. See the module-section comment above for the two divergences from
    prep._build_night (per-session params, and no on-disk check).

    `backend` is called with equality filters only, so a
    catalog_client.MemoryCalibrationBackend loaded once is the right thing to
    pass when matching many sessions at a time.
    """
    darks = _match_darks(backend, session, dark_temp_tolerance)
    flats, chosen_flat = _match_flats(backend, session, flat_window)
    flat_darks = _match_flat_darks(backend, session, chosen_flat)
    return {"darks": darks, "flats": flats, "flat_darks": flat_darks}
