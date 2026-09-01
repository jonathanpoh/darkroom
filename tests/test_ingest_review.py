"""Tests for darkroom.ingest_review — the U3 interactive confirmation pass.

Everything here exercises the pure helpers and the review loop with the
questionary prompts stubbed out; the prompts themselves are verified by hand at
a pty, same as the U1 picker.
"""
from pathlib import Path

import pytest

from darkroom import ingest_review as ir
from darkroom.ingest_review import (
    ACCEPT,
    EDIT_FILTER,
    EDIT_OPTICS,
    EDIT_TARGET,
    QUIT,
    KnownValues,
    catalog_frame_counts,
    collect_known_values,
    duplicate_session_ids,
    entry_issues,
    entry_label,
    entry_lines,
    flat_filter_hints,
    is_session_entry,
    recompute_cal_entry,
    recompute_entry,
    recompute_session_entry,
    review_entry,
    review_manifest,
    settle_needs_review,
    suggested_action,
    wants_filter,
)
from darkroom.names import KNOWN_FILTERS


# ── fixtures ─────────────────────────────────────────────────────────────────

def make_session_entry(
    *,
    target="M 81",
    obs_date="2026-06-21",
    ota="FRA400",
    camera="ZWOASI585MCPro",
    filter_="L-Pro",
    files=("a.fit", "b.fit"),
    src_dir="/card",
    status="new",
    needs_review=False,
) -> dict:
    dest = f"01_Deep Sky Objects/{target}/{obs_date}_{ota}_{camera}/Lights/{filter_ or 'NoFilter'}"
    return {
        "session_id": f"{target.replace(' ', '')}_{obs_date.replace('-', '')}_{ota}_{camera}_{filter_ or 'UnknownFilter'}",
        "target": target,
        "obs_date": obs_date,
        "ota": ota,
        "camera": camera,
        "filter": filter_,
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": len(files),
        "needs_review": needs_review,
        "status": status,
        "lights_rel_path": dest,
        "files": [
            {"src": f"{src_dir}/{n}", "dst": f"{dest}/{n}", "copy": True} for n in files
        ],
    }


def make_cal_entry(
    *,
    frame_type="Flat",
    ota="FRA400",
    camera="ZWOASI585MCPro",
    filter_="L-Pro",
    capture_date="2026-06-22",
    files=("f1.fit",),
    needs_review=False,
) -> dict:
    dest = f"00_Calibration/Flats/{ota}_{camera}_{filter_ or 'NoFilter'}/{capture_date}"
    return {
        "set_id": f"{frame_type}_{camera}_2s_200g_-20C_{capture_date}",
        "frame_type": frame_type,
        "camera": camera,
        "ota": ota,
        "filter": filter_,
        "gain": 200,
        "exposure_sec": 2.0,
        "temperature_c": -20.0,
        "capture_date": capture_date,
        "frame_count": len(files),
        "needs_review": needs_review,
        "folder_rel_path": dest,
        "files": [{"src": f"/card/{n}", "dst": f"{dest}/{n}", "copy": True} for n in files],
    }


def make_catalog_row(**kw) -> dict:
    row = {
        "session_id": "M81_20260101_FRA400_ZWOASI585MCPro_L-Pro",
        "target": "M 81",
        "obs_date": "2026-01-01",
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "frame_count": 40,
    }
    row.update(kw)
    return row


# ── collect_known_values ─────────────────────────────────────────────────────

def test_collect_known_values_merges_catalog_and_manifest():
    rows = [make_catalog_row(target="NGC 7000", camera="Canon6D", ota="FMA180")]
    manifest = {"sessions": [make_session_entry(target="M 81")], "calibration": []}
    known = collect_known_values(rows, manifest)

    assert known.targets == ("M 81", "NGC 7000")
    assert "Canon6D" in known.cameras and "ZWOASI585MCPro" in known.cameras


