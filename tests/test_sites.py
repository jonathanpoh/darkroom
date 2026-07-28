import pytest

from darkroom.sites import (
    describe_disagreement,
    haversine_m,
    home_sqm,
    modal_site,
    resolve_site,
    session_weight,
)


# Setúbal-area reference points used across the imaging-targets tests.
PALMELA = (38.563, -8.881)
SANTA_SUSANA = (38.444, -8.378)


class TestHaversineM:
    def test_same_point_is_zero(self):
        assert haversine_m(38.563, -8.881, 38.563, -8.881) == 0

    def test_symmetric(self):
        a = haversine_m(*PALMELA, *SANTA_SUSANA)
        b = haversine_m(*SANTA_SUSANA, *PALMELA)
        assert a == pytest.approx(b)

    def test_palmela_to_santa_susana_is_roughly_45km(self):
        d = haversine_m(*PALMELA, *SANTA_SUSANA)
        assert 40000 < d < 50000


class TestResolveSite:
    def _site(self, name, lat, lon, radius_m=1000, **extra):
        return {"name": name, "lat": lat, "lon": lon, "radius_m": radius_m, **extra}

    def test_exact_match_inside_radius(self):
        site = self._site("Palmela", *PALMELA)
        assert resolve_site(*PALMELA, [site]) == site

    def test_small_jitter_still_matches(self):
        # ~0.001 deg ~= 100m, well inside a 1000m radius.
        site = self._site("Palmela", *PALMELA)
        lat, lon = PALMELA[0] + 0.001, PALMELA[1] + 0.001
        assert resolve_site(lat, lon, [site]) == site

    def test_far_point_misses(self):
        site = self._site("Palmela", *PALMELA, radius_m=1000)
        # ~2km away.
        lat, lon = PALMELA[0] + 0.018, PALMELA[1]
        assert resolve_site(lat, lon, [site]) is None

    def test_nearest_of_two_wins(self):
        near = self._site("Near", PALMELA[0] + 0.0005, PALMELA[1], radius_m=5000)
        far = self._site("Far", *SANTA_SUSANA, radius_m=50000)
        result = resolve_site(*PALMELA, [far, near])
        assert result == near

    def test_lat_none_returns_none(self):
        site = self._site("Palmela", *PALMELA)
        assert resolve_site(None, PALMELA[1], [site]) is None

    def test_lon_none_returns_none(self):
        site = self._site("Palmela", *PALMELA)
        assert resolve_site(PALMELA[0], None, [site]) is None

    def test_empty_sites_returns_none(self):
        assert resolve_site(*PALMELA, []) is None


class TestHomeSqm:
    def test_home_with_sqm(self):
        sites = [{"name": "Home", "is_home": 1, "sqm": 20.5}]
        assert home_sqm(sites) == 20.5

    def test_no_home_returns_none(self):
        sites = [{"name": "Away", "is_home": 0, "sqm": 20.5}]
        assert home_sqm(sites) is None

    def test_home_without_sqm_returns_none(self):
        sites = [{"name": "Home", "is_home": 1, "sqm": None}]
        assert home_sqm(sites) is None


class TestSessionWeight:
    def test_equal_sqm_is_neutral(self):
        site = {"name": "Home", "sqm": 21.0}
        assert session_weight(site, 21.0) == pytest.approx(1.0)

    def test_darker_site_by_2_5_mag_is_10x(self):
        site = {"name": "DarkSite", "sqm": 23.5}
        assert session_weight(site, 21.0) == pytest.approx(10.0)

    def test_site_none_is_neutral(self):
        assert session_weight(None, 21.0) == 1.0

    def test_site_without_sqm_is_neutral(self):
        site = {"name": "NoSqm", "sqm": None}
        assert session_weight(site, 21.0) == 1.0

    def test_home_none_is_neutral(self):
        site = {"name": "Home", "sqm": 21.0}
        assert session_weight(site, None) == 1.0


class TestModalSite:
    def test_unanimous_frames(self):
        lat, lon, outliers = modal_site([SANTA_SUSANA] * 5)
        assert (lat, lon) == SANTA_SUSANA
        assert outliers == {}

    def test_single_stale_frame_does_not_win(self):
        # The NGC 281 2026-07-26 case: frame 1 carried a stale home fix while
        # the remaining 43 were at the real site. Taking the first frame
        # mislabelled the whole session.
        positions = [PALMELA] + [SANTA_SUSANA] * 43
        lat, lon, outliers = modal_site(positions)
        assert (lat, lon) == SANTA_SUSANA
        assert outliers == {PALMELA: 1}

    def test_jitter_within_threshold_is_not_an_outlier(self):
        nearby = (SANTA_SUSANA[0] + 0.0005, SANTA_SUSANA[1])  # ~55 m
        lat, lon, outliers = modal_site([SANTA_SUSANA] * 3 + [nearby])
        assert (lat, lon) == SANTA_SUSANA
        assert outliers == {}

    def test_none_positions_ignored(self):
        lat, lon, outliers = modal_site([(None, None), SANTA_SUSANA, (None, None)])
        assert (lat, lon) == SANTA_SUSANA
        assert outliers == {}

    def test_no_usable_positions(self):
        assert modal_site([(None, None), (38.5, None)]) == (None, None, {})

    def test_empty(self):
        assert modal_site([]) == (None, None, {})


class TestDescribeDisagreement:
    def test_reports_counts_and_distance(self):
        lines = describe_disagreement("sess", *SANTA_SUSANA, {PALMELA: 1}, 44)
        assert "sess" in lines[0]
        assert "43/44 frames" in lines[0]
        assert "1 frame(s)" in lines[1]
        # Palmela -> Santa Susana is ~45 km
        assert "45." in lines[1] or "44." in lines[1]

    def test_one_line_per_outlier_position(self):
        other = (38.3938, -8.43112)
        lines = describe_disagreement("s", *SANTA_SUSANA, {PALMELA: 1, other: 3}, 10)
        assert len(lines) == 3
        # Busiest outlier listed first
        assert "3 frame(s)" in lines[1]
