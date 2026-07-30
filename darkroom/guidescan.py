"""darkroom.guidescan — match PHD2 guide-log segments to sessions by time (F4).

Mirrors `darkroom.procscan`'s split: `scan` reads logs + the catalog and
returns objects, writing nothing; `apply` takes those objects and writes them
through a `darkroom.catalog_client.CatalogBackend`.

**Matching is purely temporal.** A session's stored UTC wall-clock span
(`sessions.start_utc`/`end_utc`) is intersected with the guide segments parsed
out of every log. Log target names are never consulted: they are messy
(`NGC7000` vs `NGC 7000`, `FOV` framing blocks) and, more importantly,
sometimes simply wrong at acquisition time — the catalog is the corrected
truth, so the only thing a log is trusted for is *when* it was guiding.

Two details that decide whether the numbers mean anything:

* **Settle exclusion is per segment.** A segment's dither offsets are seconds
  since *that segment's* start, so rows must be filtered against their own
  segment's marks before anything is pooled.
* **RMS is pooled, never averaged.** A session spanning several segments gets
  one RMS over the union of the surviving rows — averaging per-segment RMS
  values would weight a 3-minute segment like a 3-hour one. Rows are converted
  px -> arcsec with their own segment's pixel scale before pooling, so a scale
  change mid-night stays correct.

`coverage` (guided seconds / session wall span) is the guard against a partial
log looking authoritative. Sessions that match nothing and logs that match no
session are *reported*, never guessed at: a whole date range failing to match
means the ASIAir clock or timezone was not what `guidelog.LOCAL_TZ` assumes,
which is a fact for the user to act on, not something to silently correct.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from darkroom import guidelog
from darkroom.catalog import query_all_sessions
from darkroom.logs import is_chn

#: Guide logs only; `*_CHN.txt` are Chinese translations of the same content.
GUIDE_LOG_GLOB = "PHD2_GuideLog_*.txt"


@dataclass
class LoadedSegment:
    """One parsed guide segment plus the UTC timeline needed to match it."""

    log_name: str
    segment: guidelog.GuideSegment
    start_utc: datetime
    end_utc: datetime
    #: UTC timestamp of each accepted row, parallel to ``segment.rows``.
    row_times: list[datetime]


@dataclass
class SessionGuiding:
    """Pooled guiding stats for one session — one `session_guiding` row.

    `target`/`obs_date` are carried for reporting only; `as_row` emits exactly
    the table's columns.
    """

    session_id: str
    target: str
    obs_date: str
    rms_ra_arcsec: float
    rms_dec_arcsec: float
    rms_total_arcsec: float
    peak_arcsec: float
    p95_arcsec: float
    guide_frames: int
    excluded_frames: int
    dropped_frames: int
    star_lost_events: int
    dither_count: int
    guided_sec: int
    coverage: float | None
    pixel_scale_arcsec: float | None
    guide_camera: str | None
    guide_exposure_ms: int | None
    source_logs: list[str]

    def as_row(self) -> dict:
        """The `session_guiding` row this represents, `source_logs` as JSON."""
        return {
            "session_id": self.session_id,
            "rms_ra_arcsec": self.rms_ra_arcsec,
            "rms_dec_arcsec": self.rms_dec_arcsec,
            "rms_total_arcsec": self.rms_total_arcsec,
            "peak_arcsec": self.peak_arcsec,
            "p95_arcsec": self.p95_arcsec,
            "guide_frames": self.guide_frames,
            "excluded_frames": self.excluded_frames,
            "dropped_frames": self.dropped_frames,
            "star_lost_events": self.star_lost_events,
            "dither_count": self.dither_count,
            "guided_sec": self.guided_sec,
            "coverage": self.coverage,
            "pixel_scale_arcsec": self.pixel_scale_arcsec,
            "guide_camera": self.guide_camera,
            "guide_exposure_ms": self.guide_exposure_ms,
            "source_logs": json.dumps(self.source_logs),
        }


@dataclass
class ScanResult:
    """Everything a scan found — matches plus both sides of what didn't match."""

    matches: list[SessionGuiding] = field(default_factory=list)
    #: Sessions with a span whose window held no usable guide rows.
    unmatched_sessions: list[dict] = field(default_factory=list)
    #: Sessions with no start_utc/end_utc — not candidates at all (run
    #: `darkroom catalog backfill-times` first).
    undated_sessions: list[dict] = field(default_factory=list)
    #: Log basenames that contributed rows to no session.
    unmatched_logs: list[str] = field(default_factory=list)
    log_count: int = 0
    segment_count: int = 0


