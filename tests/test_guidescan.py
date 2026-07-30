"""Tests for darkroom.guidescan (F4) — guide-log segments matched to sessions.

Everything here is synthetic: logs written into tmp_path and a catalog built
with init_db. The autouse fixture in tests/conftest.py isolates HOME and the
DARKROOM_* env vars, so nothing can reach the real catalog or archive.

Log timestamps are ASIAir *local* time (Europe/Lisbon), session spans are UTC
— every fixture below is written in July, i.e. UTC+1, so a 22:00 local segment
is a 21:00 UTC one. That offset is the whole point of the matching code, so
the tests exercise it rather than sidestepping it.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from darkroom import guidescan
from darkroom.catalog_cli import _scan_guiding_run
from darkroom.catalog_client import LocalBackend
from darkroom.cataloger import init_db, upsert_session
from darkroom.guidelog import DEFAULT_SETTLE_EXCLUDE_SEC

_HEADER = (
    "Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,"
    "DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,"
    "YStep,StarMass,SNR,ErrorCode"
)
_SCALE = 6.45


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

def rows(n: int, *, t0: float = 16.0, step: float = 2.0,
         ra: float = 0.2, dec: float = -0.1) -> list[str]:
    """`n` guide-data CSV rows at `step`-second spacing starting at `t0`."""
    return [
        f"{i + 1},{t0 + i * step:.3f},\"Mount\",0.000,0.000,{ra:.3f},{dec:.3f},"
        f"0.000,0.000,0,,0,,,,1600,28.00,0"
        for i in range(n)
    ]


def segment(start: str, body: list[str], end: str | None) -> list[str]:
    """One `Guiding Begins`…`Guiding Ends` block with a realistic header."""
    lines = [
        f"Guiding Begins at {start}",
        f"Pixel scale = {_SCALE} arc-sec/px, Binning = 1, Focal length = 120 mm",
        "Camera = ZWO ASI120MM Mini, gain = 28, full size = 1280 x 960, have dark",
        "Exposure = 2000 ms",
        _HEADER,
        *body,
    ]
    if end is not None:
        lines.append(f"Guiding Ends at {end}")
    return lines


def write_log(logs_dir: Path, name: str, segments: list[list[str]]) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / name
    path.write_text(
        "PHD2 version 2.6.11, Log version 2.5. Log enabled at 2026-07-28 21:00:00\n"
        + "\n".join("\n".join(seg) for seg in segments)
        + "\n"
    )
    return path


def _session(session_id: str, start_utc: str, end_utc: str, *,
             target: str = "NGC 281", obs_date: str = "2026-07-28") -> dict:
    return {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": "L-Pro",
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 300.0,
        "focal_length": 400.0,
        "frame_count": 12,
        "total_integration_sec": 3600,
        "ra_deg": 13.19,
        "dec_deg": 60.65,
        "lights_path": f"01_Deep Sky Objects/{target}/{obs_date}_FRA400_ZWOASI585MCPro/Lights/L-Pro",
        "notes": "",
        "start_utc": start_utc,
        "end_utc": end_utc,
    }


def _build_catalog(tmp_path: Path, sessions: list[dict]) -> Path:
    db = tmp_path / "cat.db"
    init_db(db)
    for row in sessions:
        upsert_session(db, row)
    return db


def _guiding_rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM session_guiding")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# a session cleanly inside one segment
# ---------------------------------------------------------------------------

def test_session_inside_one_segment_pools_window_rows(tmp_path):
    logs_dir = tmp_path / "logs"
    # 60 rows at t=2..120; local 22:00 == 21:00 UTC (Europe/Lisbon, July).
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=2.0), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
    ])

    result = guidescan.scan(logs_dir, LocalBackend(db))

    assert len(result.matches) == 1
    m = result.matches[0]
    assert m.session_id == "s1"
    # 60 rows in window; t=2..14 (7 rows) sit inside the 15 s settle window.
    assert m.guide_frames == 53
    assert m.excluded_frames == 7
    assert m.rms_ra_arcsec == pytest.approx(0.2 * _SCALE)
    assert m.rms_dec_arcsec == pytest.approx(0.1 * _SCALE)
    assert m.rms_total_arcsec == pytest.approx((0.2**2 + 0.1**2) ** 0.5 * _SCALE)
    assert m.peak_arcsec == pytest.approx(m.rms_total_arcsec)
    assert m.pixel_scale_arcsec == pytest.approx(_SCALE)
    assert m.guide_camera == "ZWO ASI120MM Mini"
    assert m.guide_exposure_ms == 2000
    assert m.source_logs == ["PHD2_GuideLog_2026-07-28_220000.txt"]
    # Segment 21:00-22:00 UTC covers the session's whole hour.
    assert m.guided_sec == 3600
    assert m.coverage == pytest.approx(1.0)
    assert result.unmatched_sessions == []
    assert result.unmatched_logs == []


# ---------------------------------------------------------------------------
# a session spanning two segments — pooled, not averaged
# ---------------------------------------------------------------------------

def test_session_over_two_segments_pools_rows_rather_than_averaging_rms(tmp_path):
    """40 quiet rows + 10 terrible ones must weight by count, not by segment.

    Averaging the two segments' RMS values would report 3.55"; pooling the 50
    surviving rows reports 2.94". The difference is the whole reason F4 pools.
    """
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(40, ra=0.1, dec=0.0), "2026-07-28 22:10:00"),
        segment("2026-07-28 23:00:00", rows(10, ra=1.0, dec=0.0), "2026-07-28 23:05:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T23:00:00"),
    ])

    m = guidescan.scan(logs_dir, LocalBackend(db)).matches[0]

    assert m.guide_frames == 50
    pooled = ((40 * 0.1**2 + 10 * 1.0**2) / 50) ** 0.5 * _SCALE
    averaged = (0.1 * _SCALE + 1.0 * _SCALE) / 2
    assert m.rms_ra_arcsec == pytest.approx(pooled)
    assert m.rms_ra_arcsec != pytest.approx(averaged)
    assert m.peak_arcsec == pytest.approx(1.0 * _SCALE)
    # 600 s + 300 s of guiding inside a 2 h session.
    assert m.guided_sec == 900
    assert m.coverage == pytest.approx(900 / 7200)


# ---------------------------------------------------------------------------
# one segment spanning two adjacent sessions
# ---------------------------------------------------------------------------

def test_segment_spanning_two_sessions_splits_rows_by_window(tmp_path):
    logs_dir = tmp_path / "logs"
    # One two-hour segment, 22:00-00:00 local (21:00-23:00 UTC), a row a
    # minute. The first hour guides well, the second badly.
    body = (
        rows(59, t0=60.0, step=60.0, ra=0.1, dec=0.0)
        + rows(60, t0=3600.0, step=60.0, ra=1.0, dec=0.0)
    )
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", body, "2026-07-29 00:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("early", "2026-07-28T21:00:00", "2026-07-28T21:59:59"),
        _session("late", "2026-07-28T22:00:00", "2026-07-28T22:59:59"),
    ])

    by_id = {m.session_id: m for m in guidescan.scan(logs_dir, LocalBackend(db)).matches}

    assert set(by_id) == {"early", "late"}
    assert by_id["early"].guide_frames == 59
    assert by_id["early"].rms_ra_arcsec == pytest.approx(0.1 * _SCALE)
    assert by_id["late"].guide_frames == 60
    assert by_id["late"].rms_ra_arcsec == pytest.approx(1.0 * _SCALE)
    # Each session sees only its own hour of the segment, not both.
    assert by_id["early"].guided_sec == 3599
    assert by_id["late"].guided_sec == 3599


# ---------------------------------------------------------------------------
# nothing matched
# ---------------------------------------------------------------------------

def test_session_window_matching_nothing_writes_no_row(tmp_path):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("miss", "2026-07-20T21:00:00", "2026-07-20T23:00:00",
                 obs_date="2026-07-20"),
    ])

    result = guidescan.scan(logs_dir, LocalBackend(db))
    applied = guidescan.apply(LocalBackend(db), result)

    assert result.matches == []
    assert applied == 0
    assert [r["session_id"] for r in result.unmatched_sessions] == ["miss"]
    # The other half of the report: a log nobody claimed.
    assert result.unmatched_logs == ["PHD2_GuideLog_2026-07-28_220000.txt"]
    assert _guiding_rows(db) == []


def test_sessions_without_a_span_are_reported_not_matched(tmp_path):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [_session("nospan", None, None)])

    result = guidescan.scan(logs_dir, LocalBackend(db))

    assert result.matches == []
    assert [r["session_id"] for r in result.undated_sessions] == ["nospan"]
    assert result.unmatched_sessions == []


def test_too_few_surviving_rows_is_not_a_match(tmp_path):
    """A window catching the tail of someone else's segment gets no number."""
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=2.0), "2026-07-28 23:00:00"),
    ])
    # Window covers only the first 20 s of the segment: 10 rows, 7 of them
    # inside the settle exclusion.
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T21:00:20"),
    ])

    result = guidescan.scan(logs_dir, LocalBackend(db))

    assert result.matches == []
    assert [r["session_id"] for r in result.unmatched_sessions] == ["s1"]


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

