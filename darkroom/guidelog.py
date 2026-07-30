"""darkroom.guidelog — parse PHD2 guide logs into per-segment guiding stats (F4).

Pure and stdlib-only: no catalog/DB access, no astropy, no network. Reads
guide logs (``PHD2_GuideLog_*.txt``) and never writes anything.

A log is a sequence of blocks. ``Guiding Begins at <ts>`` opens a guiding
segment whose header carries the pixel scale, guide camera and guide
exposure; ``Guiding Ends at <ts>`` closes it. ``Calibration Begins at``
opens a *different* kind of block whose CSV header is
``Direction,Step,dx,dy,x,y,Dist`` — its rows must never be read as guide
data, so it resets parser state instead.

Hazards this module handles, all confirmed present in the real corpus (106
logs, 2025-03-23 → 2026-07-28):

* 178 calibration blocks with the different column header (above).
* 60,633 ``"DROP"`` rows (column 3) — dropped guide frames, excluded from RMS.
  They also carry a non-zero ``ErrorCode``, so DROP is tested first and they
  are counted as dropped, not errored.
* 169 further rows with a non-zero ``ErrorCode`` (last column; values 2/3/4/6/7
  seen) — star-loss and friends, excluded from RMS.
* Lines are only data *after* the ``Frame,Time,mount,...`` header line.
* Pixel scale is uniform in this corpus (6.45 arc-sec/px) but is parsed per
  segment, never hardcoded.
* Log timestamps are ASIAir **local** time (Europe/Lisbon); FITS ``DATE-OBS``
  is UTC. Segment starts are made tz-aware so callers can convert via
  ``GuideSegment.row_utc``, not a fixed offset.
* Some logs end without a final ``Guiding Ends`` (3 end at ``Log closed at``);
  such a segment is closed at its last row.

Settling is excluded statistically, not flagged: on this rig ``Settling
failed`` outnumbers ``Settling complete`` 10,706 : 1,350, so a settle timeout
is the norm. ``stats`` simply drops rows within ``settle_exclude_sec`` of a
segment start or an ``INFO: DITHER`` line.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ASIAir wall-clock timezone. Log timestamps carry no offset, so segments are
# localised with this and converted to UTC on demand.
LOCAL_TZ = ZoneInfo("Europe/Lisbon")

_TS_FMT = "%Y-%m-%d %H:%M:%S"

_BEGIN_PREFIX = "Guiding Begins at "
_END_PREFIX = "Guiding Ends at "
_CALIBRATION_PREFIX = "Calibration Begins at"
_DATA_HEADER_PREFIX = "Frame,Time,mount,"
_DITHER_PREFIX = "INFO: DITHER"

_PIXEL_SCALE_RE = re.compile(r"^Pixel scale = ([\d.]+)")
_CAMERA_RE = re.compile(r"^Camera = ([^,]+)")
_EXPOSURE_RE = re.compile(r"^Exposure = (\d+) ms")

# Guide-data CSV layout (0-indexed): Frame,Time,mount,dx,dy,RARawDistance,
# DECRawDistance,...,ErrorCode. A row shorter than this is malformed.
_COL_TIME = 1
_COL_MOUNT = 2
_COL_RA_RAW = 5
_COL_DEC_RAW = 6
_COL_ERROR_CODE = 17
_MIN_COLS = 18

# Defaults for stats(); a segment with fewer usable rows is not worth a number.
DEFAULT_SETTLE_EXCLUDE_SEC = 15.0
DEFAULT_MIN_ROWS = 30


@dataclass(frozen=True)
class GuideRow:
    """One accepted guide frame: seconds since segment start, raw error in px."""

    t_offset_sec: float
    ra_px: float
    dec_px: float


@dataclass
class GuideStats:
    """Guiding quality for a set of rows. All distances in arcsec."""

    rows_used: int
    rows_excluded: int
    rms_ra_arcsec: float
    rms_dec_arcsec: float
    rms_total_arcsec: float
    peak_arcsec: float
    p95_arcsec: float


@dataclass
class GuideSegment:
    """One ``Guiding Begins``…``Guiding Ends`` block of a PHD2 log."""

    start_local: datetime
    end_local: datetime | None = None
    pixel_scale_arcsec: float | None = None
    guide_camera: str | None = None
    exposure_ms: int | None = None
    rows: list[GuideRow] = field(default_factory=list)
    # Seconds-since-start of each INFO: DITHER line, for settle exclusion.
    dither_offsets: list[float] = field(default_factory=list)
    dropped: int = 0
    errored: int = 0
    # True when the log ended without a `Guiding Ends` line for this segment.
    truncated: bool = False

    def row_utc(self, row: GuideRow) -> datetime:
        """UTC timestamp of ``row`` (local start + offset, then converted)."""
        return (self.start_local + timedelta(seconds=row.t_offset_sec)).astimezone(
            timezone.utc
        )

    @property
    def duration_sec(self) -> float:
        """Wall-clock length of the segment, 0.0 if it never closed."""
        if self.end_local is None:
            return 0.0
        return (self.end_local - self.start_local).total_seconds()


def parse_log(path: str | Path, tz: ZoneInfo = LOCAL_TZ) -> list[GuideSegment]:
    """Parse a PHD2 guide log into its guiding segments.

    Segments are framed by ``Guiding Begins``/``Guiding Ends``; a
    ``Calibration Begins at`` line discards any open segment (its rows use a
    different column layout). A final segment left open by a truncated log is
    closed at its last row and marked ``truncated``; one with no rows at all
    is dropped. Read tolerant of encoding errors, like the rest of the suite.
    """
    segments: list[GuideSegment] = []
    current: GuideSegment | None = None
    in_data = False

    with open(path, errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")

            if line.startswith(_BEGIN_PREFIX):
                start = _parse_timestamp(line[len(_BEGIN_PREFIX):], tz)
                current = GuideSegment(start_local=start) if start else None
                in_data = False
                continue

            # Calibration rows are Direction,Step,dx,dy,x,y,Dist — never guide
            # data. Reset rather than risk parsing them as such.
            if line.startswith(_CALIBRATION_PREFIX):
                current, in_data = None, False
                continue

            if current is None:
                continue

            if line.startswith(_END_PREFIX):
                current.end_local = _parse_timestamp(line[len(_END_PREFIX):], tz)
                segments.append(current)
                current, in_data = None, False
                continue

            if not in_data:
                if _read_header_field(current, line):
                    continue
                if line.startswith(_DATA_HEADER_PREFIX):
                    in_data = True
                    continue

            if line.startswith(_DITHER_PREFIX):
                # PHD2 does not timestamp INFO lines, so anchor the dither to
                # the last accepted row (start of segment if there is none).
                last = current.rows[-1].t_offset_sec if current.rows else 0.0
                current.dither_offsets.append(last)
                continue

            if in_data and line[:1].isdigit():
                _read_data_row(current, line)

    if current is not None and current.rows:
        current.end_local = current.start_local + timedelta(
            seconds=current.rows[-1].t_offset_sec
        )
        current.truncated = True
        segments.append(current)

    return segments


def stats(
    rows: list[GuideRow],
    pixel_scale_arcsec: float | None,
    dither_offsets: list[float] | None = None,
    settle_exclude_sec: float = DEFAULT_SETTLE_EXCLUDE_SEC,
    min_rows: int = DEFAULT_MIN_ROWS,
) -> GuideStats | None:
    """RMS/peak/p95 in arcsec, or None if too few rows survive exclusion.

    Rows within ``settle_exclude_sec`` of the segment start or of any dither
    are dropped: guiding is still settling there and the excursions are not
    representative of the night. ``pixel_scale_arcsec`` converts px → arcsec;
    a missing scale falls back to 1.0 (i.e. results stay in px).
    """
    scale = pixel_scale_arcsec or 1.0
    marks = [0.0] + list(dither_offsets or [])
    usable = [
        row
        for row in rows
        if not any(m <= row.t_offset_sec < m + settle_exclude_sec for m in marks)
    ]
    if len(usable) < min_rows:
        return None

    ra = [row.ra_px * scale for row in usable]
    dec = [row.dec_px * scale for row in usable]
    total = [math.hypot(a, d) for a, d in zip(ra, dec)]
    rms_ra, rms_dec = _rms(ra), _rms(dec)
    return GuideStats(
        rows_used=len(usable),
        rows_excluded=len(rows) - len(usable),
        rms_ra_arcsec=rms_ra,
        rms_dec_arcsec=rms_dec,
        rms_total_arcsec=math.hypot(rms_ra, rms_dec),
        peak_arcsec=max(total),
        p95_arcsec=sorted(total)[int(len(total) * 0.95)],
    )


def segment_stats(
    segment: GuideSegment,
    settle_exclude_sec: float = DEFAULT_SETTLE_EXCLUDE_SEC,
    min_rows: int = DEFAULT_MIN_ROWS,
) -> GuideStats | None:
    """``stats`` applied to one segment's own rows, scale and dithers."""
    return stats(
        segment.rows,
        segment.pixel_scale_arcsec,
        segment.dither_offsets,
        settle_exclude_sec=settle_exclude_sec,
        min_rows=min_rows,
    )


