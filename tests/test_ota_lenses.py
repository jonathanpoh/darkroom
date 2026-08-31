"""F9: a camera lens can impersonate a telescope, and only the date tells them apart.

Jonathan owns a Canon EF 50mm f/1.8 and a Canon EF 100-400mm f/4.5-5.6L zoom,
both used for astrophotography before the scopes arrived (FMA180Pro January
2023, FRA400 January 2025). FOCALLEN alone cannot separate the zoom at 400mm
from an FRA400 — but a session predating the scope's purchase certainly was not
shot through it.
"""
import pytest

from darkroom.parse import KNOWN_OTAS, OTA_ACQUIRED, parse_ota


@pytest.mark.parametrize("fl,expected", [
    (100, "Canon100mm"),
    (104, "Canon100mm"),   # the real 2023-07 sessions read 104
    (136, "Canon135mm"),
    (200, "Canon200mm"),
    (202, "Canon200mm"),
    (301, "Canon300mm"),
    (386, "Canon400mm"),   # below the FRA400 window entirely
])
def test_zoom_stops_are_named_not_unknown(fl, expected):
    assert parse_ota(fl) == expected


@pytest.mark.parametrize("fl", [250, 350, 62, 94, 411])
def test_between_the_stops_stays_unknown(fl):
    # Off-mark focal lengths stay loud rather than being snapped to a stop.
    assert parse_ota(fl) == "Unknown"


class TestAcquisitionDates:
    def test_394mm_before_the_fra400_existed_is_the_zoom(self):
        # The 8 real rows: 2023-11-17 .. 2024-01-11, all Canon6D, fl 391-395,
        # all previously catalogued as FRA400.
        assert parse_ota(394, obs_date="2023-11-17") == "Canon400mm"
        assert parse_ota(394, obs_date="2024-01-11") == "Canon400mm"

    def test_394mm_after_the_fra400_arrived_is_the_scope(self):
        assert parse_ota(394, obs_date="2025-01-01") == "FRA400"
        assert parse_ota(402, obs_date="2026-08-31") == "FRA400"

    def test_180mm_before_the_fma180_existed_is_not_the_scope(self):
        # No such row exists in the catalog (the earliest FMA180 night is
        # 2023-12-14), but the rule must not be one-sided. 180 is not a marked
        # stop on the zoom, so the honest answer is Unknown, not a guess.
        assert parse_ota(180, obs_date="2022-11-01") == "Unknown"

    def test_180mm_after_the_fma180_arrived_is_the_scope(self):
        assert parse_ota(181, obs_date="2023-12-14") == "FMA180"

    def test_reducer_shares_the_fra400_date(self):
        assert parse_ota(280, obs_date="2024-06-01") == "Unknown"
        assert parse_ota(280, obs_date="2026-01-01") == "FRA400-07x"

    def test_missing_or_blank_date_behaves_exactly_as_before(self):
        # scanner.py can hand over an empty capture_date when DATE-OBS is
        # unreadable; "" must not read as "before every purchase".
        assert parse_ota(394) == "FRA400"
        assert parse_ota(394, obs_date=None) == "FRA400"
        assert parse_ota(394, obs_date="") == "FRA400"

    def test_a_date_object_works_too(self):
        from datetime import date
        assert parse_ota(394, obs_date=date(2023, 11, 17)) == "Canon400mm"


def test_every_producible_name_is_correctable_in_review():
    # KNOWN_OTAS is the only correction path in `ingest review`; a name
    # parse_ota can emit but review cannot offer is an unfixable session.
    produced = {parse_ota(fl, obs_date=d)
                for fl in range(0, 500)
                for d in (None, "2022-01-01", "2024-01-01", "2026-01-01")}
    produced.discard("Unknown")
    assert produced <= set(KNOWN_OTAS)


def test_acquisition_table_only_covers_scopes():
    # A lens has no acquisition guard — it is what the fallback resolves *to*.
    assert not any(name.startswith("Canon") for name in OTA_ACQUIRED)


def test_rescan_can_correct_a_calibration_set_ota(tmp_path):
    """F9: `ota` must be re-derivable by a rescan, not frozen at first sight.

    `set_id` carries camera/exposure/gain/temperature/date but *not* the
    optic, so a set whose OTA was inferred wrongly keeps that OTA forever
    unless the upsert's conflict clause updates it. Six real flat sets sat
    labelled FRA400 for a year because of this.
    """
    from darkroom.cataloger import init_db, upsert_calibration_set

    db = tmp_path / "cat.db"
    init_db(db)
    base = {
        "set_id": "Flat_Canon6D_0.07s_ISO1600_15C_2023-11-21",
        "frame_type": "Flat", "camera": "Canon6D", "filter": None,
        "gain": "ISO1600", "exposure_sec": 0.07, "temperature_c": 15.0,
        "frame_count": 20, "capture_date": "2023-11-21",
        "folder_path": "00_Calibration/Flats/400mm_Canon6D/2023-11-21",
    }
    upsert_calibration_set(db, {**base, "ota": "FRA400"})
    upsert_calibration_set(db, {**base, "ota": "Canon400mm"})

    import sqlite3
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ota FROM calibration_sets WHERE set_id = ?", (base["set_id"],)
        ).fetchall()
    assert rows == [("Canon400mm",)], "a rescan must be able to correct the optic"

