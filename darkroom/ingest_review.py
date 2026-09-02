"""darkroom.ingest_review — interactive confirmation pass over an ingest manifest.

`darkroom ingest review MANIFEST` walks every session and calibration group in a
scanned manifest and lets the values parsed out of ASIAir filenames/headers be
confirmed or corrected before anything is copied: **filter**, **target** (ASIAir
folder names are whatever was typed into the tablet) and **OTA + camera** (OTA is
inferred from FOCALLEN, which is wrong for optics `parse_ota` doesn't know).

Corrections are written back to the manifest — `ingest commit` stays entirely
non-interactive, because it runs from a Carbon Copy Cloner postflight with no TTY.

Structured like `darkroom.picker`: the helpers here are stdlib-only so importing
this module never needs a TTY or `questionary`; `questionary` is imported lazily
inside the `_prompt_*` functions that actually drive a prompt.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from darkroom.config import resolve_catalog
from darkroom.ingest import (
    cal_dest_rel,
    catalog_frame_counts,
    flat_filter_candidates,
    plan_session_files,
    report_catalog,
)
from darkroom.names import (
    KNOWN_FILTERS,
    PLACEHOLDERS,
    _normalize_camera,
    _normalize_target,
    make_cal_set_id,
    make_session_id,
    session_dest_rel,
)
from darkroom.parse import KNOWN_OTAS, PANEL_LABEL_RE

# Menu actions, and the sentinel for "none of the listed values, let me type one".
ACCEPT = "accept"
EDIT_TARGET = "target"
EDIT_FILTER = "filter"
EDIT_PANEL = "panel"
EDIT_OPTICS = "optics"
QUIT = "quit"
MANUAL = "\x00manual"


# ── known values ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KnownValues:
    """Pick-list vocabulary offered by the prompts.

    Seeded from the catalog (so corrections land on designations already in use
    rather than minting near-duplicates) and from the manifest itself (so values
    parsed from this card are offered even on a first-ever ingest).
    """

    targets: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    otas: tuple[str, ...] = ()
    cameras: tuple[str, ...] = ()
    combos: tuple[tuple[str, str], ...] = ()


#: Values that mean "we don't know", never offered as something to pick.
_PLACEHOLDERS = PLACEHOLDERS


def _extras(seen, preferred) -> list[str]:
    """Sorted real values from *seen* that aren't already in *preferred*."""
    return sorted({s for s in seen if s not in _PLACEHOLDERS} - set(preferred))


def collect_known_values(rows: list[dict], manifest: dict) -> KnownValues:
    """Build the pick-lists from catalog *rows* plus the manifest's own values.

    `KNOWN_FILTERS`/`KNOWN_OTAS` lead their lists in canonical order (they are
    the physical kit); anything else observed follows, sorted.
    """
    sessions = manifest.get("sessions") or []
    calibration = manifest.get("calibration") or []
    entries = sessions + calibration

    targets = {r.get("target") for r in rows} | {e.get("target") for e in sessions}
    filters = {r.get("filter") for r in rows} | {e.get("filter") for e in entries}
    otas = {r.get("ota") for r in rows} | {e.get("ota") for e in entries}
    cameras = {r.get("camera") for r in rows} | {e.get("camera") for e in entries}

    combos = {
        (r.get("ota"), r.get("camera"))
        for r in list(rows) + entries
        if r.get("ota") not in _PLACEHOLDERS and r.get("camera") not in _PLACEHOLDERS
    }

    return KnownValues(
        targets=tuple(sorted(t for t in targets if t not in _PLACEHOLDERS)),
        filters=tuple(KNOWN_FILTERS) + tuple(_extras(filters, KNOWN_FILTERS)),
        otas=tuple(KNOWN_OTAS) + tuple(_extras(otas, KNOWN_OTAS)),
        cameras=tuple(sorted(c for c in cameras if c not in _PLACEHOLDERS)),
        combos=tuple(sorted(combos)),
    )


# ── entry inspection ─────────────────────────────────────────────────────────

def is_session_entry(entry: dict) -> bool:
    """True for a sessions[] entry, False for a calibration[] one."""
    return "lights_rel_path" in entry


def wants_filter(entry: dict) -> bool:
    """True iff a filter value is meaningful for this entry.

    Lights and Flats carry a filter; Darks, FlatDarks and Bias are shot through
    whatever was in the path and their filter is legitimately null.
    """
    return is_session_entry(entry) or entry.get("frame_type") == "Flat"