def load_segments(logs_dir: Path) -> list[LoadedSegment]:
    """Parse every guide log in `logs_dir` into UTC-timed segments.

    A segment left without a usable end timestamp (a malformed `Guiding Ends`
    line) is closed at its last row, matching how `guidelog` treats a
    truncated final segment. Segments with no accepted rows are dropped —
    they can neither contribute stats nor meaningfully claim wall time.
    """
    loaded: list[LoadedSegment] = []
    for path in sorted(Path(logs_dir).glob(GUIDE_LOG_GLOB)):
        if is_chn(path.name):
            continue
        for segment in guidelog.parse_log(path):
            if not segment.rows:
                continue
            row_times = [segment.row_utc(row) for row in segment.rows]
            start_utc = segment.start_local.astimezone(timezone.utc)
            end_utc = (
                segment.end_local.astimezone(timezone.utc)
                if segment.end_local is not None
                else row_times[-1]
            )
            loaded.append(
                LoadedSegment(
                    log_name=path.name,
                    segment=segment,
                    start_utc=start_utc,
                    end_utc=max(end_utc, start_utc),
                    row_times=row_times,
                )
            )
    return loaded


def scan(
    logs_dir: Path,
    backend,
    *,
    settle_exclude_sec: float = guidelog.DEFAULT_SETTLE_EXCLUDE_SEC,
    min_rows: int = guidelog.DEFAULT_MIN_ROWS,
) -> ScanResult:
    """Reduce every guide log against every dated session. Writes nothing.

    `backend` is a `darkroom.catalog_client.CatalogBackend`; sessions come
    from `darkroom.catalog.query_all_sessions`. Only the logs directory is
    read from disk — the archive is never touched.
    """
    segments = load_segments(logs_dir)
    result = ScanResult(
        log_count=len({s.log_name for s in segments}),
        segment_count=len(segments),
    )

    contributing: set[str] = set()
    for session in query_all_sessions(backend):
        start = _parse_utc(session.get("start_utc"))
        end = _parse_utc(session.get("end_utc"))
        if start is None or end is None:
            result.undated_sessions.append(session)
            continue

        match = _reduce_session(
            session, start, end, segments,
            settle_exclude_sec=settle_exclude_sec, min_rows=min_rows,
        )
        if match is None:
            result.unmatched_sessions.append(session)
            continue
        result.matches.append(match)
        contributing.update(match.source_logs)

    result.unmatched_logs = sorted(
        {s.log_name for s in segments} - contributing
    )
    return result


def apply(backend, result: ScanResult) -> int:
    """Write every match via `backend.upsert_session_guiding`. Returns the count.

    The write is INSERT OR REPLACE, so a re-scan of unchanged logs leaves the
    same rows in place rather than accumulating duplicates.
    """
    for match in result.matches:
        backend.upsert_session_guiding(match.as_row())
    return len(result.matches)


