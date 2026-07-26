"""Password and API-key hashing (M5).

Deliberately stdlib-only. Creator OS is a local-first desktop application that
must install without a compiler toolchain, so pulling in ``bcrypt``/``argon2``
(both C extensions) would be a real deployment regression. PBKDF2-HMAC-SHA256
at 600,000 iterations is what OWASP currently recommends when Argon2id and
scrypt are unavailable, and it ships in CPython.

Format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<digest_hex>``

The parameters are embedded in the stored string, so iteration counts can be
raised later without invalidating existing hashes — :func:`needs_rehash` tells
the caller when to upgrade a hash on the next successful login.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Tuple

ALGORITHM = "pbkdf2_sha256"

#: OWASP 2023 guidance for PBKDF2-HMAC-SHA256.
DEFAULT_ITERATIONS = 600_000
SALT_BYTES = 16

#: Guards against a memory/CPU exhaustion vector: PBKDF2 cost is paid by the
#: server, so an unbounded password length is a cheap DoS.
MAX_PASSWORD_BYTES = 1024
MIN_PASSWORD_LENGTH = 12


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails the strength policy."""


def validate_password_strength(password: str) -> None:
    """Raise :class:`PasswordPolicyError` if ``password`` is too weak.

    The policy favours length over character-class gymnastics, which is both
    more usable and more effective.
    """
    if not isinstance(password, str) or not password:
        raise PasswordPolicyError("A password is required.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes."
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    lowered = password.lower()
    if lowered in {"password1234", "administrator", "creatoros123"}:
        raise PasswordPolicyError("That password is too common.")
    if len(set(password)) < 5:
        raise PasswordPolicyError("Password must not be a repeated character run.")


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Return an encoded PBKDF2 hash for ``password``."""
    if not isinstance(password, str) or not password:
        raise PasswordPolicyError("A password is required.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"Password exceeds {MAX_PASSWORD_BYTES} bytes.")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def _parse(encoded: str) -> Tuple[int, bytes, bytes]:
    algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
    if algorithm != ALGORITHM:
        raise ValueError(f"Unsupported password algorithm {algorithm!r}.")
    return int(iterations), bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against an encoded hash.

    Returns ``False`` rather than raising on a malformed hash, so a corrupt row
    denies access instead of turning into a 500.
    """
    if not password or not encoded:
        return False
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        iterations, salt, expected = _parse(encoded)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate, expected)


def needs_rehash(encoded: str, *, iterations: int = DEFAULT_ITERATIONS) -> bool:
    """Whether a stored hash uses outdated parameters."""
    try:
        stored_iterations, _, _ = _parse(encoded)
    except (ValueError, TypeError):
        return True
    return stored_iterations < iterations


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
#: Recognisable, greppable prefix so a leaked key is obvious in logs and repos.
API_KEY_PREFIX = "cos_"
API_KEY_BYTES = 32


def generate_api_key() -> str:
    """Return a fresh, high-entropy API key (256 bits)."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(API_KEY_BYTES)}"


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest stored for an API key.

    A plain hash (not PBKDF2) is correct here: the key is 256 bits of random
    data, so it is not brute-forceable and does not need a slow KDF — and the
    digest must be cheap because it is computed on *every* authenticated
    request.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def api_key_prefix(key: str) -> str:
    """Non-secret display prefix used to identify a key in listings."""
    return key[:12]
