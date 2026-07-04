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
    """True iff the session's structured processed_state is 'processed'."""
    return row.get("processed_state") == "processed"


def needs_processing(row: dict) -> bool:
    """True iff the session is still a candidate for processing.

    Only 'unprocessed' sessions are candidates. 'processed' and 'skipped' are
    both *settled* states: a skipped night was deliberately set aside (bad
    tracking, clouds, …), so the picker treats it like a processed one — not
    pre-checked, not counted as backlog — while still listing it so the choice
    can be overridden. Anything without an explicit state defaults to a
    candidate.
    """
    return (row.get("processed_state") or "unprocessed") == "unprocessed"


def summarize_targets(rows: list[dict]) -> list[dict]:
    """One summary dict per target, sorted by latest_date descending.

    Each summary: target, night_count (distinct obs_dates), unprocessed_count
    (distinct obs_dates with at least one row still needing processing —
    skipped nights don't count), total_hours (sum of total_integration_sec/
    3600, None-safe), latest_date.
    """
    by_target: dict[str, list[dict]] = {}
    for row in rows:
        by_target.setdefault(row["target"], []).append(row)

    summaries = []
    for target, trows in by_target.items():
        dates = {r["obs_date"] for r in trows}
        unprocessed_dates = {r["obs_date"] for r in trows if needs_processing(r)}
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
    """e.g. '3 nights · 15.9h · 2 unprocessed', or '... · nothing to process'."""
    base = f"{summary['night_count']} nights · {summary['total_hours']:.1f}h"
    if summary["unprocessed_count"] == 0:
        return f"{base} · nothing to process"
    return f"{base} · {summary['unprocessed_count']} unprocessed"


def group_nights(rows: list[dict]) -> list[dict]:
    """Group one target's rows by obs_date, newest first.

    Each night dict: obs_date, filters (comma-joined, filter or "NoFilter"),
    frame_count (sum, None-safe), total_hours, processed (True iff every row
    in the night is processed), candidate (True iff any row still needs
    processing — drives the pre-check; a night that is entirely
    processed/skipped is not a candidate), rows (the original row dicts).
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
            "candidate": any(needs_processing(r) for r in drows),
            "rows": drows,
        })
    nights.sort(key=lambda n: n["obs_date"], reverse=True)
    return nights


def night_label(night: dict) -> str:
    """e.g. '2026-06-21  L-Pro  132f  6.6h'.

    Appends '  [processed ✓]' when every row is processed, or '  [skipped]'
    when the night is settled (nothing left to process) but not fully
    processed — i.e. one or more rows were deliberately skipped. A night with
    work still to do gets no tag.
    """
    label = (
        f"{night['obs_date']}  {night['filters']}  "
        f"{night['frame_count']}f  {night['total_hours']:.1f}h"
    )
    if night["processed"]:
        label += "  [processed ✓]"
    elif not night.get("candidate", True):
        label += "  [skipped]"
    return label


# ── interactive flow ─────────────────────────────────────────────────────────

def picker_style():
    """Shared questionary Style for all wbpp prompts.

    The autocomplete dropdown is unreadable on a dark terminal by default:
    prompt_toolkit's completion-menu background is a hardcoded light gray
    (bg:#bbbbbb), and questionary's own WordCompleter stamps every candidate
    with "class:answer" (bold orange/yellow) — which, being the rightmost
    class in the compound style string prompt_toolkit builds per item, wins
    over any plain completion-menu/completion-menu.completion override. So a
    bare override of those two classes changes the box color but the text
    stays orange-on-whatever. To actually reclaim the text color we have to
    match the *combined* class set ("completion-menu.completion answer") that
    prompt_toolkit assembles for each candidate. The highlighted-row variant
    additionally picks up a bare "selected" class (rightmost of all, so it
    wins last) that prompt_toolkit's own base style defines as plain
    "reverse" — that flips our explicit fg/bg back to the default look, so
    every current-row rule below also has to repeat "noreverse" and the
    three progressively-larger combos ("...current", "...current answer",
    "...current answer selected") all need to be pinned, since whichever
    combo is matched last as prompt_toolkit walks the style string left to
    right is the one that sticks.
    """
    import questionary  # lazy: keep this module importable without a TTY/dep

    current = "bg:#00afaf fg:#000000 bold noreverse"
    return questionary.Style([
        ("completion-menu", "bg:#262626 fg:#d7d7d7"),
        ("completion-menu.completion", "bg:#262626 fg:#d7d7d7"),
        ("completion-menu.completion answer", "bg:#262626 fg:#d7d7d7"),
        ("completion-menu.completion.current", current),
        ("completion-menu.completion.current answer", current),
        ("completion-menu.completion.current answer selected", current),
        ("completion-menu.meta.completion", "bg:#262626 fg:#9e9e9e"),
        ("completion-menu.meta.completion.current", "bg:#00afaf fg:#000000 bold"),
    ])


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
        style=picker_style(),
    ).ask()
    if not target:
        return None

    target_rows = [r for r in rows if r["target"] == target]
    nights = group_nights(target_rows)
    choices = [
        questionary.Choice(
            title=night_label(night), value=night["obs_date"], checked=night["candidate"]
        )
        for night in nights
    ]
    selected_dates = questionary.checkbox(
        f"Select nights for {target}:", choices=choices, style=picker_style()
    ).ask()
    if not selected_dates:
        return None

    wanted = set(selected_dates)
    result = []
    for night in nights:
        if night["obs_date"] in wanted:
            result.extend(night["rows"])
    return result