def entry_label(entry: dict) -> str:
    """Short identifying label, e.g. 'M 81 · 2026-06-21' or 'Flat · 2026-06-22'."""
    if is_session_entry(entry):
        return f"{entry.get('target')} · {entry.get('obs_date')}"
    return f"{entry.get('frame_type')} · {entry.get('capture_date')}"


def entry_issues(entry: dict) -> list[str]:
    """Human-readable problems with the parsed values — drives the ⚠ lines.

    Empty list means nothing looks wrong; the entry can be accepted blind.
    """
    issues = []

    if wants_filter(entry) and entry.get("filter") in _PLACEHOLDERS:
        issues.append("filter unknown — commit will refuse this entry")
    elif wants_filter(entry) and entry["filter"] not in KNOWN_FILTERS:
        issues.append(f"filter {entry['filter']!r} is not one of the known filters")

    if entry.get("ota") in _PLACEHOLDERS:
        focal = entry.get("focal_length")
        detail = f" (FOCALLEN {focal:g})" if isinstance(focal, (int, float)) else ""
        issues.append(f"OTA not recognised{detail} — pick the optic by hand")

    if is_session_entry(entry):
        target = entry.get("target") or ""
        normalized = _normalize_target(target)
        if normalized != target:
            issues.append(f"target {target!r} normalizes to {normalized!r}")

    return issues


def suggested_action(entry: dict) -> str:
    """Which menu item to land the cursor on when the entry is opened.

    A clean entry defaults to ACCEPT, so confirming it is a single Enter. An
    entry with a problem defaults to the edit that fixes it — the whole point of
    U3 is that a wrong value should be harder to wave through than to correct,
    since every one of these ends up baked into a folder name and a session_id.
    Ordered by how much damage the value does: an unknown filter blocks the
    commit outright, an unknown OTA lands in the folder name, and target drift
    quietly forks a second folder for a target that already exists.
    """
    if wants_filter(entry) and entry.get("filter") in _PLACEHOLDERS:
        return EDIT_FILTER
    if entry.get("ota") in _PLACEHOLDERS:
        return EDIT_OPTICS
    if is_session_entry(entry):
        target = entry.get("target") or ""
        if _normalize_target(target) != target:
            return EDIT_TARGET
    return ACCEPT


def entry_lines(entry: dict, position: str = "") -> list[str]:
    """The summary block printed above the menu for one entry."""
    kind = "Session" if is_session_entry(entry) else "Calibration"
    head = f"{position} {kind}  {entry_label(entry)}".strip()

    lines = ["", head, "─" * max(len(head), 40)]
    lines.append(f"  Optics    : {entry.get('ota')} / {entry.get('camera')}")
    if wants_filter(entry):
        lines.append(f"  Filter    : {entry.get('filter') or '(unknown)'}")
    # M1: only shown when set — the overwhelming majority of sessions are
    # single-pointing, and a permanent "Panel: (none)" line would be noise.
    if entry.get("panel"):
        lines.append(f"  Panel     : {entry['panel']}")

    exposure = entry.get("exposure_sec")
    frames = entry.get("frame_count")
    lines.append(f"  Frames    : {frames} × {exposure}s  gain {entry.get('gain')}")

    if is_session_entry(entry):
        lines.append(f"  Status    : {entry.get('status')}")
        lines.append(f"  Dest      : {entry.get('lights_rel_path')}")
    else:
        lines.append(f"  Dest      : {entry.get('folder_rel_path')}")

    lines.extend(f"  ⚠ {issue}" for issue in entry_issues(entry))
    return lines


def flat_filter_hints(cal_entry: dict, session_entries: list[dict]) -> list[str]:
    """Filters used by Light sessions this Flat group plausibly belongs to.

    `ingest.flat_filter_candidates` over manifest dicts.
    """
    return flat_filter_candidates(
        cal_entry.get("capture_date"), cal_entry.get("camera"), cal_entry.get("ota"),
        (
            (s.get("filter"), s.get("camera"), s.get("ota"), s.get("obs_date"))
            for s in session_entries
        ),
    )


# ── recomputing derived fields ───────────────────────────────────────────────

def recompute_cal_entry(entry: dict) -> list[str]:
    """Rebuild set_id, folder_rel_path and file dsts after an edit."""
    entry["set_id"] = make_cal_set_id(
        entry["frame_type"], entry["camera"], entry["gain"],
        entry["exposure_sec"], entry["temperature_c"], entry["capture_date"],
    )
    dest_rel = cal_dest_rel(
        entry["frame_type"], entry["camera"], entry["ota"],
        entry.get("filter"), entry["capture_date"],
    )
    entry["folder_rel_path"] = str(dest_rel)
    for f in entry.get("files") or []:
        f["dst"] = str(dest_rel / Path(f["dst"]).name)
    return []