def test_partial_log_gives_coverage_below_one(tmp_path):
    logs_dir = tmp_path / "logs"
    # One hour of guiding inside a two-hour session.
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=60.0, step=60.0),
                "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T23:00:00"),
    ])

    m = guidescan.scan(logs_dir, LocalBackend(db)).matches[0]

    assert m.guided_sec == 3600
    assert m.coverage == pytest.approx(0.5)


def test_duplicate_logs_cannot_push_coverage_above_one(tmp_path):
    """The same night archived twice is a union, not a sum."""
    logs_dir = tmp_path / "logs"
    seg = segment("2026-07-28 22:00:00", rows(60, t0=60.0, step=60.0),
                  "2026-07-28 23:00:00")
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [seg])
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000_copy.txt", [seg])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
    ])

    m = guidescan.scan(logs_dir, LocalBackend(db)).matches[0]

    assert m.guided_sec == 3600
    assert m.coverage == pytest.approx(1.0)
    assert len(m.source_logs) == 2


def test_chn_translations_are_skipped(tmp_path):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000_CHN.txt", [
        segment("2026-07-28 22:00:00", rows(60), "2026-07-28 23:00:00"),
    ])

    assert guidescan.load_segments(logs_dir) == []


# ---------------------------------------------------------------------------
# apply / idempotency
# ---------------------------------------------------------------------------

