"""M5: password hashing and JWT primitives.

These are the foundations everything else in the auth stack rests on, so they
are tested directly rather than only through the API.
"""

from __future__ import annotations

import time

import pytest

from app.services.security.passwords import (
    API_KEY_PREFIX,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.services.security.tokens import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    TokenExpiredError,
    TokenInvalidError,
    decode_token,
    encode_token,
    generate_secret,
)

SECRET = "unit-test-secret-key-long-enough-for-hs256-000"


class TestPasswordHashing:
    def test_hash_verifies_and_is_not_plaintext(self):
        encoded = hash_password("correct-horse-battery", iterations=1000)
        assert "correct-horse-battery" not in encoded
        assert encoded.startswith("pbkdf2_sha256$")
        assert verify_password("correct-horse-battery", encoded) is True

    def test_wrong_password_is_rejected(self):
        encoded = hash_password("correct-horse-battery", iterations=1000)
        assert verify_password("wrong-horse-battery", encoded) is False

    def test_same_password_gets_a_unique_salt(self):
        """Identical passwords must not produce identical hashes."""
        first = hash_password("correct-horse-battery", iterations=1000)
        second = hash_password("correct-horse-battery", iterations=1000)
        assert first != second
        assert verify_password("correct-horse-battery", first)
        assert verify_password("correct-horse-battery", second)

    def test_malformed_hash_denies_rather_than_raising(self):
        """A corrupt row must fail closed, not turn into a 500."""
        for bad in ("", "garbage", "pbkdf2_sha256$notanint$aa$bb", "a$b$c$d"):
            assert verify_password("anything", bad) is False

    def test_empty_password_never_verifies(self):
        encoded = hash_password("correct-horse-battery", iterations=1000)
        assert verify_password("", encoded) is False

    def test_needs_rehash_detects_weaker_parameters(self):
        weak = hash_password("correct-horse-battery", iterations=1000)
        assert needs_rehash(weak, iterations=600_000) is True
        strong = hash_password("correct-horse-battery", iterations=600_000)
        assert needs_rehash(strong, iterations=600_000) is False

    @pytest.mark.parametrize(
        "password",
        ["short", "a" * (MIN_PASSWORD_LENGTH - 1), "aaaaaaaaaaaaaaa", ""],
    )
    def test_weak_passwords_are_rejected(self, password):
        with pytest.raises(PasswordPolicyError):
            validate_password_strength(password)

    def test_reasonable_password_is_accepted(self):
        validate_password_strength("a-perfectly-fine-passphrase")

    def test_oversized_password_is_rejected(self):
        """Unbounded input would make PBKDF2 a CPU-exhaustion vector."""
        with pytest.raises(PasswordPolicyError):
            hash_password("x" * 5000)


class TestApiKeys:
    def test_generated_key_is_prefixed_and_unique(self):
        first, second = generate_api_key(), generate_api_key()
        assert first.startswith(API_KEY_PREFIX)
        assert first != second
        assert len(first) > 30

    def test_hash_is_stable_and_one_way(self):
        key = generate_api_key()
        assert hash_api_key(key) == hash_api_key(key)
        assert key not in hash_api_key(key)
        assert len(hash_api_key(key)) == 64

    def test_prefix_is_not_the_whole_key(self):
        key = generate_api_key()
        assert key.startswith(api_key_prefix(key))
        assert len(api_key_prefix(key)) < len(key)


class TestJwt:
    def test_round_trip_preserves_claims(self):
        token = encode_token({"sub": "7", "role": "admin"}, SECRET, expires_in=60)
        claims = decode_token(token, SECRET)
        assert claims["sub"] == "7"
        assert claims["role"] == "admin"
        assert claims["typ"] == TOKEN_TYPE_ACCESS
        assert claims["jti"]

    def test_tampered_payload_is_rejected(self):
        import base64
        import json

        token = encode_token({"sub": "1", "role": "viewer"}, SECRET, expires_in=60)
        header, payload, signature = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["role"] = "admin"  # privilege escalation attempt
        forged = (
            base64.urlsafe_b64encode(json.dumps(decoded).encode())
            .rstrip(b"=")
            .decode()
        )
        with pytest.raises(TokenInvalidError):
            decode_token(f"{header}.{forged}.{signature}", SECRET)

    def test_wrong_secret_is_rejected(self):
        token = encode_token({"sub": "1"}, SECRET, expires_in=60)
        with pytest.raises(TokenInvalidError):
            decode_token(token, "a-completely-different-secret-value-here")

    def test_alg_none_is_rejected(self):
        """The classic JWT bypass: swap the algorithm for 'none'."""
        import base64
        import json

        header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            .rstrip(b"=")
            .decode()
        )
        payload = (
            base64.urlsafe_b64encode(
                json.dumps({"sub": "1", "role": "admin", "exp": time.time() + 60}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        with pytest.raises(TokenInvalidError):
            decode_token(f"{header}.{payload}.", SECRET)

    def test_expired_token_is_rejected(self):
        token = encode_token({"sub": "1"}, SECRET, expires_in=-3600)
        with pytest.raises(TokenExpiredError):
            decode_token(token, SECRET)

    def test_token_type_is_enforced(self):
        """A refresh token must not be usable as an access token."""
        refresh = encode_token(
            {"sub": "1"}, SECRET, expires_in=60, token_type=TOKEN_TYPE_REFRESH
        )
        with pytest.raises(TokenInvalidError):
            decode_token(refresh, SECRET, expected_type=TOKEN_TYPE_ACCESS)

    def test_issuer_and_audience_are_enforced(self):
        token = encode_token(
            {"sub": "1"}, SECRET, expires_in=60, issuer="creator-os", audience="api"
        )
        decode_token(token, SECRET, issuer="creator-os", audience="api")
        with pytest.raises(TokenInvalidError):
            decode_token(token, SECRET, issuer="somebody-else")
        with pytest.raises(TokenInvalidError):
            decode_token(token, SECRET, audience="another-audience")

    @pytest.mark.parametrize(
        "token", ["", "not-a-jwt", "a.b", "a.b.c.d", "...", "@@@.###.$$$"]
    )
    def test_malformed_tokens_are_rejected(self, token):
        with pytest.raises((TokenInvalidError, TokenExpiredError)):
            decode_token(token, SECRET)

    def test_each_token_has_a_distinct_jti(self):
        first = decode_token(encode_token({"sub": "1"}, SECRET, expires_in=60), SECRET)
        second = decode_token(encode_token({"sub": "1"}, SECRET, expires_in=60), SECRET)
        assert first["jti"] != second["jti"]

    def test_empty_secret_cannot_issue_or_verify(self):
        with pytest.raises(TokenInvalidError):
            encode_token({"sub": "1"}, "", expires_in=60)
        token = encode_token({"sub": "1"}, SECRET, expires_in=60)
        with pytest.raises(TokenInvalidError):
            decode_token(token, "")

    def test_generated_secret_is_long_and_random(self):
        assert generate_secret() != generate_secret()
        assert len(generate_secret()) >= 32