def recompute_session_entry(
    entry: dict,
    catalog_sessions: dict[str, int],
    archive: Path,
) -> list[str]:
    """Rebuild session_id, lights_rel_path, status and file dsts after an edit.

    An identity edit changes the session_id, so the "new / existing / topup"
    verdict has to be re-derived against the catalog too — a session renamed off
    a colliding id is usually `new` again, and would otherwise be silently
    skipped at commit. Returns warning lines (empty when all is well).
    """
    filter_ = entry.get("filter") or None
    # M1: panel is a fourth identity field, so it feeds both builders here.
    # Empty string means "not a mosaic panel" and must normalize to None, or a
    # cleared panel would append a bare "_P" to the id and a "P" dir to the path.
    panel = entry.get("panel") or None
    entry["session_id"] = make_session_id(
        entry["target"], entry["obs_date"], entry["ota"], entry["camera"], filter_,
        panel=panel,
    )
    dest_rel = session_dest_rel(
        entry["target"], entry["obs_date"], entry["ota"], entry["camera"], filter_,
        panel=panel,
    )
    entry["lights_rel_path"] = str(dest_rel)

    files = entry.get("files") or []
    if not files:
        # Manifests written before every frame was listed (old "existing"
        # entries carried files: []) can't have their copy plan rebuilt.
        for f in files:
            f["dst"] = str(dest_rel / Path(f["dst"]).name)
        return [
            f"{entry['session_id']}: no file list in the manifest, so the copy "
            "plan could not be rebuilt — re-run `darkroom ingest scan` if this "
            "session still needs copying."
        ]

    srcs = [Path(f["src"]) for f in files]
    status, file_entries = plan_session_files(
        srcs, dest_rel, archive / dest_rel, entry["session_id"], catalog_sessions,
    )
    entry["status"] = status
    entry["files"] = file_entries
    return []


def recompute_entry(
    entry: dict,
    catalog_sessions: dict[str, int],
    archive: Path,
) -> list[str]:
    """Recompute derived fields for either entry kind."""
    if is_session_entry(entry):
        return recompute_session_entry(entry, catalog_sessions, archive)
    return recompute_cal_entry(entry)


def settle_needs_review(entry: dict) -> None:
    """`needs_review` tracks exactly one thing: is the filter still unknown?

    Commit hard-refuses flagged entries, which is what stops a `NoFilter`/
    `UnknownFilter` session reaching the archive.
    """
    entry["needs_review"] = wants_filter(entry) and entry.get("filter") in _PLACEHOLDERS


def duplicate_session_ids(sessions: list[dict]) -> list[str]:
    """session_ids claimed by more than one entry after editing.

    Retargeting two nights onto the same identity would have them upsert over
    each other at commit, so the review reports it rather than letting it land.
    """
    seen: dict[str, int] = {}
    for entry in sessions:
        sid = entry.get("session_id")
        if sid:
            seen[sid] = seen.get(sid, 0) + 1
    return sorted(sid for sid, n in seen.items() if n > 1)


# ── prompts ──────────────────────────────────────────────────────────────────

def _style():
    """Shared questionary style — the dark-terminal fixes live in picker."""
    from darkroom.picker import picker_style

    return picker_style()


def _prompt_action(entry: dict) -> str | None:
    """Accept-or-edit menu for one entry. None if the user interrupted."""
    import questionary  # lazy: keep this module importable without a TTY/dep

    issues = entry_issues(entry)
    accept_title = "Accept as-is" if not issues else "Accept anyway"

    choices = [questionary.Choice(title=accept_title, value=ACCEPT)]
    if is_session_entry(entry):
        choices.append(questionary.Choice(title="Change target", value=EDIT_TARGET))
    if wants_filter(entry):
        choices.append(questionary.Choice(title="Change filter", value=EDIT_FILTER))
    if is_session_entry(entry):
        # M1: only sessions carry a panel — calibration is never per-panel,
        # since one flat set serves every panel of a night.
        choices.append(questionary.Choice(title="Change mosaic panel", value=EDIT_PANEL))
    choices.append(questionary.Choice(title="Change OTA / camera", value=EDIT_OPTICS))
    choices.append(questionary.Choice(title="Stop reviewing", value=QUIT))

    return questionary.select(
        "", choices=choices, default=suggested_action(entry), style=_style(),
    ).ask()


