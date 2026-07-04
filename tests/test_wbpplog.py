"""Tests for darkroom.wbpplog (F2) — WBPP log-derived session->edit attribution."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from darkroom.wbpplog import _night_from_local_dt, collect_runs, parse_log_nights


def touch(p: Path, content: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def write_log(p: Path, lines: list[str]) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    return p


# ── _night_from_local_dt ──────────────────────────────────────────────────────

def test_night_from_local_dt_evening_capture_is_same_date():
    assert _night_from_local_dt(datetime(2024, 2, 28, 23, 30, 0)) == "2024-02-28"


def test_night_from_local_dt_after_midnight_capture_is_previous_date():
    assert _night_from_local_dt(datetime(2024, 2, 29, 2, 0, 0)) == "2024-02-28"


# ── parse_log_nights ───────────────────────────────────────────────────────────

def test_parse_log_nights_mixed_frame_types_only_light_counted(tmp_path):
    log = write_log(tmp_path / "one.log", [
        "[2024-02-28 17:01:01] [000] /old/staging/Light/NGC7000/"
        "Light_NGC7000_180.0s_Bin1_ISO3200_20240228-233000_14.0C_0019.fit",
        "[2024-02-28 17:05:01] [001] /old/staging/Dark/"
        "Dark_180.0s_Bin1_ISO3200_20240228-233500_14.0C_0001.fit",
        "[2024-02-28 17:06:01] [002] /old/staging/Flat/"
        "Flat_1.0s_Bin1_ISO3200_20240228-233600_14.0C_0002.fit",
    ])

    assert parse_log_nights(log) == {"2024-02-28"}


def test_parse_log_nights_noon_rule_boundary(tmp_path):
    log = write_log(tmp_path / "one.log", [
        # Evening frame: 23:30 -> that calendar date.
        "Light_NGC7000_180.0s_Bin1_ISO3200_20240228-233000_14.0C_0001.fit",
        # After-midnight frame: 02:00 -> previous calendar date (same night).
        "Light_NGC7000_180.0s_Bin1_ISO3200_20240229-020000_14.0C_0002.fit",
    ])

    assert parse_log_nights(log) == {"2024-02-28"}


def test_parse_log_nights_dedups_across_many_frames(tmp_path):
    lines = [
        f"Light_NGC7000_180.0s_Bin1_ISO3200_20240228-{h:02d}0000_14.0C_{i:04d}.fit"
        for i, h in enumerate([20, 21, 22, 23])
    ]
    log = write_log(tmp_path / "one.log", lines)

    assert parse_log_nights(log) == {"2024-02-28"}


def test_parse_log_nights_ignores_unparseable_tokens(tmp_path):
    log = write_log(tmp_path / "one.log", [
        "Light_NGC7000_no_datetime_here.fit",
        "Light_NGC7000_180.0s_Bin1_ISO3200_20240228-233000_14.0C_0001.fit",
        "some random line with no paths at all",
    ])

    assert parse_log_nights(log) == {"2024-02-28"}


def test_parse_log_nights_tolerates_quoted_and_bracketed_paths(tmp_path):
    log = write_log(tmp_path / "one.log", [
        '[000] "/old/staging/Light/NGC7000/'
        'Light_NGC7000_180.0s_Bin1_ISO3200_20240228-233000_14.0C_0001.fit"',
    ])

    assert parse_log_nights(log) == {"2024-02-28"}


def test_parse_log_nights_empty_for_no_light_frames(tmp_path):
    log = write_log(tmp_path / "one.log", [
        "Dark_180.0s_Bin1_ISO3200_20240228-233500_14.0C_0001.fit",
        "Flat_1.0s_Bin1_ISO3200_20240228-233600_14.0C_0002.fit",
    ])

    assert parse_log_nights(log) == set()


def test_parse_log_nights_tolerates_encoding_errors(tmp_path):
    log = tmp_path / "one.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(
        b"\xff\xfe garbage bytes\n"
        b"Light_NGC7000_180.0s_Bin1_ISO3200_20240228-233000_14.0C_0001.fit\n"
    )

    assert parse_log_nights(log) == {"2024-02-28"}


# ── collect_runs ───────────────────────────────────────────────────────────────

def test_collect_runs_no_export_is_in_progress_with_path_edit_date(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    run_dir = target_dir / "_Processed" / "2025-04-26" / "Title"
    write_log(run_dir / "logs" / "one.log", [
        "Light_M81_180.0s_Bin1_ISO3200_20250425-233000_14.0C_0001.fit",
    ])
    touch(run_dir / "master" / "masterLight.xisf")

    runs = collect_runs(target_dir, archive)

    assert len(runs) == 1
    r = runs[0]
    assert r.run_dir == run_dir
    assert r.has_export is False
    assert r.edit_date == "2025-04-26"
    assert r.nights == frozenset({"2025-04-25"})


def test_collect_runs_tif_under_run_marks_has_export(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    run_dir = target_dir / "_Processed" / "2025-04-26" / "Title"
    write_log(run_dir / "logs" / "one.log", [
        "Light_M81_180.0s_Bin1_ISO3200_20250425-233000_14.0C_0001.fit",
    ])
    touch(run_dir / "M81_final.tif")

    runs = collect_runs(target_dir, archive)

    assert len(runs) == 1
    assert runs[0].has_export is True


def test_collect_runs_logs_with_no_light_frames_not_a_run(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    run_dir = target_dir / "_Processed" / "2025-04-26" / "Title"
    write_log(run_dir / "logs" / "one.log", [
        "Dark_180.0s_Bin1_ISO3200_20250425-233000_14.0C_0001.fit",
    ])
    touch(run_dir / "master" / "masterLight.xisf")

    assert collect_runs(target_dir, archive) == []


def test_collect_runs_none_when_no_logs_dir(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    touch(target_dir / "master" / "masterLight.xisf")

    assert collect_runs(target_dir, archive) == []


def test_collect_runs_multiple_logs_union_nights(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    run_dir = target_dir / "_Processed" / "2025-04-26" / "Title"
    write_log(run_dir / "logs" / "one.log", [
        "Light_M81_180.0s_Bin1_ISO3200_20250320-233000_14.0C_0001.fit",
    ])
    write_log(run_dir / "logs" / "two.log", [
        "Light_M81_180.0s_Bin1_ISO3200_20250321-233000_14.0C_0001.fit",
    ])

    runs = collect_runs(target_dir, archive)

    assert len(runs) == 1
    assert runs[0].nights == frozenset({"2025-03-20", "2025-03-21"})


def test_collect_runs_edit_date_falls_back_to_mtime_when_no_dated_component(tmp_path):
    archive = tmp_path / "archive"
    target_dir = archive / "01_Deep Sky Objects" / "M 81"
    # No YYYY-MM-DD anywhere in the run's path.
    run_dir = target_dir / "SomeRun" / "Title"
    write_log(run_dir / "logs" / "one.log", [
        "Light_M81_180.0s_Bin1_ISO3200_20250425-233000_14.0C_0001.fit",
    ])

    runs = collect_runs(target_dir, archive)

    assert len(runs) == 1
    # Falls back to a real date (mtime of the log file itself), not None.
    assert runs[0].edit_date is not None
    import re
    assert re.match(r"\d{4}-\d{2}-\d{2}", runs[0].edit_date)