def test_collect_known_values_puts_known_kit_first():
    rows = [make_catalog_row(filter_="x", ota="x")]
    known = collect_known_values(rows, {})

    assert known.filters[: len(KNOWN_FILTERS)] == tuple(KNOWN_FILTERS)
    assert known.otas[:3] == ("FMA180", "FRA400-07x", "FRA400")


def test_collect_known_values_appends_unseen_values_after_known_kit():
    rows = [make_catalog_row(filter=None), make_catalog_row(filter="Duoband")]
    known = collect_known_values(rows, {})

    assert known.filters[-1] == "Duoband"
    assert known.filters.count("Duoband") == 1


def test_collect_known_values_excludes_placeholders():
    rows = [
        make_catalog_row(target=None, ota="Unknown", camera=None, filter="UnknownFilter"),
    ]
    known = collect_known_values(rows, {})

    assert known.targets == ()
    assert known.cameras == ()
    assert "Unknown" not in known.otas
    assert "UnknownFilter" not in known.filters


def test_collect_known_values_combos_skip_unknown_optics():
    rows = [
        make_catalog_row(ota="FRA400", camera="ZWOASI585MCPro"),
        make_catalog_row(ota="Unknown", camera="Canon6D"),
    ]
    known = collect_known_values(rows, {})

    assert known.combos == (("FRA400", "ZWOASI585MCPro"),)


def test_collect_known_values_handles_empty_manifest():
    assert collect_known_values([], {}).targets == ()
    assert collect_known_values([], {"sessions": None, "calibration": None}).cameras == ()


def test_collect_known_values_sources_combos_from_calibration_too():
    manifest = {"calibration": [make_cal_entry(ota="FMA180", camera="Canon6D")]}
    known = collect_known_values([], manifest)

    assert ("FMA180", "Canon6D") in known.combos


# ── catalog_frame_counts ─────────────────────────────────────────────────────

def test_catalog_frame_counts():
    rows = [make_catalog_row(session_id="A", frame_count=12)]
    assert catalog_frame_counts(rows) == {"A": 12}


def test_catalog_frame_counts_null_count_is_zero():
    assert catalog_frame_counts([make_catalog_row(session_id="A", frame_count=None)]) == {"A": 0}


# ── entry inspection ─────────────────────────────────────────────────────────

def test_is_session_entry():
    assert is_session_entry(make_session_entry()) is True
    assert is_session_entry(make_cal_entry()) is False


@pytest.mark.parametrize(
    "frame_type,expected",
    [("Flat", True), ("Dark", False), ("FlatDark", False), ("Bias", False)],
)
def test_wants_filter_calibration(frame_type, expected):
    assert wants_filter(make_cal_entry(frame_type=frame_type)) is expected


def test_wants_filter_session_always_true():
    assert wants_filter(make_session_entry(filter_=None)) is True


def test_entry_label():
    assert entry_label(make_session_entry()) == "M 81 · 2026-06-21"
    assert entry_label(make_cal_entry(frame_type="Dark")) == "Dark · 2026-06-22"


# ── entry_issues ─────────────────────────────────────────────────────────────

def test_entry_issues_clean_session():
    assert entry_issues(make_session_entry()) == []


def test_entry_issues_unknown_filter():
    issues = entry_issues(make_session_entry(filter_=None))
    assert any("filter unknown" in i for i in issues)


def test_entry_issues_unrecognised_filter():
    issues = entry_issues(make_session_entry(filter_="panel_1-2"))
    assert any("not one of the known filters" in i for i in issues)


def test_entry_issues_unknown_ota_names_focal_length():
    issues = entry_issues(make_session_entry(ota="Unknown"))
    assert any("FOCALLEN 400" in i for i in issues)


def test_entry_issues_unknown_ota_without_focal_length():
    entry = make_session_entry(ota="Unknown")
    entry["focal_length"] = None
    issues = entry_issues(entry)
    assert any("OTA not recognised" in i and "FOCALLEN" not in i for i in issues)