def _reduce_session(
    session: dict,
    start: datetime,
    end: datetime,
    segments: list[LoadedSegment],
    *,
    settle_exclude_sec: float,
    min_rows: int,
) -> SessionGuiding | None:
    """Pool one session's in-window rows across segments into a single row.

    Returns None when too few rows survive — a session whose window caught a
    handful of frames from the tail of some other night's segment does not
    deserve a number.
    """
    pooled: list[guidelog.GuideRow] = []
    excluded = 0
    dropped = 0
    star_lost = 0
    dithers = 0
    overlaps: list[tuple[datetime, datetime]] = []
    logs: set[str] = set()
    # Keyed by the segment's optics/camera identity, weighted by pooled rows,
    # so a scale disagreement resolves to whichever covers the most data.
    scales: Counter = Counter()

    for loaded in segments:
        segment = loaded.segment
        overlap_start = max(loaded.start_utc, start)
        overlap_end = min(loaded.end_utc, end)
        if overlap_end > overlap_start:
            overlaps.append((overlap_start, overlap_end))

        in_window = [
            row
            for row, when in zip(segment.rows, loaded.row_times)
            if start <= when <= end
        ]
        if not in_window:
            continue

        # Settle exclusion is per segment: dither offsets (and the segment
        # start itself) are seconds since *this* segment began, so filtering
        # has to happen before anything is pooled.
        marks = [0.0] + list(segment.dither_offsets)
        usable = [
            row
            for row in in_window
            if not any(m <= row.t_offset_sec < m + settle_exclude_sec for m in marks)
        ]
        excluded += len(in_window) - len(usable)

        scale = segment.pixel_scale_arcsec or 1.0
        pooled.extend(
            guidelog.GuideRow(
                t_offset_sec=row.t_offset_sec,
                ra_px=row.ra_px * scale,
                dec_px=row.dec_px * scale,
            )
            for row in usable
        )

        # DROP/error rows carry no usable timestamp (guidelog only counts
        # them), so a segment straddling two sessions has its counts split by
        # the share of its accepted rows that landed in this window. An
        # approximation, and flagged as one — the alternative is charging both
        # sessions the full count.
        share = len(in_window) / len(segment.rows)
        dropped += round(segment.dropped * share)
        star_lost += round(segment.errored * share)

        # Same wall-clock -> UTC arithmetic as GuideSegment.row_utc, so a
        # dither and the rows around it can never land on opposite sides of
        # the window boundary.
        dithers += sum(
            1
            for offset in segment.dither_offsets
            if start
            <= (segment.start_local + timedelta(seconds=offset)).astimezone(timezone.utc)
            <= end
        )

        if usable:
            logs.add(loaded.log_name)
            scales[
                (segment.pixel_scale_arcsec, segment.guide_camera, segment.exposure_ms)
            ] += len(usable)

    # settle_exclude_sec=0 because exclusion already happened per segment
    # above; this call is here to reuse guidelog's RMS/peak/p95 definitions
    # rather than restate them. The rows are already in arcsec, hence scale 1.
    pooled_stats = guidelog.stats(
        pooled, 1.0, None, settle_exclude_sec=0.0, min_rows=min_rows
    )
    if pooled_stats is None:
        return None

    guided_sec = int(round(_union_seconds(overlaps)))
    span_sec = (end - start).total_seconds()
    (pixel_scale, camera, exposure_ms), _ = scales.most_common(1)[0]

    return SessionGuiding(
        session_id=session["session_id"],
        target=session.get("target") or "",
        obs_date=session.get("obs_date") or "",
        rms_ra_arcsec=pooled_stats.rms_ra_arcsec,
        rms_dec_arcsec=pooled_stats.rms_dec_arcsec,
        rms_total_arcsec=pooled_stats.rms_total_arcsec,
        peak_arcsec=pooled_stats.peak_arcsec,
        p95_arcsec=pooled_stats.p95_arcsec,
        guide_frames=pooled_stats.rows_used,
        excluded_frames=excluded,
        dropped_frames=dropped,
        star_lost_events=star_lost,
        dither_count=dithers,
        guided_sec=guided_sec,
        coverage=(guided_sec / span_sec) if span_sec > 0 else None,
        pixel_scale_arcsec=pixel_scale,
        guide_camera=camera,
        guide_exposure_ms=exposure_ms,
        source_logs=sorted(logs),
    )


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    """Total wall time covered by `intervals`, counting overlaps once.

    Two logs archived from the same night (or two segments running long) must
    not be able to push coverage above 1.0, so this is a union, not a sum.
    """
    total = 0.0
    reached: datetime | None = None
    for start, end in sorted(intervals):
        if reached is None or start > reached:
            total += (end - start).total_seconds()
            reached = end
        elif end > reached:
            total += (end - reached).total_seconds()
            reached = end
    return total


def _parse_utc(value) -> datetime | None:
    """Parse a stored `start_utc`/`end_utc` into an aware UTC datetime.

    The catalog stores second-resolution ISO with no offset (see
    `cataloger._format_utc`), but a `Z` or `+00:00` suffix is accepted too so
    a hand-edited row can't silently fall out of the scan.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
