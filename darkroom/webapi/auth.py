"""darkroom.webapi.auth — password hashing + signed session cookies (W9).

Stdlib only. Two independent primitives:

- `hash_password` / `verify_password`: scrypt-based password storage for the
  single UI password (separate from the `/api` bearer token, which is
  untouched by this module).
- `mint_cookie` / `verify_cookie`: a stateless, HMAC-signed session cookie.
  The signing `key` passed to both functions is the *stored password-hash
  string* itself (`hash_password`'s return value) — never the plaintext
  password. That string is high-entropy and lives only server-side, so it
  doubles as a session-signing secret with no extra storage. A useful
  side-effect: changing the password (and thus its hash) automatically
  invalidates every previously-issued cookie, with no session table or
  revocation list needed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash `password` with scrypt, returning `scrypt$<salt_hex>$<hash_hex>`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check `password` against a `hash_password`-produced string.

    Returns False (never raises) on any malformed `stored` value.
    """
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=len(expected) or _DKLEN,
        )
    except ValueError:
        # e.g. dklen=0 from a truncated/empty hash_hex
        return False

    return hmac.compare_digest(digest, expected)


def _sign(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def mint_cookie(key: str, max_age_seconds: int) -> str:
    """Mint a signed `<expiry>.<hmac_sha256_hex>` session cookie value."""
    expiry = int(time.time()) + max_age_seconds
    signature = _sign(key, str(expiry))
    return f"{expiry}.{signature}"


def verify_cookie(key: str, value: str | None) -> bool:
    """Verify a `mint_cookie`-produced value: not expired, signature intact."""
    if value is None:
        return False
    try:
        expiry_str, signature = value.split(".", 1)
        expiry = int(expiry_str)
    except ValueError:
        return False

    if expiry <= int(time.time()):
        return False

    expected = _sign(key, expiry_str)
    return hmac.compare_digest(signature, expected)
