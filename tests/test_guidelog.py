"""Tests for darkroom.guidelog (F4) — PHD2 guide log parsing + guiding stats.

The fixture log below is synthetic but mirrors the real corpus's hazards: a
calibration block with its own column layout, a DROP row, a non-zero
ErrorCode row, a dither, and a final segment truncated by the log ending.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from darkroom.guidelog import (
    LOCAL_TZ,
    GuideRow,
    parse_log,
    segment_stats,
    stats,
)

_HEADER = "Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode"


def data_row(frame: int, t: float, ra: float, dec: float, mount: str = '"Mount"',
             error_code: int = 0) -> str:
    """One guide-data CSV row with the real column count (18)."""
    return (
        f"{frame},{t:.3f},{mount},0.000,0.000,{ra:.3f},{dec:.3f},"
        f"0.000,0.000,0,,0,,,,1600,28.00,{error_code}"
    )


def segment_lines(start: str, rows: list[str], end: str | None,
                  scale: str = "6.45") -> list[str]:
    lines = [
        f"Guiding Begins at {start}",
        f"Pixel scale = {scale} arc-sec/px, Binning = 1, Focal length = 120 mm",
        "Camera = ZWO ASI120MM Mini, gain = 28, full size = 1280 x 960, have dark",
        "Exposure = 2000 ms",
        "Lock position = 1146.965, 739.067, Star position = 1146.895, 739.175",
        _HEADER,
        *rows,
    ]
    if end is not None:
        lines.append(f"Guiding Ends at {end}")
    return lines


def write_fixture(tmp_path: Path) -> Path:
    """A log with: a calibration block, a clean segment (DROP + ErrorCode +
    dither inside it), and a truncated final segment."""
    # Segment 1: 60 accepted rows at 2 s spacing, dither at t=60.
    seg1_rows: list[str] = []
    frame = 1
    for i in range(30):
        seg1_rows.append(data_row(frame, 2.0 * (i + 1), 0.20, -0.10))
        frame += 1
    seg1_rows.append(data_row(frame, 61.0, 9.99, 9.99, mount='"DROP"'))
    frame += 1
    seg1_rows.append(data_row(frame, 62.0, 9.99, 9.99, error_code=3))
    frame += 1
    seg1_rows.append("INFO: DITHER by 6.804, -2.112, new lock pos = 1052.6, 614.5")
    for i in range(30):
        seg1_rows.append(data_row(frame, 64.0 + 2.0 * i, 0.30, -0.20))
        frame += 1

    seg2_rows = [data_row(i + 1, 2.0 * (i + 1), 0.10, 0.10) for i in range(40)]

    lines = [
        "PHD2 version, Log version 2.5. Log enabled at 2026-07-28 22:00:00",
        "",
        # Calibration block: different columns, must not be read as guide data.
        "Calibration Begins at 2026-07-28 22:05:00",
        "Pixel scale = 6.45 arc-sec/px, Binning = 1, Focal length = 120 mm",
        "Direction,Step,dx,dy,x,y,Dist",
        'West,1,-1.000,2.000,500.000,600.000,2.236',
        'West,2,-2.000,4.000,499.000,602.000,4.472',
        "Calibration complete",
        "",
        *segment_lines("2026-07-28 22:10:00", seg1_rows, "2026-07-28 22:14:00"),
        "",
        # Truncated: no `Guiding Ends`, log just stops.
        *segment_lines("2026-07-28 22:20:00", seg2_rows, None),
    ]
    path = tmp_path / "PHD2_GuideLog_2026-07-28_220000.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


# ── parse_log ────────────────────────────────────────────────────────────────

def test_parse_log_returns_only_guiding_segments(tmp_path):
    segments = parse_log(write_fixture(tmp_path))

    assert len(segments) == 2
    assert segments[0].start_local == datetime(2026, 7, 28, 22, 10, tzinfo=LOCAL_TZ)
    assert segments[0].end_local == datetime(2026, 7, 28, 22, 14, tzinfo=LOCAL_TZ)


def test_parse_log_ignores_calibration_rows(tmp_path):
    segments = parse_log(write_fixture(tmp_path))

    # Calibration's Direction,Step,... rows start with a letter and sit in a
    # block that reset the parser — they can reach neither segment.
    assert all(row.ra_px in (0.20, 0.30, 0.10) for seg in segments for row in seg.rows)


def test_parse_log_reads_header_fields_per_segment(tmp_path):
    segment = parse_log(write_fixture(tmp_path))[0]

    assert segment.pixel_scale_arcsec == 6.45
    assert segment.guide_camera == "ZWO ASI120MM Mini"
    assert segment.exposure_ms == 2000


def test_parse_log_counts_drop_and_error_rows_without_using_them(tmp_path):
    segment = parse_log(write_fixture(tmp_path))[0]

    assert segment.dropped == 1
    assert segment.errored == 1
    assert len(segment.rows) == 60
    assert all(abs(row.ra_px) < 1.0 for row in segment.rows)


def test_parse_log_records_dither_offset_at_last_row(tmp_path):
    segment = parse_log(write_fixture(tmp_path))[0]

    # DROP/errored rows never become the anchor; the last accepted row is t=60.
    assert segment.dither_offsets == [60.0]


def test_parse_log_closes_truncated_final_segment_at_last_row(tmp_path):
    segment = parse_log(write_fixture(tmp_path))[1]

    assert segment.truncated is True
    assert len(segment.rows) == 40
    # Last row is at t=80 s after a 22:20:00 start.
    assert segment.end_local == datetime(2026, 7, 28, 22, 21, 20, tzinfo=LOCAL_TZ)
    assert segment.duration_sec == 80.0


def test_parse_log_drops_truncated_segment_with_no_rows(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text(
        "\n".join(
            segment_lines("2026-07-28 22:10:00", [], "2026-07-28 22:14:00")
            + ["Guiding Begins at 2026-07-28 22:20:00", "Log closed at 2026-07-28 22:20:05"]
        )
        + "\n"
    )

    assert len(parse_log(path)) == 1


def test_parse_log_skips_malformed_and_short_rows(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text(
        "\n".join(
            segment_lines(
                "2026-07-28 22:10:00",
                ["1,2.000,\"Mount\",0.1,0.2", data_row(2, 4.0, 0.1, 0.2)],
                "2026-07-28 22:14:00",
            )
        )
        + "\n"
    )

    segment = parse_log(path)[0]
    assert len(segment.rows) == 1
    assert segment.dropped == 0 and segment.errored == 0


def test_parse_log_ignores_data_lines_before_the_frame_header(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text(
        "\n".join(
            [
                "Guiding Begins at 2026-07-28 22:10:00",
                "Pixel scale = 6.45 arc-sec/px, Binning = 1, Focal length = 120 mm",
                # A stray numeric line before the CSV header is not data.
                data_row(1, 2.0, 5.0, 5.0),
                _HEADER,
                data_row(2, 4.0, 0.1, 0.2),
                "Guiding Ends at 2026-07-28 22:14:00",
            ]
        )
        + "\n"
    )

    segment = parse_log(path)[0]
    assert [row.t_offset_sec for row in segment.rows] == [4.0]


def test_row_utc_converts_from_asiair_local_time(tmp_path):
    segment = parse_log(write_fixture(tmp_path))[0]

    # 22:10:00 Europe/Lisbon in July is UTC+1, first row is 2 s in.
    assert segment.row_utc(segment.rows[0]) == datetime(
        2026, 7, 28, 21, 10, 2, tzinfo=timezone.utc
    )


def test_parse_log_honours_explicit_timezone(tmp_path):
    segments = parse_log(write_fixture(tmp_path), tz=ZoneInfo("UTC"))

    assert segments[0].start_local == datetime(2026, 7, 28, 22, 10, tzinfo=timezone.utc)


# ── stats ────────────────────────────────────────────────────────────────────

def test_stats_returns_none_below_min_rows():
    rows = [GuideRow(t_offset_sec=20.0 + i, ra_px=0.1, dec_px=0.1) for i in range(29)]

    assert stats(rows, 6.45) is None


def test_stats_converts_pixels_to_arcsec():
    rows = [GuideRow(t_offset_sec=20.0 + i, ra_px=0.2, dec_px=-0.1) for i in range(40)]

    result = stats(rows, 6.45)
    assert result is not None
    assert result.rows_used == 40 and result.rows_excluded == 0
    # Tolerance, not equality: RMS goes through sqrt(sum(x**2)/n), so even a
    # run of identical rows lands an ulp off the closed form on some libms.
    assert result.rms_ra_arcsec == pytest.approx(0.2 * 6.45)
    assert result.rms_dec_arcsec == pytest.approx(0.1 * 6.45)
    expected_total = ((0.2 * 6.45) ** 2 + (0.1 * 6.45) ** 2) ** 0.5
    assert abs(result.rms_total_arcsec - expected_total) < 1e-9
    assert abs(result.peak_arcsec - result.p95_arcsec) < 1e-9


def test_stats_excludes_settle_window_after_start():
    # 10 wild rows inside the first 15 s, then 40 quiet ones.
    rows = [GuideRow(t_offset_sec=float(i), ra_px=10.0, dec_px=10.0) for i in range(10)]
    rows += [GuideRow(t_offset_sec=20.0 + i, ra_px=0.1, dec_px=0.1) for i in range(40)]

    result = stats(rows, 1.0)
    assert result is not None
    assert result.rows_used == 40 and result.rows_excluded == 10
    assert abs(result.peak_arcsec - (0.1 * 2 ** 0.5)) < 1e-9


def test_stats_excludes_settle_window_after_dither():
    rows = [GuideRow(t_offset_sec=20.0 + i, ra_px=0.1, dec_px=0.1) for i in range(60)]
    # Dither at t=30 knocks out rows 30.0 <= t < 45.0 (15 of them).
    result = stats(rows, 1.0, [30.0])

    assert result is not None
    assert result.rows_used == 45 and result.rows_excluded == 15


def test_stats_without_pixel_scale_stays_in_pixels():
    rows = [GuideRow(t_offset_sec=20.0 + i, ra_px=0.2, dec_px=0.0) for i in range(40)]

    result = stats(rows, None)
    assert result is not None
    assert abs(result.rms_ra_arcsec - 0.2) < 1e-9


def test_segment_stats_uses_segment_scale_and_dithers(tmp_path):
    segment = parse_log(write_fixture(tmp_path))[0]

    result = segment_stats(segment)
    assert result is not None
    # 60 accepted rows: 7 fall in the 15 s start window (t=2..14) and 7 in the
    # dither window (t=60, 64..74).
    assert result.rows_used == 46 and result.rows_excluded == 14
    # Survivors are a mix of the 0.20 px and 0.30 px halves, scaled to arcsec.
    assert 0.2 * 6.45 < result.rms_ra_arcsec < 0.3 * 6.45
    assert 0.1 * 6.45 < result.rms_dec_arcsec < 0.2 * 6.45
    assert abs(result.peak_arcsec - (0.3 ** 2 + 0.2 ** 2) ** 0.5 * 6.45) < 1e-9