def test_entry_issues_target_normalization_drift():
    issues = entry_issues(make_session_entry(target="ngc7000"))
    assert any("normalizes to 'NGC 7000'" in i for i in issues)


def test_entry_issues_dark_with_null_filter_is_clean():
    assert entry_issues(make_cal_entry(frame_type="Dark", filter_=None)) == []


def test_entry_issues_flat_with_null_filter_is_flagged():
    issues = entry_issues(make_cal_entry(frame_type="Flat", filter_=None))
    assert any("filter unknown" in i for i in issues)


# ── suggested_action ─────────────────────────────────────────────────────────

def test_suggested_action_clean_entry_defaults_to_accept():
    assert suggested_action(make_session_entry()) == ACCEPT
    assert suggested_action(make_cal_entry(frame_type="Dark", filter_=None)) == ACCEPT


def test_suggested_action_unknown_filter_wins():
    entry = make_session_entry(filter_=None, ota="Unknown", target="m81")
    assert suggested_action(entry) == EDIT_FILTER


def test_suggested_action_unknown_ota_beats_target_drift():
    entry = make_session_entry(ota="Unknown", target="m81")
    assert suggested_action(entry) == EDIT_OPTICS


def test_suggested_action_target_drift():
    assert suggested_action(make_session_entry(target="m81")) == EDIT_TARGET


def test_suggested_action_flat_missing_filter():
    assert suggested_action(make_cal_entry(filter_=None)) == EDIT_FILTER


def test_suggested_action_ignores_target_drift_on_calibration():
    assert suggested_action(make_cal_entry(ota="Unknown")) == EDIT_OPTICS


# ── entry_lines ──────────────────────────────────────────────────────────────

def test_entry_lines_shows_identity_and_destination():
    text = "\n".join(entry_lines(make_session_entry(), position="[1/3]"))
    assert "[1/3] Session  M 81 · 2026-06-21" in text
    assert "FRA400 / ZWOASI585MCPro" in text
    assert "01_Deep Sky Objects/M 81" in text
    assert "Filter    : L-Pro" in text


def test_entry_lines_shows_warnings():
    text = "\n".join(entry_lines(make_session_entry(filter_=None)))
    assert "⚠ filter unknown" in text


def test_entry_lines_calibration_omits_filter_row_for_darks():
    lines = entry_lines(make_cal_entry(frame_type="Dark", filter_=None))
    assert not any(line.startswith("  Filter") for line in lines)
    assert any("00_Calibration" in line for line in lines)


# ── flat_filter_hints ────────────────────────────────────────────────────────

