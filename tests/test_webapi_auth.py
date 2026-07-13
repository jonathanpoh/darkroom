"""Tests for darkroom.webapi.auth — password hashing + signed session cookies."""
from __future__ import annotations

import pytest

from darkroom.webapi import auth

PASSWORD = "correct horse battery staple"


# ---------------------------------------------------------------------------
# hash_password / verify_password
# ---------------------------------------------------------------------------


def test_hash_password_roundtrip():
    stored = auth.hash_password(PASSWORD)
    assert stored.startswith("scrypt$")
    assert auth.verify_password(PASSWORD, stored) is True


def test_verify_password_rejects_wrong_password():
    stored = auth.hash_password(PASSWORD)
    assert auth.verify_password("wrong password", stored) is False


def test_hash_password_uses_random_salt():
    a = auth.hash_password(PASSWORD)
    b = auth.hash_password(PASSWORD)
    assert a != b
    assert auth.verify_password(PASSWORD, a) is True
    assert auth.verify_password(PASSWORD, b) is True


@pytest.mark.parametrize("stored", ["", "nonsense", "scrypt$xx", "scrypt$xx$yy$zz"])
def test_verify_password_malformed_stored_returns_false(stored):
    assert auth.verify_password(PASSWORD, stored) is False


def test_verify_password_wrong_scheme_returns_false():
    assert auth.verify_password(PASSWORD, "bcrypt$aa$bb") is False


# ---------------------------------------------------------------------------
# mint_cookie / verify_cookie
# ---------------------------------------------------------------------------


def test_mint_verify_cookie_roundtrip():
    key = "some-hash-string"
    cookie = auth.mint_cookie(key, max_age_seconds=60)
    assert auth.verify_cookie(key, cookie) is True


def test_verify_cookie_rejects_wrong_key():
    cookie = auth.mint_cookie("key-a", max_age_seconds=60)
    assert auth.verify_cookie("key-b", cookie) is False


def test_verify_cookie_rejects_tampered_signature():
    key = "some-hash-string"
    cookie = auth.mint_cookie(key, max_age_seconds=60)
    expiry, sig = cookie.split(".", 1)
    tampered = f"{expiry}.{'f' * len(sig)}"
    assert auth.verify_cookie(key, tampered) is False


def test_verify_cookie_rejects_tampered_expiry():
    key = "some-hash-string"
    cookie = auth.mint_cookie(key, max_age_seconds=60)
    expiry, sig = cookie.split(".", 1)
    tampered = f"{int(expiry) + 1000}.{sig}"
    assert auth.verify_cookie(key, tampered) is False


def test_verify_cookie_rejects_expired():
    key = "some-hash-string"
    cookie = auth.mint_cookie(key, max_age_seconds=-1)
    assert auth.verify_cookie(key, cookie) is False


def test_verify_cookie_rejects_expired_via_monkeypatched_time(monkeypatch):
    import time as time_module

    key = "some-hash-string"
    cookie = auth.mint_cookie(key, max_age_seconds=10)
    assert auth.verify_cookie(key, cookie) is True

    real_time = time_module.time
    monkeypatch.setattr(time_module, "time", lambda: real_time() + 1000)
    assert auth.verify_cookie(key, cookie) is False


@pytest.mark.parametrize("value", [None, "", "nonsense", "12345", "abc.def", "not-an-int.sig"])
def test_verify_cookie_rejects_garbage(value):
    assert auth.verify_cookie("some-hash-string", value) is False
