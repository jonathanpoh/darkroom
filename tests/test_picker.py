import subprocess
import sys
from pathlib import Path

import pytest

from darkroom.catalog_client import LocalBackend
from darkroom.cataloger import init_db, upsert_session
from darkroom.picker import (
    group_nights,
    is_processed,
    needs_processing,
    night_label,
    summarize_targets,
    target_meta,
)
from darkroom.prep import _resolve_rows


def make_session(
    session_id: str,
    target: str,
    obs_date: str,
    *,
    filter_: str | None = None,
    frame_count: int | None = 10,
    total_integration_sec: int | None = 3600,
    processed_state: str = "unprocessed",
) -> dict:
    return {
        "session_id": session_id,
        "target": target,
        "obs_date": obs_date,
        "ota": "FRA400",
        "camera": "ZWOASI585MCPro",
        "filter": filter_,
        "gain": 200,
        "temperature_c": -20.0,
        "exposure_sec": 180.0,
        "focal_length": 400.0,
        "frame_count": frame_count,
        "total_integration_sec": total_integration_sec,
        "ra_deg": None,
        "dec_deg": None,
        "lights_path": f"path/{session_id}",
        "processed_state": processed_state,
        "notes": "",
    }


# ── is_processed ─────────────────────────────────────────────────────────────

def test_is_processed_missing_key():
    assert is_processed({}) is False


def test_is_processed_unprocessed():
    assert is_processed({"processed_state": "unprocessed"}) is False


def test_is_processed_skipped():
    assert is_processed({"processed_state": "skipped"}) is False


def test_is_processed_real_value():
    assert is_processed({"processed_state": "processed"}) is True


def test_is_processed_in_progress():
    # F1: 'in_progress' means editing has started but isn't finished — not
    # (yet) processed.
    assert is_processed({"processed_state": "in_progress"}) is False


# ── needs_processing ─────────────────────────────────────────────────────────

def test_needs_processing_unprocessed_is_candidate():
    assert needs_processing({"processed_state": "unprocessed"}) is True


def test_needs_processing_missing_key_defaults_candidate():
    assert needs_processing({}) is True


def test_needs_processing_processed_is_not_candidate():
    assert needs_processing({"processed_state": "processed"}) is False


def test_needs_processing_skipped_is_not_candidate():
    # A skipped night is settled — deliberately set aside, not a candidate.
    assert needs_processing({"processed_state": "skipped"}) is False


def test_needs_processing_in_progress_is_not_candidate():
    # F1: 'in_progress' is already underway — not a wbpp candidate, but also
    # not "processed" (see test_is_processed_in_progress).
    assert needs_processing({"processed_state": "in_progress"}) is False


# ── summarize_targets ────────────────────────────────────────────────────────

def test_summarize_targets_counts_and_ordering():
    rows = [
        make_session("m81_1", "M 81", "2026-06-01", filter_="L-Pro",
                     total_integration_sec=1800, processed_state="unprocessed"),
        make_session("m81_2", "M 81", "2026-06-01", filter_="Ha",
                     total_integration_sec=None, processed_state="processed"),
        make_session("m81_3", "M 81", "2026-06-10", filter_=None,
                     total_integration_sec=3600, processed_state="unprocessed"),
        make_session("ngc_1", "NGC 7000", "2026-06-15", filter_="L-eXtreme",
                     total_integration_sec=7200, processed_state="processed"),
    ]
    summaries = summarize_targets(rows)

    # Sorted by latest_date descending: NGC 7000 (06-15) before M 81 (06-10)
    assert [s["target"] for s in summaries] == ["NGC 7000", "M 81"]

    ngc = next(s for s in summaries if s["target"] == "NGC 7000")
    assert ngc["night_count"] == 1
    assert ngc["unprocessed_count"] == 0
    assert ngc["total_hours"] == pytest.approx(2.0)
    assert ngc["latest_date"] == "2026-06-15"

    m81 = next(s for s in summaries if s["target"] == "M 81")
    assert m81["night_count"] == 2
    # Both nights have at least one unprocessed row (06-01 has one unprocessed
    # row alongside a processed one; 06-10 is fully unprocessed).
    assert m81["unprocessed_count"] == 2
    assert m81["total_hours"] == pytest.approx((1800 + 0 + 3600) / 3600)
    assert m81["latest_date"] == "2026-06-10"