def _parse_timestamp(text: str, tz: ZoneInfo) -> datetime | None:
    """Parse a ``YYYY-MM-DD HH:MM:SS`` log timestamp as local time."""
    try:
        return datetime.strptime(text.strip(), _TS_FMT).replace(tzinfo=tz)
    except ValueError:
        return None


def _read_header_field(segment: GuideSegment, line: str) -> bool:
    """Fill pixel scale / camera / exposure from a segment header line."""
    match = _PIXEL_SCALE_RE.match(line)
    if match:
        segment.pixel_scale_arcsec = float(match.group(1))
        return True
    match = _CAMERA_RE.match(line)
    if match:
        segment.guide_camera = match.group(1).strip() or None
        return True
    match = _EXPOSURE_RE.match(line)
    if match:
        segment.exposure_ms = int(match.group(1))
        return True
    return False


def _read_data_row(segment: GuideSegment, line: str) -> None:
    """Accept one CSV guide row, or count it as dropped/errored."""
    fields = line.split(",")
    if len(fields) < _MIN_COLS:
        return
    if fields[_COL_MOUNT].strip('"') == "DROP":
        segment.dropped += 1
        return
    try:
        error_code = int(fields[_COL_ERROR_CODE] or 0)
    except ValueError:
        error_code = 0
    if error_code != 0:
        segment.errored += 1
        return
    try:
        segment.rows.append(
            GuideRow(
                t_offset_sec=float(fields[_COL_TIME]),
                ra_px=float(fields[_COL_RA_RAW]),
                dec_px=float(fields[_COL_DEC_RAW]),
            )
        )
    except ValueError:
        return


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values))