def test_apply_writes_one_row_per_session(tmp_path):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=2.0), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
    ])

    result = guidescan.scan(logs_dir, LocalBackend(db))
    assert guidescan.apply(LocalBackend(db), result) == 1

    written = _guiding_rows(db)
    assert len(written) == 1
    row = written[0]
    assert row["session_id"] == "s1"
    assert row["guide_frames"] == 53
    assert row["rms_total_arcsec"] == pytest.approx(
        (0.2**2 + 0.1**2) ** 0.5 * _SCALE
    )
    assert row["coverage"] == pytest.approx(1.0)
    assert json.loads(row["source_logs"]) == ["PHD2_GuideLog_2026-07-28_220000.txt"]
    assert row["computed_at"]


def test_rescan_replaces_rather_than_duplicating(tmp_path):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=2.0), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
    ])

    guidescan.apply(LocalBackend(db), guidescan.scan(logs_dir, LocalBackend(db)))
    first = _guiding_rows(db)
    guidescan.apply(LocalBackend(db), guidescan.scan(logs_dir, LocalBackend(db)))
    second = _guiding_rows(db)

    assert len(second) == 1
    for key in ("session_id", "rms_total_arcsec", "guide_frames", "coverage",
                "source_logs"):
        assert second[0][key] == first[0][key]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _args(db: Path, logs_dir: Path, *, apply: bool,
          settle_exclude: float = DEFAULT_SETTLE_EXCLUDE_SEC) -> argparse.Namespace:
    return argparse.Namespace(
        catalog=str(db), logs=str(logs_dir), apply=apply,
        settle_exclude=settle_exclude,
    )


def test_cli_dry_run_reports_without_writing(tmp_path, capsys):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=2.0), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
        _session("miss", "2026-07-01T21:00:00", "2026-07-01T22:00:00",
                 target="M 81", obs_date="2026-07-01"),
    ])

    _scan_guiding_run(_args(db, logs_dir, apply=False))

    out = capsys.readouterr().out
    assert "1 log(s), 1 guiding segment(s); 1 session(s) matched" in out
    assert "1 dated session(s) matched no guide data:" in out
    assert "miss" in out
    assert "s1" in out
    assert "1 session(s) would get guiding stats; run with --apply to write" in out
    assert _guiding_rows(db) == []


def test_cli_apply_writes_and_reports(tmp_path, capsys):
    logs_dir = tmp_path / "logs"
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(60, t0=2.0), "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
    ])

    _scan_guiding_run(_args(db, logs_dir, apply=True))

    out = capsys.readouterr().out
    assert "Applied guiding stats to 1 session(s)" in out
    assert len(_guiding_rows(db)) == 1


def test_cli_settle_exclude_is_plumbed_through_to_the_scan(tmp_path):
    """A wider settle window must drop more rows — the flag has to reach scan().

    Measured on real data: a good night barely moves (NGC 281 0.92" -> 0.90"
    from 15s to 120s) but a bad one moves a lot (M 45 15.04" -> 5.60"), which
    is exactly why it is tunable and why the default is pinned.
    """
    logs_dir = tmp_path / "logs"
    # A row a second for 200 s, so widening the window has rows to remove.
    write_log(logs_dir, "PHD2_GuideLog_2026-07-28_220000.txt", [
        segment("2026-07-28 22:00:00", rows(200, t0=1.0, step=1.0),
                "2026-07-28 23:00:00"),
    ])
    db = _build_catalog(tmp_path, [
        _session("s1", "2026-07-28T21:00:00", "2026-07-28T22:00:00"),
    ])

    _scan_guiding_run(_args(db, logs_dir, apply=True))
    default_frames = _guiding_rows(db)[0]["guide_frames"]

    _scan_guiding_run(_args(db, logs_dir, apply=True, settle_exclude=60.0))
    widened_frames = _guiding_rows(db)[0]["guide_frames"]

    assert default_frames == 200 - 14   # t=1..14 inside the default 15 s
    assert widened_frames == 200 - 59   # t=1..59 inside a 60 s window
    assert widened_frames < default_frames


def test_cli_missing_logs_dir_exits(tmp_path):
    db = _build_catalog(tmp_path, [])
    with pytest.raises(SystemExit):
        _scan_guiding_run(_args(db, tmp_path / "nope", apply=False))
