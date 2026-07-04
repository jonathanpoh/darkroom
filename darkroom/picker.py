"""darkroom.picker — interactive session selection for `darkroom wbpp`.

Pure helpers here are stdlib-only so importing this module never requires a
TTY or the `questionary` dependency. `questionary` is imported lazily inside
`pick_sessions`, the only function that actually drives a prompt.
"""
from __future__ import annotations

from pathlib import Path

from darkroom.catalog import query_all_sessions


# ── pure helpers ─────────────────────────────────────────────────────────────

def is_processed(row: dict) -> bool:
    """True iff processed_status is set to a non-blank value."""
    status = row.get("processed_status")
    return status is not None and status.strip() != ""


def summarize_targets(rows: list[dict]) -> list[dict]:
    """One summary dict per target, sorted by latest_date descending.

    Each summary: target, night_count (distinct obs_dates), unprocessed_count
    (distinct obs_dates with at least one unprocessed row), total_hours
    (sum of total_integration_sec/3600, None-safe), latest_date.
    """
    by_target: dict[str, list[dict]] = {}
    for row in rows:
        by_target.setdefault(row["target"], []).append(row)

    summaries = []
    for target, trows in by_target.items():
        dates = {r["obs_date"] for r in trows}
        unprocessed_dates = {r["obs_date"] for r in trows if not is_processed(r)}
        total_hours = sum(r["total_integration_sec"] or 0 for r in trows) / 3600
        summaries.append({
            "target": target,
            "night_count": len(dates),
            "unprocessed_count": len(unprocessed_dates),
            "total_hours": total_hours,
            "latest_date": max(dates),
        })
    summaries.sort(key=lambda s: s["latest_date"], reverse=True)
    return summaries


def target_meta(summary: dict) -> str:
    """e.g. '3 nights · 15.9h · 2 unprocessed', or '... · all processed'."""
    base = f"{summary['night_count']} nights · {summary['total_hours']:.1f}h"
    if summary["unprocessed_count"] == 0:
        return f"{base} · all processed"
    return f"{base} · {summary['unprocessed_count']} unprocessed"


def group_nights(rows: list[dict]) -> list[dict]:
    """Group one target's rows by obs_date, newest first.

    Each night dict: obs_date, filters (comma-joined, filter or "NoFilter"),
    frame_count (sum, None-safe), total_hours, processed (True iff every row
    in the night is processed), rows (the original row dicts).
    """
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(row["obs_date"], []).append(row)

    nights = []
    for obs_date, drows in by_date.items():
        filters = ", ".join(r["filter"] or "NoFilter" for r in drows)
        frame_count = sum(r["frame_count"] or 0 for r in drows)
        total_hours = sum(r["total_integration_sec"] or 0 for r in drows) / 3600
        nights.append({
            "obs_date": obs_date,
            "filters": filters,
            "frame_count": frame_count,
            "total_hours": total_hours,
            "processed": all(is_processed(r) for r in drows),
            "rows": drows,
        })
    nights.sort(key=lambda n: n["obs_date"], reverse=True)
    return nights


def night_label(night: dict) -> str:
    """e.g. '2026-06-21  L-Pro  132f  6.6h', with '  [processed ✓]' appended."""
    label = (
        f"{night['obs_date']}  {night['filters']}  "
        f"{night['frame_count']}f  {night['total_hours']:.1f}h"
    )
    if night["processed"]:
        label += "  [processed ✓]"
    return label


# ── interactive flow ─────────────────────────────────────────────────────────

def pick_sessions(catalog: Path) -> list[dict] | None:
    """Interactively pick a target then one or more nights. None if cancelled.

    Returns the concatenated rows of the selected nights (night order
    preserved), or None if there is nothing to pick or the user backs out.
    """
    import questionary  # lazy: keep this module importable without a TTY/dep

    rows = query_all_sessions(catalog)
    if not rows:
        print("No sessions found in catalog.")
        return None

    summaries = summarize_targets(rows)
    names = [s["target"] for s in summaries]
    meta = {s["target"]: target_meta(s) for s in summaries}

    def _validate(text: str) -> bool | str:
        if text in names:
            return True
        return "Pick a target from the list (Tab to autocomplete)."

    target = questionary.autocomplete(
        "Target:",
        choices=names,
        meta_information=meta,
        match_middle=True,
        ignore_case=True,
        validate=_validate,
    ).ask()
    if not target:
        return None

    target_rows = [r for r in rows if r["target"] == target]
    nights = group_nights(target_rows)
    choices = [
        questionary.Choice(
            title=night_label(night), value=night["obs_date"], checked=not night["processed"]
        )
        for night in nights
    ]
    selected_dates = questionary.checkbox(
        f"Select nights for {target}:", choices=choices
    ).ask()
    if not selected_dates:
        return None

    wanted = set(selected_dates)
    result = []
    for night in nights:
        if night["obs_date"] in wanted:
            result.extend(night["rows"])
    return result
