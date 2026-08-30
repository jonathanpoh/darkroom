from darkroom.names import (
    _normalize_target, _normalize_camera, make_session_id, session_dest_rel, target_slug,
)


class TestNormalizeTarget:
    def test_messier_spacing(self):
        assert _normalize_target("M81") == "M 81"

    def test_messier_already_spaced(self):
        assert _normalize_target("M 81") == "M 81"

    def test_caldwell_spacing(self):
        assert _normalize_target("C49") == "C 49"

    def test_ngc_lowercase(self):
        assert _normalize_target("ngc7000") == "NGC 7000"
        assert _normalize_target("ic 443") == "IC 443"

    def test_caldwell_lowercase(self):
        assert _normalize_target("c49") == "C 49"

    def test_sh2_dash_form(self):
        assert _normalize_target("SH2-103") == "Sh2-103"

    def test_sh2_space_form(self):
        assert _normalize_target("Sh 2-103") == "Sh2-103"
        assert _normalize_target("Sh 2 103") == "Sh2-103"

    def test_sh2_already_canonical(self):
        assert _normalize_target("Sh2-103") == "Sh2-103"

    def test_unrecognized_passthrough(self):
        assert _normalize_target("Andromeda") == "Andromeda"


class TestNormalizeCamera:
    def test_none(self):
        assert _normalize_camera(None) is None

    def test_zwo_strips_spaces(self):
        assert _normalize_camera("ZWO ASI585MC Pro") == "ZWOASI585MCPro"

    def test_canon_alias(self):
        assert _normalize_camera("Canon EOS 6D") == "Canon6D"

    def test_canon_already_aliased(self):
        assert _normalize_camera("CanonEOS6D") == "Canon6D"
        assert _normalize_camera("Canon6D") == "Canon6D"


class TestMakeSessionId:
    """Moved here from test_cataloger.py in W4 (make_session_id lives in names.py now,
    keeping the write-layer astropy-free); still re-exported from darkroom.cataloger
    for back-compat, so the original tests there keep passing too."""

    def test_canonical(self):
        assert make_session_id("M 81", "2026-02-19", "FRA400", "ASI585MC", "L-Pro") == \
            "M81_20260219_FRA400_ASI585MC_L-Pro"

    def test_spaces_stripped_from_target(self):
        assert make_session_id("NGC 7380", "2025-10-01", "FRA400", "Canon6D", "L-Extreme") == \
            "NGC7380_20251001_FRA400_Canon6D_L-Extreme"

    def test_empty_filter_becomes_unknownfilter(self):
        assert make_session_id("M 45", "2024-09-19", "FRA400", "Canon6D", "") == \
            "M45_20240919_FRA400_Canon6D_UnknownFilter"

    def test_none_filter_becomes_unknownfilter(self):
        assert make_session_id("M 45", "2024-09-19", "FRA400", "Canon6D", None) == \
            "M45_20240919_FRA400_Canon6D_UnknownFilter"

    def test_target_with_multiple_spaces(self):
        assert make_session_id("IC 1805", "2025-11-01", "FMA180", "ASI585MC", "L-Extreme") == \
            "IC1805_20251101_FMA180_ASI585MC_L-Extreme"

    def test_panel_appends_p_suffix(self):
        """M1: resolves the same-night panel collision catalog_db.rename_target
        currently reports as a per-row error."""
        assert make_session_id(
            "IC 4604", "2025-04-26", "FRA400", "Canon6D", "NoFilter", panel="1-1"
        ) == "IC4604_20250426_FRA400_Canon6D_NoFilter_P1-1"

    def test_panel_none_is_byte_identical_to_no_panel(self):
        assert make_session_id(
            "M 81", "2026-02-19", "FRA400", "ASI585MC", "L-Pro", panel=None
        ) == make_session_id("M 81", "2026-02-19", "FRA400", "ASI585MC", "L-Pro")


class TestSessionDestRel:
    def test_no_panel_unchanged(self):
        result = session_dest_rel("M 81", "2026-02-19", "FRA400", "ZWO ASI585MC Pro", "L-Pro")
        assert str(result) == "01_Deep Sky Objects/M 81/2026-02-19_FRA400_ZWOASI585MCPro/Lights/L-Pro"

    def test_panel_adds_p_subdirectory(self):
        result = session_dest_rel(
            "IC 4604", "2025-04-26", "FRA400", "Canon6D", "NoFilter", panel="1-1"
        )
        assert str(result) == \
            "01_Deep Sky Objects/IC 4604/2025-04-26_FRA400_Canon6D/Lights/NoFilter/P1-1"

    def test_panel_none_keyword_is_byte_identical(self):
        with_kw = session_dest_rel("M 81", "2026-02-19", "FRA400", "ASI585MC", "L-Pro", panel=None)
        without_kw = session_dest_rel("M 81", "2026-02-19", "FRA400", "ASI585MC", "L-Pro")
        assert with_kw == without_kw


class TestTargetSlug:
    """Shared by darkroom.wbpp (creates <wbpp_root>/<slug>/) and darkroom.finish
    (looks it up) — R4: was duplicated identically in prep.py and finish.py."""

    def test_strips_single_space(self):
        assert target_slug("M 81") == "M81"

    def test_no_spaces_unchanged(self):
        assert target_slug("NGC7380") == "NGC7380"

    def test_strips_multiple_spaces(self):
        assert target_slug("IC 1805") == "IC1805"

    def test_strips_all_spaces(self):
        assert target_slug("Sh 2 103") == "Sh2103"