def _prompt_target(current: str, known: KnownValues) -> str | None:
    """Autocomplete over catalog targets; free text is allowed for new ones."""
    import questionary

    suggested = _normalize_target(current or "")
    choices = list(known.targets) or [suggested]

    answer = questionary.autocomplete(
        "Target:",
        choices=choices,
        default=suggested,
        match_middle=True,
        ignore_case=True,
        validate=lambda text: bool(text.strip()) or "Target cannot be empty.",
        style=_style(),
    ).ask()
    if answer is None:
        return None
    # Normalize whatever came back, so a typed 'ngc7000' still lands as 'NGC 7000'.
    return _normalize_target(answer)


def _prompt_filter(current: str | None, known: KnownValues, hints: list[str]) -> str | None:
    """Pick a filter from the known list, or type one. None if interrupted."""
    import questionary

    ordered = list(hints) + [f for f in known.filters if f not in hints]
    choices = [
        questionary.Choice(
            title=f"{f}   ← used by a matching Light session" if f in hints else f,
            value=f,
        )
        for f in ordered
    ]
    choices.append(questionary.Choice(title="Enter manually…", value=MANUAL))

    default = current if current in ordered else (hints[0] if hints else None)
    answer = questionary.select(
        "Filter:", choices=choices, default=default, style=_style(),
    ).ask()
    if answer is None:
        return None
    if answer != MANUAL:
        return answer

    typed = questionary.text("Filter name:", style=_style()).ask()
    return typed.strip() if typed and typed.strip() else None


def _prompt_panel(panel: str | None) -> str | None:
    """Type a mosaic panel label, or blank it to make this an ordinary session.

    Free text rather than a pick-list: panel labels are grid coordinates from
    whatever mosaic was framed that night, so there is no stable vocabulary to
    offer the way there is for filters and optics. Returns None if interrupted;
    "" means the user cleared it.
    """
    import questionary

    typed = questionary.text(
        "Mosaic panel (e.g. 1-1, blank for none):",
        default=panel or "",
        validate=lambda t: (
            not t.strip()
            or bool(PANEL_LABEL_RE.fullmatch(t.strip()))
            or "Panel must look like N-M (e.g. 1-1), or be blank."
        ),
        style=_style(),
    ).ask()
    if typed is None:
        return None
    return typed.strip()


def _prompt_optics(
    ota: str | None, camera: str | None, known: KnownValues
) -> tuple[str, str] | None:
    """Pick an observed OTA+camera pair, or enter each by hand."""
    import questionary

    choices = [
        questionary.Choice(title=f"{o} / {c}", value=(o, c)) for o, c in known.combos
    ]
    choices.append(questionary.Choice(title="Enter manually…", value=MANUAL))

    default = (ota, camera) if (ota, camera) in known.combos else None
    answer = questionary.select(
        "OTA / camera:", choices=choices, default=default, style=_style(),
    ).ask()
    if answer is None:
        return None
    if answer != MANUAL:
        return answer

    # Deliberately no pre-filled default here, unlike the target prompt: you
    # only reach these by picking "Enter manually…", i.e. after rejecting every
    # value on offer — so seeding the buffer with the current one just means
    # deleting it first.
    new_ota = questionary.autocomplete(
        "OTA:", choices=list(known.otas),
        match_middle=True, ignore_case=True,
        validate=lambda t: bool(t.strip()) or "OTA cannot be empty.",
        style=_style(),
    ).ask()
    if new_ota is None:
        return None
    new_camera = questionary.autocomplete(
        "Camera:", choices=list(known.cameras),
        match_middle=True, ignore_case=True,
        validate=lambda t: bool(t.strip()) or "Camera cannot be empty.",
        style=_style(),
    ).ask()
    if new_camera is None:
        return None
    return new_ota.strip(), _normalize_camera(new_camera.strip())


# ── review loop ──────────────────────────────────────────────────────────────

