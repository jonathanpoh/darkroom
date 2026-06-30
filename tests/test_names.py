from darkroom.names import _normalize_target, _normalize_camera


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