def test_target_meta_all_processed():
    summary = {"night_count": 1, "total_hours": 2.0, "unprocessed_count": 0}
    assert target_meta(summary) == "1 nights · 2.0h · nothing to process"


def test_summarize_targets_skipped_night_not_counted_unprocessed():
    rows = [
        make_session("s_1", "M 51", "2026-06-01", filter_="L-Pro",
                     processed_state="skipped"),
        make_session("s_2", "M 51", "2026-06-02", filter_="L-Pro",
                     processed_state="unprocessed"),
    ]
    summary = next(s for s in summarize_targets(rows) if s["target"] == "M 51")
    # Two nights, but only the unprocessed one counts as backlog.
    assert summary["night_count"] == 2
    assert summary["unprocessed_count"] == 1


def test_target_meta_some_unprocessed():
    summary = {"night_count": 2, "total_hours": 1.5, "unprocessed_count": 2}
    assert target_meta(summary) == "2 nights · 1.5h · 2 unprocessed"


# ── group_nights ─────────────────────────────────────────────────────────────

def test_group_nights_multi_filter_and_ordering_and_nofilter():
    rows = [
        make_session("m81_1", "M 81", "2026-06-01", filter_="L-Pro",
                     frame_count=50, total_integration_sec=1800, processed_state="unprocessed"),
        make_session("m81_2", "M 81", "2026-06-01", filter_="Ha",
                     frame_count=None, total_integration_sec=None,
                     processed_state="processed"),
        make_session("m81_3", "M 81", "2026-06-10", filter_=None,
                     frame_count=132, total_integration_sec=3600, processed_state="unprocessed"),
    ]
    nights = group_nights(rows)

    # Newest first
    assert [n["obs_date"] for n in nights] == ["2026-06-10", "2026-06-01"]

    night_10 = nights[0]
    assert night_10["filters"] == "NoFilter"
    assert night_10["frame_count"] == 132
    assert night_10["total_hours"] == pytest.approx(1.0)
    assert night_10["processed"] is False
    assert night_10["candidate"] is True

    night_01 = nights[1]
    # Multi-filter night grouped into a single entry, filters comma-joined
    assert night_01["filters"] == "L-Pro, Ha"
    assert night_01["frame_count"] == 50  # None-safe sum
    assert night_01["total_hours"] == pytest.approx(0.5)
    # Mixed processed/unprocessed rows -> not fully processed, still a candidate
    assert night_01["processed"] is False
    assert night_01["candidate"] is True
    assert len(night_01["rows"]) == 2


def test_group_nights_processed_true_only_when_all_rows_processed():
    rows = [
        make_session("ngc_1", "NGC 7000", "2026-06-15", filter_="L-eXtreme",
                     processed_state="processed"),
        make_session("ngc_2", "NGC 7000", "2026-06-15", filter_="SII",
                     processed_state="processed"),
    ]
    nights = group_nights(rows)
    assert len(nights) == 1
    assert nights[0]["processed"] is True
    assert nights[0]["candidate"] is False


def test_group_nights_skipped_night_is_settled_not_candidate():
    rows = [
        make_session("sk_1", "M 51", "2026-06-15", filter_="L-Pro",
                     processed_state="skipped"),
    ]
    nights = group_nights(rows)
    assert len(nights) == 1
    # Skipped is settled: not a candidate (won't be pre-checked) and not
    # "processed" (so it gets the [skipped] label, not [processed ✓]).
    assert nights[0]["candidate"] is False
    assert nights[0]["processed"] is False


def test_group_nights_unprocessed_plus_skipped_is_candidate():
    # If even one row still needs processing, the night stays a candidate.
    rows = [
        make_session("mix_1", "M 51", "2026-06-15", filter_="L-Pro",
                     processed_state="skipped"),
        make_session("mix_2", "M 51", "2026-06-15", filter_="Ha",
                     processed_state="unprocessed"),
    ]
    nights = group_nights(rows)
    assert nights[0]["candidate"] is True
    assert nights[0]["processed"] is False