def review_entry(
    entry: dict,
    known: KnownValues,
    session_entries: list[dict],
    catalog_sessions: dict[str, int],
    archive: Path,
    position: str = "",
) -> tuple[bool, bool, list[str]]:
    """Drive the accept/edit menu for one entry until accepted or abandoned.

    Returns (changed, keep_going, warnings). The menu re-displays after every
    edit so the corrected values (and the recomputed destination) are visible
    before the entry is accepted.
    """
    changed = False
    warnings: list[str] = []

    while True:
        for line in entry_lines(entry, position):
            print(line)

        action = _prompt_action(entry)
        if action is None or action == QUIT:
            return changed, False, warnings
        if action == ACCEPT:
            return changed, True, warnings

        if action == EDIT_TARGET:
            new = _prompt_target(entry.get("target") or "", known)
            edited = new is not None and new != entry.get("target")
            if edited:
                entry["target"] = new
        elif action == EDIT_FILTER:
            hints = (
                []
                if is_session_entry(entry)
                else flat_filter_hints(entry, session_entries)
            )
            new = _prompt_filter(entry.get("filter"), known, hints)
            edited = new is not None and new != entry.get("filter")
            if edited:
                entry["filter"] = new
        elif action == EDIT_PANEL:
            new = _prompt_panel(entry.get("panel"))
            edited = new is not None and (new or None) != (entry.get("panel") or None)
            if edited:
                entry["panel"] = new or None
        else:  # EDIT_OPTICS
            new = _prompt_optics(entry.get("ota"), entry.get("camera"), known)
            edited = new is not None and new != (entry.get("ota"), entry.get("camera"))
            if edited:
                entry["ota"], entry["camera"] = new

        if edited:
            changed = True
            settle_needs_review(entry)
            warnings.extend(recompute_entry(entry, catalog_sessions, archive))


def review_manifest(
    manifest: dict,
    known: KnownValues,
    catalog_sessions: dict[str, int],
    archive: Path,
    *,
    flagged_only: bool = False,
) -> tuple[int, list[str]]:
    """Walk the manifest's entries. Returns (entries_changed, warnings)."""
    sessions = manifest.get("sessions") or []
    calibration = manifest.get("calibration") or []
    entries = sessions + calibration
    if flagged_only:
        entries = [e for e in entries if e.get("needs_review")]

    changed_count = 0
    warnings: list[str] = []

    for i, entry in enumerate(entries, 1):
        changed, keep_going, entry_warnings = review_entry(
            entry, known, sessions, catalog_sessions, archive,
            position=f"[{i}/{len(entries)}]",
        )
        changed_count += bool(changed)
        warnings.extend(entry_warnings)
        if not keep_going:
            print(f"\nStopped after {i - 1} of {len(entries)} entries.")
            break

    for sid in duplicate_session_ids(sessions):
        warnings.append(f"{sid}: claimed by more than one session in this manifest.")

    return changed_count, warnings


def _load_catalog_rows(args) -> list[dict]:
    """Catalog session rows for the pick-lists, or [] if the catalog is unreachable.

    Review is a local editing pass over a YAML file; a catalog that is down (or
    a remote one behind a flaky link) should cost autocomplete suggestions, not
    the whole command.
    """
    from darkroom.catalog_client import resolve_backend

    report_catalog(resolve_catalog(getattr(args, "catalog", None)))
    try:
        backend = resolve_backend(getattr(args, "catalog", None))
        return backend.query_sessions()
    except Exception as exc:  # noqa: BLE001 — any backend failure degrades the same way
        print(f"Warning: catalog unavailable ({exc}) — suggestions limited to "
              "known kit and this manifest.", file=sys.stderr)
        return []


def cmd_review(args) -> None:
    """`darkroom ingest review` — confirm/correct a manifest before committing."""
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Error: manifest file not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    entries = (manifest.get("sessions") or []) + (manifest.get("calibration") or [])
    if args.flagged_only:
        entries = [e for e in entries if e.get("needs_review")]

    if not entries:
        print("No items needed review." if args.flagged_only else "Manifest is empty.")
        return

    if not sys.stdin.isatty():
        print(
            "Error: `darkroom ingest review` needs an interactive terminal.\n"
            "       Run it from a shell, or fix the manifest by hand and commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    archive = Path((manifest.get("meta") or {}).get("archive") or "")
    rows = _load_catalog_rows(args)
    known = collect_known_values(rows, manifest)
    catalog_sessions = catalog_frame_counts(rows)

    changed, warnings = review_manifest(
        manifest, known, catalog_sessions, archive, flagged_only=args.flagged_only,
    )

    if changed:
        manifest_path.write_text(
            yaml.dump(manifest, default_flow_style=False, sort_keys=False,
                      allow_unicode=True)
        )
        print(f"\nUpdated {changed} entr{'y' if changed == 1 else 'ies'}: {manifest_path}")
    else:
        print("\nNo changes made.")

    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    still_flagged = [
        e.get("session_id") or e.get("set_id")
        for e in (manifest.get("sessions") or []) + (manifest.get("calibration") or [])
        if e.get("needs_review")
    ]
    if still_flagged:
        print(
            f"\n{len(still_flagged)} entr{'y' if len(still_flagged) == 1 else 'ies'} "
            "still without a filter — commit will refuse until resolved:",
            file=sys.stderr,
        )
        for item in still_flagged:
            print(f"  - {item}", file=sys.stderr)
