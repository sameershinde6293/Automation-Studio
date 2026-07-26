"""JWT issuance and verification (M5).

A compact, dependency-free HS256 JWT implementation. ``PyJWT`` would be the
obvious choice, but Creator OS deliberately keeps its runtime dependency set
small and installable without network access to extra wheels; HS256 over
``hmac``/``hashlib`` is ~100 lines and fully testable.

Security properties enforced here:

* **Algorithm is pinned.** The ``alg`` header is checked against ``HS256``
  before verification, so the classic ``alg: none`` and RS256-to-HS256
  confusion attacks are rejected.
* **Signature is compared in constant time** (``hmac.compare_digest``).
* **Claims are validated**: ``exp``, ``nbf``, ``iss``, ``aud`` and ``typ``.
  An access token can never be replayed as a refresh token, and vice versa.
* **Every token carries a ``jti``**, which is what makes server-side refresh
  revocation possible.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any, Dict, Optional

ALGORITHM = "HS256"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

#: Tolerance for clock skew between issuer and verifier, in seconds.
LEEWAY_SECONDS = 30


class TokenError(Exception):
    """Base class for all token failures."""


class TokenExpiredError(TokenError):
    """The token is structurally valid but past its expiry."""


class TokenInvalidError(TokenError):
    """The token is malformed, mis-signed, or has the wrong claims."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:  # noqa: BLE001 - any decode failure is "invalid"
        raise TokenInvalidError("Token segment is not valid base64url.") from exc


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def encode_token(
    claims: Dict[str, Any],
    secret: str,
    *,
    expires_in: float,
    token_type: str = TOKEN_TYPE_ACCESS,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    jti: Optional[str] = None,
) -> str:
    """Build a signed HS256 JWT.

    Args:
        claims: Application claims (``sub``, ``role``, ...). Reserved claims
            set by this function are overwritten.
        secret: HMAC signing secret.
        expires_in: Lifetime in seconds from now.
        token_type: Written to the ``typ`` claim and enforced on decode.
        issuer / audience: Optional ``iss`` / ``aud`` claims.
        jti: Token id. Generated when omitted.
    """
    if not secret:
        raise TokenInvalidError("A signing secret is required to issue tokens.")

    now = int(time.time())
    payload: Dict[str, Any] = dict(claims)
    payload.update(
        {
            "iat": now,
            "nbf": now,
            "exp": now + int(expires_in),
            "typ": token_type,
            "jti": jti or uuid.uuid4().hex,
        }
    )
    if issuer:
        payload["iss"] = issuer
    if audience:
        payload["aud"] = audience

    header = {"alg": ALGORITHM, "typ": "JWT"}
    header_segment = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_segment = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode(
            "utf-8"
        )
    )
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = _b64url_encode(_sign(signing_input, secret))
    return f"{header_segment}.{payload_segment}.{signature}"


def decode_token(
    token: str,
    secret: str,
    *,
    expected_type: Optional[str] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    verify_exp: bool = True,
) -> Dict[str, Any]:
    """Verify a JWT and return its claims.

    Raises:
        TokenExpiredError: the signature is good but the token has expired.
        TokenInvalidError: anything else (bad shape, bad alg, bad signature,
            wrong type/issuer/audience).
    """
    if not token or not isinstance(token, str):
        raise TokenInvalidError("A token is required.")
    if not secret:
        raise TokenInvalidError("A signing secret is required to verify tokens.")

    parts = token.split(".")
    if len(parts) != 3:
        raise TokenInvalidError("A JWT must have three segments.")
    header_segment, payload_segment, signature_segment = parts

    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("Token header is not valid JSON.") from exc
    if not isinstance(header, dict):
        raise TokenInvalidError("Token header must be an object.")

    # Pin the algorithm *before* verifying: this is the defence against
    # `alg: none` and algorithm-confusion attacks.
    if header.get("alg") != ALGORITHM:
        raise TokenInvalidError(f"Unsupported token algorithm {header.get('alg')!r}.")

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected_signature = _sign(signing_input, secret)
    provided_signature = _b64url_decode(signature_segment)
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise TokenInvalidError("Token signature does not match.")

    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("Token payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise TokenInvalidError("Token payload must be an object.")

    now = time.time()
    if verify_exp:
        exp = payload.get("exp")
        if exp is None:
            raise TokenInvalidError("Token is missing the 'exp' claim.")
        try:
            if float(exp) + LEEWAY_SECONDS < now:
                raise TokenExpiredError("Token has expired.")
        except (TypeError, ValueError) as exc:
            raise TokenInvalidError("Token 'exp' claim is not numeric.") from exc

    nbf = payload.get("nbf")
    if nbf is not None:
        try:
            if float(nbf) - LEEWAY_SECONDS > now:
                raise TokenInvalidError("Token is not valid yet.")
        except (TypeError, ValueError) as exc:
            raise TokenInvalidError("Token 'nbf' claim is not numeric.") from exc

    if expected_type is not None and payload.get("typ") != expected_type:
        raise TokenInvalidError(
            f"Expected a {expected_type!r} token, got {payload.get('typ')!r}."
        )
    if issuer is not None and payload.get("iss") != issuer:
        raise TokenInvalidError("Token issuer does not match.")
    if audience is not None and payload.get("aud") != audience:
        raise TokenInvalidError("Token audience does not match.")

    return payload


def generate_secret(length: int = 48) -> str:
    """Generate a signing secret suitable for ``AUTH_SECRET_KEY``."""
    return secrets.token_urlsafe(length)


def hash_token_id(jti: str) -> str:
    """Digest of a token id, as stored in ``refresh_sessions.token_hash``."""
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()
