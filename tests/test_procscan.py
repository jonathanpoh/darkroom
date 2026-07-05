"""Tests for darkroom.procscan (F1) — archive-derived processed_state reconciliation."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pytest

from darkroom.catalog import query_all_sessions
from darkroom.catalog_client import LocalBackend
from darkroom.catalog_cli import _scan_processed_run
from darkroom.cataloger import init_db, set_processed_state, upsert_session
from darkroom.procscan import Transition, apply, classify_session, classify_target, scan


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _session(session_id: str, target: str = "M 81", obs_date: str = "2026-02-19", **extra) -> dict:
    base = {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": 100,
        "total_integration_sec": 18000,
        "ra_deg": 148.89,
        "dec_deg": 69.07,
        "lights_path": f"01_Deep Sky Objects/{target}/{obs_date}_FRA400_ZWOASI585MCPro/Lights/L-Pro",
        "notes": "",
    }
    base.update(extra)
    return base


def _build_catalog(tmp_path: Path, sessions: list[dict]) -> Path:
    db = tmp_path / "cat.db"
    init_db(db)
    for row in sessions:
        upsert_session(db, row)
    return db


# ── classify_target ──────────────────────────────────────────────────────────

def test_classify_target_subs_only_dir_both_lists_empty(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    session = target_dir / "2026-02-19_FRA400_ZWOASI585MCPro" / "Lights"
    touch(session / "Light_0001.fit")
    touch(session / "Light_0002.orf")
    touch(session / "Light_0003.cr2")

    ev = classify_target(target_dir, tmp_path)

    assert ev == {
        "export_dates": [], "inprogress_dates": [],
        "logged_processed_nights": frozenset(), "logged_inprogress_nights": frozenset(),
    }


def test_classify_target_masterlight_xisf_is_inprogress_not_export(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    master = target_dir / "2025-01-18_FRA400_ZWOASI585MCPro" / "master"
    touch(master / "masterLight_BIN-1_3840x2160_FILTER-L-Pro_RGB.xisf")

    ev = classify_target(target_dir, tmp_path)

    assert ev["export_dates"] == []
    assert len(ev["inprogress_dates"]) == 1


def test_classify_target_tif_under_dated_processed_folder(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    touch(target_dir / "_Processed" / "2025-04-26" / "M81_final.tif")

    ev = classify_target(target_dir, tmp_path)

    assert ev["export_dates"] == ["2025-04-26"]
    assert ev["inprogress_dates"] == []


def test_classify_target_ignores_subs_and_lights_thumbnail(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    session = target_dir / "2026-02-19_FRA400_ZWOASI585MCPro"
    touch(session / "Lights" / "Light_0001.fit")
    # A .jpg is an export extension, but living under Lights/ it's a thumbnail
    # (or otherwise sub-adjacent) and must never count as export evidence.
    touch(session / "Lights" / "Light_0001_thn.jpg")
    touch(session / "master" / "masterLight_stack.xisf")

    ev = classify_target(target_dir, tmp_path)

    assert ev["export_dates"] == []
    assert len(ev["inprogress_dates"]) == 1


def test_classify_target_ignores_lights_lowercase_variant(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    touch(target_dir / "2026-02-19_FRA400_ZWOASI585MCPro" / "lights" / "export_looking.jpg")

    ev = classify_target(target_dir, tmp_path)

    assert ev["export_dates"] == []


def test_classify_target_messy_folder_names_no_crash(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    # "2025-02 Processed" (year-month, not a full date) and a trailing-space
    # variant — both real folder names seen in the archive.
    touch(target_dir / "2025-02 Processed" / "master" / "masterLight_stack.xisf")
    touch(target_dir / "2025-02 Processed " / "final_export.tif")

    ev = classify_target(target_dir, tmp_path)

    assert len(ev["inprogress_dates"]) == 1
    assert len(ev["export_dates"]) == 1
    # Neither folder name contains a full YYYY-MM-DD, so both fall back to mtime.
    today = date.today().isoformat()
    assert ev["inprogress_dates"][0] == today
    assert ev["export_dates"][0] == today


def test_classify_target_nested_processed_date_at_depth(tmp_path):
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    touch(target_dir / "_Processed" / "2025-06-10" / "M81_LRGB" / "master" / "masterLight.xisf")

    ev = classify_target(target_dir, tmp_path)

    assert ev["inprogress_dates"] == ["2025-06-10"]


# ── classify_session ─────────────────────────────────────────────────────────

def test_classify_session_before_edit_date_is_processed():
    target_ev = {"export_dates": ["2025-04-26"], "inprogress_dates": []}
    state, evidence, ev_date = classify_session("2025-03-10", target_ev)
    assert (state, evidence, ev_date) == ("processed", "date-bound", "2025-04-26")


def test_classify_session_after_newest_edit_stays_unprocessed():
    # The M 81 case: 2025-03 nights get processed by a 2025-04 edit, but a
    # 2026-02 night (shot after the edit) can't retroactively be covered.
    target_ev = {"export_dates": ["2025-04-26"], "inprogress_dates": []}
    state, evidence, ev_date = classify_session("2026-02-19", target_ev)
    assert (state, evidence, ev_date) == ("unprocessed", "", None)


def test_classify_session_in_progress_when_only_xisf_evidence():
    target_ev = {"export_dates": [], "inprogress_dates": ["2025-05-01"]}
    state, evidence, ev_date = classify_session("2025-04-01", target_ev)
    assert (state, evidence, ev_date) == ("in_progress", "date-bound", "2025-05-01")


def test_classify_session_export_outranks_inprogress():
    target_ev = {"export_dates": ["2025-05-01"], "inprogress_dates": ["2025-01-01"]}
    state, evidence, ev_date = classify_session("2025-04-01", target_ev)
    assert state == "processed"


def test_classify_session_earliest_covering_date_used():
    # Two edits touch this target; the earliest one that still covers the
    # session's obs_date is the one that "did" the processing.
    target_ev = {"export_dates": ["2025-04-26", "2025-06-01"], "inprogress_dates": []}
    state, evidence, ev_date = classify_session("2025-03-10", target_ev)
    assert ev_date == "2025-04-26"


def test_classify_session_no_evidence_is_unprocessed():
    assert classify_session("2026-02-19", {"export_dates": [], "inprogress_dates": []}) == (
        "unprocessed", "", None,
    )


# ── classify_target / classify_session: F2 log evidence ─────────────────────

def test_classify_target_no_logs_regresses_to_f1_behavior(tmp_path):
    """A target with no WBPP logs at all: logged_* sets are empty and the
    *_dates pools + classify_session outcome are byte-identical to F1."""
    target_dir = tmp_path / "01_Deep Sky Objects" / "M 81"
    touch(target_dir / "_Processed" / "2025-04-26" / "M81_final.tif")
    touch(target_dir / "master" / "masterLight_stack.xisf")

    ev = classify_target(target_dir, tmp_path)

    assert ev["logged_processed_nights"] == frozenset()
    assert ev["logged_inprogress_nights"] == frozenset()
    assert ev["export_dates"] == ["2025-04-26"]
    assert len(ev["inprogress_dates"]) == 1

    state, evidence, ev_date = classify_session("2025-03-10", ev)
    assert (state, evidence, ev_date) == ("processed", "date-bound", "2025-04-26")


def test_classify_target_distinguishes_log_vs_date_bound_evidence(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    run_dir = target_dir / "_Processed" / "2025-04-26" / "Title"
    touch(run_dir / "logs" / "one.log",
          b"Light_M81_180.0s_Bin1_ISO3200_20250320-233000_14.0C_0001.fit\n")
    touch(run_dir / "M81_final.tif")
    # A separate, un-logged loose master elsewhere under the target.
    touch(target_dir / "loose" / "masterLight_stack.xisf")

    ev = classify_target(target_dir, archive)

    assert ev["logged_processed_nights"] == frozenset({"2025-03-20"})
    # The run's own export is claimed by the run and excluded from the
    # date-bound pool -- only the un-logged loose master remains there.
    assert ev["export_dates"] == []
    assert len(ev["inprogress_dates"]) == 1

    state, evidence, ev_date = classify_session("2025-03-20", ev)
    assert (state, evidence) == ("processed", "log")

    state, evidence, ev_date = classify_session("2020-01-01", ev)
    assert (state, evidence) == ("in_progress", "date-bound")


def test_classify_session_logged_inprogress_when_run_has_no_export():
    target_ev = {
        "export_dates": [], "inprogress_dates": [],
        "logged_processed_nights": frozenset(),
        "logged_inprogress_nights": frozenset({"2025-03-20"}),
    }
    assert classify_session("2025-03-20", target_ev) == ("in_progress", "log", "2025-03-20")


def test_classify_session_logged_processed_wins_over_logged_inprogress():
    # A night present in both logged sets resolves to processed (checked first).
    target_ev = {
        "export_dates": [], "inprogress_dates": [],
        "logged_processed_nights": frozenset({"2025-03-20"}),
        "logged_inprogress_nights": frozenset({"2025-03-20"}),
    }
    assert classify_session("2025-03-20", target_ev) == ("processed", "log", "2025-03-20")


def test_classify_session_logged_night_ignores_date_bound_pools():
    # Even if a date-bound pool would also cover it, a logged night takes the
    # exact log attribution (evidence == "log", not "date-bound").
    target_ev = {
        "export_dates": ["2025-04-26"], "inprogress_dates": [],
        "logged_processed_nights": frozenset({"2025-03-20"}),
        "logged_inprogress_nights": frozenset(),
    }
    assert classify_session("2025-03-20", target_ev) == ("processed", "log", "2025-03-20")


# ── scan: monotonic upgrade rules ────────────────────────────────────────────

def test_scan_never_downgrades_processed(tmp_path):
    archive = tmp_path / "archive"
    # Only subs on disk — no processing evidence at all.
    touch(archive / "01_Deep Sky Objects" / "M 81" / "2026-02-19_FRA400_ZWOASI585MCPro"
          / "Lights" / "Light_0001.fit")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])
    set_processed_state(db, "s1", state="processed", processed_date="2026-05-01")

    transitions = scan(archive, LocalBackend(db))

    t = transitions[0]
    assert t.current_state == "processed"
    assert t.proposed_state == "unprocessed"
    assert t.change is False


def test_scan_never_changes_skipped(tmp_path):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])
    set_processed_state(db, "s1", state="skipped")

    transitions = scan(archive, LocalBackend(db))

    t = transitions[0]
    assert t.current_state == "skipped"
    assert t.proposed_state == "processed"  # would upgrade, but skipped is locked
    assert t.change is False


def test_scan_upgrades_unprocessed_to_processed(tmp_path):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    transitions = scan(archive, LocalBackend(db))

    t = transitions[0]
    assert t.proposed_state == "processed"
    assert t.change is True
    assert t.evidence_date == "2026-05-01"


def test_scan_upgrades_unprocessed_to_in_progress(tmp_path):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "master" / "masterLight_stack.xisf")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    transitions = scan(archive, LocalBackend(db))

    t = transitions[0]
    assert t.proposed_state == "in_progress"
    assert t.change is True


def test_scan_missing_target_folder_reports_unprocessed_no_change(tmp_path):
    archive = tmp_path / "archive"
    (archive / "01_Deep Sky Objects").mkdir(parents=True)
    db = _build_catalog(tmp_path, [_session("s1", target="Ghost Target", obs_date="2026-02-19")])

    transitions = scan(archive, LocalBackend(db))

    t = transitions[0]
    assert t.proposed_state == "unprocessed"
    assert t.change is False


def test_scan_idempotent_after_apply(tmp_path):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    first = scan(archive, LocalBackend(db))
    applied = apply(LocalBackend(db), first)
    assert applied == 1

    second = scan(archive, LocalBackend(db))
    assert all(not t.change for t in second)
    assert second[0].current_state == "processed"


def test_scan_tolerates_row_missing_processed_state_key(tmp_path):
    # A row dict without a 'processed_state' key (e.g. very old schema) must
    # be treated as 'unprocessed', not raise.
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "master" / "masterLight.xisf")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    transitions = scan(archive, LocalBackend(db))
    assert transitions[0].current_state == "unprocessed"


# ── scan: F2 log evidence / over-attribution fix ─────────────────────────────

def test_scan_log_evidence_prevents_date_bound_over_attribution(tmp_path):
    """The F2 headline fix: a has-export run's logged nights get exact
    'processed' attribution, but the run's export file is claimed by the run
    and excluded from the date-bound pool -- so an EARLIER, un-logged night
    is NOT swept up by the run's edit date the way F1 would have swept it.
    """
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    run_dir = target_dir / "_Processed" / "2025-04-26" / "Title"
    touch(
        run_dir / "logs" / "one.log",
        b"Light_M81_180.0s_Bin1_ISO3200_20250320-233000_14.0C_0001.fit\n"
        b"Light_M81_180.0s_Bin1_ISO3200_20250321-233000_14.0C_0001.fit\n",
    )
    touch(run_dir / "M81_final.tif")

    db = _build_catalog(tmp_path, [
        _session("s_a", obs_date="2025-03-20"),  # night A: logged
        _session("s_b", obs_date="2025-03-21"),  # night B: logged
        _session("s_c", obs_date="2025-03-15"),  # earlier, NOT in the log
    ])

    transitions = scan(archive, LocalBackend(db))
    by_id = {t.session_id: t for t in transitions}

    assert by_id["s_a"].proposed_state == "processed"
    assert by_id["s_a"].evidence == "log"
    assert by_id["s_a"].evidence_date == "2025-03-20"

    assert by_id["s_b"].proposed_state == "processed"
    assert by_id["s_b"].evidence == "log"

    # Without the claimed-subtree exclusion, s_c would be swept to
    # 'processed' by the run's export date-bound heuristic (its export is
    # dated 2025-04-26, which is >= 2025-03-15). It must NOT be.
    assert by_id["s_c"].proposed_state == "unprocessed"
    assert by_id["s_c"].change is False


def test_apply_sets_processed_date_from_log_evidence(tmp_path):
    archive = tmp_path / "archive"
    run_dir = archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2025-04-26" / "Title"
    touch(run_dir / "logs" / "one.log",
          b"Light_M81_180.0s_Bin1_ISO3200_20250320-233000_14.0C_0001.fit\n")
    touch(run_dir / "M81_final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2025-03-20")])

    transitions = scan(archive, LocalBackend(db))
    apply(LocalBackend(db), transitions)

    row = query_all_sessions(LocalBackend(db))[0]
    assert row["processed_state"] == "processed"
    assert row["processed_date"] == "2025-03-20"


# ── apply ─────────────────────────────────────────────────────────────────

def test_apply_only_applies_changed_transitions(tmp_path):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [
        _session("s1", obs_date="2026-02-19"),
        _session("s2", obs_date="2026-02-20"),
    ])
    set_processed_state(db, "s2", state="processed", processed_date="2020-01-01")

    transitions = scan(archive, LocalBackend(db))
    applied = apply(LocalBackend(db), transitions)

    assert applied == 1
    rows = {r["session_id"]: r for r in query_all_sessions(LocalBackend(db))}
    assert rows["s1"]["processed_state"] == "processed"
    assert rows["s1"]["processed_date"] == "2026-05-01"
    # s2 was already 'processed' and equal-rank -> untouched, date preserved.
    assert rows["s2"]["processed_date"] == "2020-01-01"


def test_apply_sets_processed_date(tmp_path):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "master" / "masterLight.xisf")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    transitions = scan(archive, LocalBackend(db))
    apply(LocalBackend(db), transitions)

    row = query_all_sessions(LocalBackend(db))[0]
    assert row["processed_state"] == "in_progress"
    assert row["processed_date"] is not None


def test_apply_returns_zero_when_nothing_changed(tmp_path):
    archive = tmp_path / "archive"
    (archive / "01_Deep Sky Objects" / "M 81").mkdir(parents=True)
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    transitions = scan(archive, LocalBackend(db))
    assert apply(LocalBackend(db), transitions) == 0


# ── CLI: dry-run vs --apply ──────────────────────────────────────────────────

def _cli_args(catalog: Path, archive: Path, apply_: bool) -> argparse.Namespace:
    return argparse.Namespace(catalog=str(catalog), archive=str(archive), apply=apply_)


def test_cli_dry_run_prints_proposed_changes_without_mutating(tmp_path, capsys):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    _scan_processed_run(_cli_args(db, archive, apply_=False))

    out = capsys.readouterr().out
    assert "unprocessed -> processed" in out
    assert "run with --apply to write" in out

    row = query_all_sessions(LocalBackend(db))[0]
    assert row["processed_state"] == "unprocessed"
    assert row["processed_date"] is None


def test_cli_apply_mutates_catalog(tmp_path, capsys):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    _scan_processed_run(_cli_args(db, archive, apply_=True))

    out = capsys.readouterr().out
    assert "Applied 1 change" in out

    row = query_all_sessions(LocalBackend(db))[0]
    assert row["processed_state"] == "processed"
    assert row["processed_date"] == "2026-05-01"


def test_cli_dry_run_shows_log_evidence_kind(tmp_path, capsys):
    archive = tmp_path / "archive"
    run_dir = archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2025-04-26" / "Title"
    touch(run_dir / "logs" / "one.log",
          b"Light_M81_180.0s_Bin1_ISO3200_20250320-233000_14.0C_0001.fit\n")
    touch(run_dir / "M81_final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2025-03-20")])

    _scan_processed_run(_cli_args(db, archive, apply_=False))

    out = capsys.readouterr().out
    assert "[log 2025-03-20]" in out


def test_cli_dry_run_shows_date_bound_evidence_kind(tmp_path, capsys):
    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "_Processed" / "2026-05-01" / "final.tif")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    _scan_processed_run(_cli_args(db, archive, apply_=False))

    out = capsys.readouterr().out
    assert "[date-bound 2026-05-01]" in out


def test_cli_requires_archive(tmp_path, monkeypatch):
    # Isolate from the real machine's darkroom.toml/env (which may set
    # archive_path) so the "no archive resolvable" path is actually exercised.
    monkeypatch.delenv("DARKROOM_ARCHIVE", raising=False)
    monkeypatch.setattr("darkroom.config.find_toml", lambda: {})
    db = _build_catalog(tmp_path, [_session("s1")])
    args = argparse.Namespace(catalog=str(db), archive=None, apply=False)
    with pytest.raises(SystemExit):
        _scan_processed_run(args)


def test_scan_never_calls_init_db(tmp_path, monkeypatch):
    """Dry-run path must be pure-read: it must never migrate the DB schema."""
    from darkroom import cataloger

    calls = []
    monkeypatch.setattr(cataloger, "init_db", lambda *a, **k: calls.append((a, k)))

    archive = tmp_path / "archive"
    touch(archive / "01_Deep Sky Objects" / "M 81" / "master" / "masterLight.xisf")
    db = _build_catalog(tmp_path, [_session("s1", obs_date="2026-02-19")])

    scan(archive, LocalBackend(db))

    assert calls == []