# ── night_label ──────────────────────────────────────────────────────────────

def test_night_label_unprocessed():
    night = {
        "obs_date": "2026-06-21", "filters": "L-Pro",
        "frame_count": 132, "total_hours": 6.6, "processed": False,
    }
    assert night_label(night) == "2026-06-21  L-Pro  132f  6.6h"


def test_night_label_processed():
    night = {
        "obs_date": "2026-06-21", "filters": "L-Pro",
        "frame_count": 132, "total_hours": 6.6, "processed": True,
    }
    assert night_label(night) == "2026-06-21  L-Pro  132f  6.6h  [processed ✓]"


def test_night_label_skipped():
    night = {
        "obs_date": "2026-06-21", "filters": "L-Pro",
        "frame_count": 132, "total_hours": 6.6, "processed": False,
        "candidate": False,
    }
    assert night_label(night) == "2026-06-21  L-Pro  132f  6.6h  [skipped]"


# ── _resolve_rows ────────────────────────────────────────────────────────────

def _build_catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "cat.db"
    init_db(catalog)
    for row in (
        make_session("m81_1", "M 81", "2026-06-01", filter_="L-Pro"),
        make_session("m81_2", "M 81", "2026-06-01", filter_="Ha"),
        make_session("m81_3", "M 81", "2026-06-10", filter_=None),
    ):
        upsert_session(catalog, row)
    return catalog


def test_resolve_rows_single_date_backward_compat(tmp_path):
    backend = LocalBackend(_build_catalog(tmp_path))
    target_name, rows = _resolve_rows(
        backend, target="M 81", dates=["2026-06-01"], session_id=None
    )
    assert target_name == "M 81"
    assert {r["session_id"] for r in rows} == {"m81_1", "m81_2"}


def test_resolve_rows_multiple_dates_selects_exactly_those_nights(tmp_path):
    backend = LocalBackend(_build_catalog(tmp_path))
    target_name, rows = _resolve_rows(
        backend, target="M 81", dates=["2026-06-01", "2026-06-10"], session_id=None
    )
    assert target_name == "M 81"
    assert {r["session_id"] for r in rows} == {"m81_1", "m81_2", "m81_3"}


def test_resolve_rows_missing_date_exits_listing_available_nights(tmp_path):
    backend = LocalBackend(_build_catalog(tmp_path))
    with pytest.raises(SystemExit) as exc:
        _resolve_rows(
            backend, target="M 81", dates=["2026-06-01", "2099-01-01"], session_id=None
        )
    message = str(exc.value)
    assert "2099-01-01" in message
    assert "2026-06-01" in message
    assert "2026-06-10" in message


def test_resolve_rows_unknown_target_exits_with_picker_hint(tmp_path):
    backend = LocalBackend(_build_catalog(tmp_path))
    with pytest.raises(SystemExit) as exc:
        _resolve_rows(backend, target="Nonexistent", dates=None, session_id=None)
    message = str(exc.value)
    assert "darkroom wbpp" in message
    assert "picker" in message


def test_resolve_rows_session_id_path(tmp_path):
    backend = LocalBackend(_build_catalog(tmp_path))
    target_name, rows = _resolve_rows(
        backend, target=None, dates=None, session_id="m81_2"
    )
    assert target_name == "M 81"
    assert [r["session_id"] for r in rows] == ["m81_2"]


def test_resolve_rows_neither_target_nor_session_exits(tmp_path):
    backend = LocalBackend(_build_catalog(tmp_path))
    with pytest.raises(SystemExit):
        _resolve_rows(backend, target=None, dates=None, session_id=None)


# ── module import has no hard dependency on questionary ─────────────────────

def test_import_picker_does_not_import_questionary():
    result = subprocess.run(
        [sys.executable, "-c",
         "import darkroom.picker, sys; assert 'questionary' not in sys.modules"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