def test_flat_filter_hints_morning_after():
    flat = make_cal_entry(capture_date="2026-06-22", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Extreme")]
    assert flat_filter_hints(flat, sessions) == ["L-Extreme"]


def test_flat_filter_hints_same_day():
    flat = make_cal_entry(capture_date="2026-06-21", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Pro")]
    assert flat_filter_hints(flat, sessions) == ["L-Pro"]


def test_flat_filter_hints_multiple_sorted():
    flat = make_cal_entry(capture_date="2026-06-22", filter_=None)
    sessions = [
        make_session_entry(obs_date="2026-06-21", filter_="L-Pro"),
        make_session_entry(obs_date="2026-06-22", filter_="L-Extreme"),
    ]
    assert flat_filter_hints(flat, sessions) == ["L-Extreme", "L-Pro"]


def test_flat_filter_hints_too_far_away():
    flat = make_cal_entry(capture_date="2026-06-25", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Pro")]
    assert flat_filter_hints(flat, sessions) == []


def test_flat_filter_hints_before_the_session_does_not_match():
    flat = make_cal_entry(capture_date="2026-06-20", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Pro")]
    assert flat_filter_hints(flat, sessions) == []


def test_flat_filter_hints_requires_matching_optics():
    flat = make_cal_entry(capture_date="2026-06-22", camera="Canon6D", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Pro")]
    assert flat_filter_hints(flat, sessions) == []

    flat = make_cal_entry(capture_date="2026-06-22", ota="FMA180", filter_=None)
    assert flat_filter_hints(flat, sessions) == []


def test_flat_filter_hints_skips_sessions_without_a_filter():
    flat = make_cal_entry(capture_date="2026-06-22", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_=None)]
    assert flat_filter_hints(flat, sessions) == []


def test_flat_filter_hints_missing_or_bad_capture_date():
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Pro")]
    assert flat_filter_hints(make_cal_entry(capture_date=""), sessions) == []
    assert flat_filter_hints(make_cal_entry(capture_date="not-a-date"), sessions) == []


# ── recompute ────────────────────────────────────────────────────────────────

def test_recompute_cal_entry_rewrites_id_path_and_dsts():
    entry = make_cal_entry(filter_="L-Pro")
    entry["filter"] = "L-Extreme"
    assert recompute_cal_entry(entry) == []

    assert entry["folder_rel_path"] == (
        "00_Calibration/Flats/FRA400_ZWOASI585MCPro_L-Extreme/2026-06-22"
    )
    assert entry["files"][0]["dst"].endswith("L-Extreme/2026-06-22/f1.fit")


def test_recompute_cal_entry_dark_ignores_filter_in_path():
    entry = make_cal_entry(frame_type="Dark", filter_=None)
    recompute_cal_entry(entry)
    assert entry["folder_rel_path"] == "00_Calibration/Darks/ZWOASI585MCPro"


def test_recompute_session_entry_after_filter_edit(tmp_path):
    entry = make_session_entry(filter_=None, needs_review=True)
    entry["filter"] = "L-Extreme"
    assert recompute_session_entry(entry, {}, tmp_path) == []

    assert entry["session_id"] == "M81_20260621_FRA400_ZWOASI585MCPro_L-Extreme"
    assert entry["lights_rel_path"].endswith("Lights/L-Extreme")
    assert all(f["dst"].startswith(entry["lights_rel_path"]) for f in entry["files"])
    assert entry["status"] == "new"


def test_recompute_session_entry_after_target_edit(tmp_path):
    entry = make_session_entry(target="ngc7000")
    entry["target"] = "NGC 7000"
    recompute_session_entry(entry, {}, tmp_path)

    assert entry["session_id"].startswith("NGC7000_20260621_")
    assert entry["lights_rel_path"] == (
        "01_Deep Sky Objects/NGC 7000/2026-06-21_FRA400_ZWOASI585MCPro/Lights/L-Pro"
    )


def test_recompute_session_entry_after_optics_edit(tmp_path):
    entry = make_session_entry()
    entry["ota"], entry["camera"] = "FMA180", "Canon6D"
    recompute_session_entry(entry, {}, tmp_path)

    assert entry["session_id"] == "M81_20260621_FMA180_Canon6D_L-Pro"
    assert "2026-06-21_FMA180_Canon6D" in entry["lights_rel_path"]


def test_recompute_session_entry_redetects_existing_status(tmp_path):
    """Editing onto an id the catalog already has in full makes it 'existing'."""
    entry = make_session_entry(filter_=None, needs_review=True, status="new")
    entry["filter"] = "L-Pro"
    recompute_session_entry(
        entry, {"M81_20260621_FRA400_ZWOASI585MCPro_L-Pro": 2}, tmp_path,
    )

    assert entry["status"] == "existing"
    assert all(f["copy"] is False for f in entry["files"])


def test_recompute_session_entry_redetects_new_status(tmp_path):
    """Editing off a colliding id restores 'new' so commit stops skipping it."""
    entry = make_session_entry(status="existing")
    for f in entry["files"]:
        f["copy"] = False
    entry["target"] = "NGC 7000"
    recompute_session_entry(
        entry, {"M81_20260621_FRA400_ZWOASI585MCPro_L-Pro": 2}, tmp_path,
    )

    assert entry["status"] == "new"
    assert all(f["copy"] is True for f in entry["files"])


def test_recompute_session_entry_topup_checks_disk(tmp_path):
    entry = make_session_entry(files=("a.fit", "b.fit"))
    dest = tmp_path / entry["lights_rel_path"]
    dest.mkdir(parents=True)
    (dest / "a.fit").write_text("")

    recompute_session_entry(
        entry, {"M81_20260621_FRA400_ZWOASI585MCPro_L-Pro": 1}, tmp_path,
    )

    assert entry["status"] == "topup"
    copies = {Path(f["src"]).name: f["copy"] for f in entry["files"]}
    assert copies == {"a.fit": False, "b.fit": True}


def test_recompute_session_entry_warns_when_file_list_is_missing(tmp_path):
    entry = make_session_entry(status="existing")
    entry["files"] = []
    entry["filter"] = "L-Extreme"
    warnings = recompute_session_entry(entry, {}, tmp_path)

    assert len(warnings) == 1
    assert "re-run `darkroom ingest scan`" in warnings[0]
    assert entry["status"] == "existing"  # left alone rather than guessed at


def test_recompute_entry_dispatches(tmp_path):
    cal = make_cal_entry()
    cal["filter"] = "L-Ultimate"
    recompute_entry(cal, {}, tmp_path)
    assert "L-Ultimate" in cal["folder_rel_path"]

    sess = make_session_entry()
    sess["filter"] = "L-Ultimate"
    recompute_entry(sess, {}, tmp_path)
    assert sess["session_id"].endswith("_L-Ultimate")


# ── needs_review / duplicates ────────────────────────────────────────────────

def test_settle_needs_review_clears_once_filter_is_known():
    entry = make_session_entry(filter_=None, needs_review=True)
    entry["filter"] = "L-Pro"
    settle_needs_review(entry)
    assert entry["needs_review"] is False


def test_settle_needs_review_keeps_flag_while_filter_unknown():
    entry = make_session_entry(filter_=None, needs_review=True)
    settle_needs_review(entry)
    assert entry["needs_review"] is True


def test_settle_needs_review_nofilter_is_a_real_answer():
    entry = make_session_entry(filter_=None, needs_review=True)
    entry["filter"] = "NoFilter"
    settle_needs_review(entry)
    assert entry["needs_review"] is False


def test_settle_needs_review_never_flags_darks():
    entry = make_cal_entry(frame_type="Dark", filter_=None, needs_review=True)
    settle_needs_review(entry)
    assert entry["needs_review"] is False


def test_duplicate_session_ids():
    a = make_session_entry(target="M 81")
    b = make_session_entry(target="M 81")
    c = make_session_entry(target="M 82")
    assert duplicate_session_ids([a, b, c]) == [a["session_id"]]
    assert duplicate_session_ids([a, c]) == []


# ── review loop (prompts stubbed) ────────────────────────────────────────────

@pytest.fixture
def stub_prompts(monkeypatch):
    """Queue up menu actions and prompt answers for the review loop."""

    class Stubs:
        def __init__(self):
            self.actions = []
            self.targets = []
            self.filters = []
            self.optics = []

        def install(self):
            monkeypatch.setattr(ir, "_prompt_action", lambda e: self.actions.pop(0))
            monkeypatch.setattr(ir, "_prompt_target", lambda c, k: self.targets.pop(0))
            monkeypatch.setattr(ir, "_prompt_filter", lambda c, k, h: self.filters.pop(0))
            monkeypatch.setattr(ir, "_prompt_optics", lambda o, c, k: self.optics.pop(0))
            return self

    return Stubs().install()


KNOWN = KnownValues(
    targets=("M 81",), filters=KNOWN_FILTERS, otas=("FRA400",),
    cameras=("ZWOASI585MCPro",), combos=(("FRA400", "ZWOASI585MCPro"),),
)


def test_review_entry_accept_leaves_entry_untouched(stub_prompts, tmp_path):
    stub_prompts.actions = [ACCEPT]
    entry = make_session_entry()
    before = dict(entry)

    changed, keep_going, warnings = review_entry(entry, KNOWN, [], {}, tmp_path)

    assert (changed, keep_going, warnings) == (False, True, [])
    assert entry == before


def test_review_entry_filter_edit_recomputes_and_clears_flag(stub_prompts, tmp_path):
    stub_prompts.actions = [EDIT_FILTER, ACCEPT]
    stub_prompts.filters = ["L-Extreme"]
    entry = make_session_entry(filter_=None, needs_review=True)

    changed, keep_going, _ = review_entry(entry, KNOWN, [], {}, tmp_path)

    assert (changed, keep_going) == (True, True)
    assert entry["filter"] == "L-Extreme"
    assert entry["needs_review"] is False
    assert entry["session_id"].endswith("_L-Extreme")
    assert entry["lights_rel_path"].endswith("Lights/L-Extreme")


def test_review_entry_target_then_optics_in_one_visit(stub_prompts, tmp_path):
    stub_prompts.actions = [EDIT_TARGET, EDIT_OPTICS, ACCEPT]
    stub_prompts.targets = ["NGC 7000"]
    stub_prompts.optics = [("FMA180", "Canon6D")]
    entry = make_session_entry()

    changed, _, _ = review_entry(entry, KNOWN, [], {}, tmp_path)

    assert changed is True
    assert entry["session_id"] == "NGC7000_20260621_FMA180_Canon6D_L-Pro"
    assert entry["lights_rel_path"] == (
        "01_Deep Sky Objects/NGC 7000/2026-06-21_FMA180_Canon6D/Lights/L-Pro"
    )


def test_review_entry_cancelled_prompt_is_not_a_change(stub_prompts, tmp_path):
    """Ctrl-C out of a sub-prompt returns None — the entry keeps its value."""
    stub_prompts.actions = [EDIT_FILTER, ACCEPT]
    stub_prompts.filters = [None]
    entry = make_session_entry(filter_="L-Pro")

    changed, keep_going, _ = review_entry(entry, KNOWN, [], {}, tmp_path)

    assert (changed, keep_going) == (False, True)
    assert entry["filter"] == "L-Pro"


def test_review_entry_reselecting_the_same_value_is_not_a_change(stub_prompts, tmp_path):
    stub_prompts.actions = [EDIT_FILTER, ACCEPT]
    stub_prompts.filters = ["L-Pro"]
    entry = make_session_entry(filter_="L-Pro")

    changed, _, _ = review_entry(entry, KNOWN, [], {}, tmp_path)
    assert changed is False


def test_review_entry_quit_stops_the_walk(stub_prompts, tmp_path):
    stub_prompts.actions = [QUIT]
    changed, keep_going, _ = review_entry(make_session_entry(), KNOWN, [], {}, tmp_path)
    assert (changed, keep_going) == (False, False)


def test_review_entry_interrupted_menu_stops_the_walk(stub_prompts, tmp_path):
    stub_prompts.actions = [None]
    _, keep_going, _ = review_entry(make_session_entry(), KNOWN, [], {}, tmp_path)
    assert keep_going is False


def test_review_entry_flat_edit_gets_session_hints(monkeypatch, tmp_path):
    seen = {}
    actions = iter([EDIT_FILTER, ACCEPT])
    monkeypatch.setattr(ir, "_prompt_action", lambda e: next(actions))

    def fake_filter(current, known, hints):
        seen["hints"] = hints
        return "L-Extreme"

    monkeypatch.setattr(ir, "_prompt_filter", fake_filter)

    flat = make_cal_entry(capture_date="2026-06-22", filter_=None)
    sessions = [make_session_entry(obs_date="2026-06-21", filter_="L-Extreme")]

    review_entry(flat, KNOWN, sessions, {}, tmp_path)

    assert seen["hints"] == ["L-Extreme"]
    assert flat["filter"] == "L-Extreme"
    assert "L-Extreme" in flat["folder_rel_path"]


# ── review_manifest ──────────────────────────────────────────────────────────

def test_review_manifest_walks_sessions_then_calibration(stub_prompts, tmp_path):
    manifest = {
        "sessions": [make_session_entry()],
        "calibration": [make_cal_entry(), make_cal_entry(frame_type="Dark", filter_=None)],
    }
    stub_prompts.actions = [ACCEPT, ACCEPT, ACCEPT]

    changed, warnings = review_manifest(manifest, KNOWN, {}, tmp_path)

    assert (changed, warnings) == (0, [])
    assert stub_prompts.actions == []


def test_review_manifest_counts_changed_entries(stub_prompts, tmp_path):
    manifest = {
        "sessions": [make_session_entry(filter_=None), make_session_entry(target="M 82")],
        "calibration": [],
    }
    stub_prompts.actions = [EDIT_FILTER, ACCEPT, ACCEPT]
    stub_prompts.filters = ["L-Pro"]

    changed, _ = review_manifest(manifest, KNOWN, {}, tmp_path)
    assert changed == 1


def test_review_manifest_flagged_only_skips_clean_entries(stub_prompts, tmp_path):
    flagged = make_session_entry(filter_=None, needs_review=True)
    manifest = {
        "sessions": [make_session_entry(), flagged],
        "calibration": [make_cal_entry()],
    }
    stub_prompts.actions = [EDIT_FILTER, ACCEPT]
    stub_prompts.filters = ["L-Pro"]

    changed, _ = review_manifest(manifest, KNOWN, {}, tmp_path, flagged_only=True)

    assert changed == 1
    assert stub_prompts.actions == []
    assert flagged["needs_review"] is False


def test_review_manifest_quit_stops_early(stub_prompts, tmp_path, capsys):
    manifest = {"sessions": [make_session_entry(), make_session_entry(target="M 82")]}
    stub_prompts.actions = [ACCEPT, QUIT]

    review_manifest(manifest, KNOWN, {}, tmp_path)

    assert "Stopped after 1 of 2 entries." in capsys.readouterr().out


def test_review_manifest_reports_duplicate_ids(stub_prompts, tmp_path):
    manifest = {
        "sessions": [make_session_entry(target="M 82"), make_session_entry(target="M 81")],
    }
    stub_prompts.actions = [EDIT_TARGET, ACCEPT, ACCEPT]
    stub_prompts.targets = ["M 81"]

    _, warnings = review_manifest(manifest, KNOWN, {}, tmp_path)

    assert any("claimed by more than one session" in w for w in warnings)


def test_review_manifest_empty_is_a_no_op(stub_prompts, tmp_path):
    assert review_manifest({}, KNOWN, {}, tmp_path) == (0, [])


# ── cmd_review ───────────────────────────────────────────────────────────────

class Args:
    def __init__(self, manifest, flagged_only=False, catalog=None):
        self.manifest = manifest
        self.flagged_only = flagged_only
        self.catalog = catalog


def test_cmd_review_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ir.cmd_review(Args(tmp_path / "nope.yaml"))
    assert exc.value.code == 1


def test_cmd_review_empty_manifest_returns_without_a_tty(tmp_path, capsys):
    path = tmp_path / "m.yaml"
    path.write_text("sessions: []\ncalibration: []\n")

    ir.cmd_review(Args(path))
    assert "Manifest is empty." in capsys.readouterr().out


def test_cmd_review_flagged_only_with_nothing_flagged(tmp_path, capsys):
    import yaml

    path = tmp_path / "m.yaml"
    path.write_text(yaml.dump({"sessions": [make_session_entry()], "calibration": []}))

    ir.cmd_review(Args(path, flagged_only=True))
    assert "No items needed review." in capsys.readouterr().out


def test_cmd_review_refuses_without_a_tty(tmp_path, monkeypatch, capsys):
    import yaml

    path = tmp_path / "m.yaml"
    path.write_text(yaml.dump({"sessions": [make_session_entry()], "calibration": []}))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit) as exc:
        ir.cmd_review(Args(path))

    assert exc.value.code == 1
    assert "needs an interactive terminal" in capsys.readouterr().err


def test_cmd_review_writes_corrections_back(tmp_path, monkeypatch, capsys):
    import yaml

    entry = make_session_entry(filter_=None, needs_review=True)
    path = tmp_path / "m.yaml"
    path.write_text(yaml.dump({
        "meta": {"archive": str(tmp_path / "archive")},
        "sessions": [entry],
        "calibration": [],
    }))

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ir, "_load_catalog_rows", lambda args: [])
    actions = iter([EDIT_FILTER, ACCEPT])
    monkeypatch.setattr(ir, "_prompt_action", lambda e: next(actions))
    monkeypatch.setattr(ir, "_prompt_filter", lambda c, k, h: "L-Extreme")

    ir.cmd_review(Args(path))

    written = yaml.safe_load(path.read_text())["sessions"][0]
    assert written["filter"] == "L-Extreme"
    assert written["needs_review"] is False
    assert written["session_id"].endswith("_L-Extreme")
    assert "Updated 1 entry" in capsys.readouterr().out


def test_cmd_review_reports_entries_still_missing_a_filter(tmp_path, monkeypatch, capsys):
    import yaml

    entry = make_session_entry(filter_=None, needs_review=True)
    path = tmp_path / "m.yaml"
    path.write_text(yaml.dump({"meta": {"archive": str(tmp_path)}, "sessions": [entry]}))

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ir, "_load_catalog_rows", lambda args: [])
    monkeypatch.setattr(ir, "_prompt_action", lambda e: ACCEPT)

    ir.cmd_review(Args(path))

    err = capsys.readouterr().err
    assert "still without a filter" in err
    assert entry["session_id"] in err


def test_load_catalog_rows_degrades_when_the_catalog_is_unreachable(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("darkroom.catalog_client.resolve_backend", boom)

    assert ir._load_catalog_rows(Args("x")) == []
    assert "catalog unavailable" in capsys.readouterr().err


def test_recompute_entry_leaves_the_session_span_untouched(tmp_path):
    """start_utc/end_utc are derived from frames, not from identity fields.

    An identity edit rewrites session_id/lights_rel_path/dsts; the wall-clock
    span describes when the frames were shot and must survive unchanged.
    """
    sess = make_session_entry()
    sess["start_utc"] = "2026-06-21T22:00:00"
    sess["end_utc"] = "2026-06-22T02:30:00"

    sess["target"] = "M 82"
    recompute_entry(sess, {}, tmp_path)

    assert sess["session_id"].startswith("M82_")
    assert sess["start_utc"] == "2026-06-21T22:00:00"
    assert sess["end_utc"] == "2026-06-22T02:30:00"


def test_prompt_panel_validator_accepts_labels_and_blank(monkeypatch):
    """The panel prompt's validator is exercised, not just constructed.

    It referenced a `_PANEL_LABEL_RE` that did not exist, so typing any panel
    raised NameError inside questionary. Capture the validate callable and run
    it directly.
    """
    import questionary

    captured = {}

    class _Q:
        def ask(self):
            return "1-2"

    def fake_text(message, **kwargs):
        captured.update(kwargs)
        return _Q()

    monkeypatch.setattr(questionary, "text", fake_text)
    assert ir._prompt_panel(None) == "1-2"
    validate = captured["validate"]
    assert validate("1-2") is True
    assert validate("  ") is True          # blank clears the panel
    assert validate("10-3") is True
    assert isinstance(validate("abc"), str)  # an error message, not a bool
    assert isinstance(validate("1-2-3"), str)
