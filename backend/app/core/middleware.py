"""HTTP middleware: request correlation, access logging, security headers,
body-size limits and a lightweight in-process rate limiter.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections import deque
from typing import Deque, Dict, Iterable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.infrastructure.logging.logger import (
    correlation_id_var,
    get_logger,
    request_id_var,
)
from app.infrastructure.observability.metrics import (
    http_errors_total,
    http_request_duration_seconds,
    http_requests_in_flight,
    http_requests_total,
    normalise_path,
    rate_limit_rejections_total,
)

logger = get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, expose it on ``request.state`` and response headers,
    and emit one structured access log line per request."""

    def __init__(
        self,
        app: ASGIApp,
        header_name: str = "X-Request-ID",
        correlation_header: str = "X-Correlation-ID",
        *,
        collect_metrics: bool = True,
    ) -> None:
        super().__init__(app)
        self.header_name = header_name
        self.correlation_header = correlation_header
        self.collect_metrics = collect_metrics

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(self.header_name)
        rid = incoming if incoming and len(incoming) <= 128 else uuid.uuid4().hex
        # A correlation id spans several requests belonging to one logical
        # operation (e.g. an editor save followed by a run). It is echoed back
        # unchanged, and defaults to the request id when the client sends none.
        incoming_correlation = request.headers.get(self.correlation_header)
        correlation_id = (
            incoming_correlation
            if incoming_correlation and len(incoming_correlation) <= 128
            else rid
        )
        request.state.request_id = rid
        request.state.correlation_id = correlation_id
        token = request_id_var.set(rid)
        correlation_token = correlation_id_var.set(correlation_id)

        metrics_enabled = self.collect_metrics
        if metrics_enabled:
            http_requests_in_flight.inc()

        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                duration_ms = (time.perf_counter() - start) * 1000
                logger.exception(
                    "%s %s -> unhandled error in %.2fms",
                    request.method,
                    request.url.path,
                    duration_ms,
                    extra={
                        "request_id": rid,
                        "correlation_id": correlation_id,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                raise
        finally:
            request_id_var.reset(token)
            correlation_id_var.reset(correlation_token)
            if metrics_enabled:
                http_requests_in_flight.dec()

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[self.header_name] = rid
        response.headers[self.correlation_header] = correlation_id
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"

        if metrics_enabled:
            route = normalise_path(request)
            status = str(response.status_code)
            http_requests_total.inc(
                method=request.method, path=route, status=status
            )
            http_request_duration_seconds.observe(
                duration_ms / 1000, method=request.method, path=route
            )
            if response.status_code >= 400:
                http_errors_total.inc(
                    method=request.method, path=route, status=status
                )
            if response.status_code == 429:
                rate_limit_rejections_total.inc()

        log = logger.warning if response.status_code >= 500 else logger.info
        log(
            "%s %s -> %s in %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "request_id": rid,
                "correlation_id": correlation_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply defensive response headers suitable for a local desktop API."""

    def __init__(
        self,
        app: ASGIApp,
        csp: Optional[str] = None,
        *,
        hsts: bool = False,
        hsts_max_age: int = 31536000,
    ) -> None:
        super().__init__(app)
        self.csp = csp or "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # Credentials must never be cached by an intermediary.
        if request.url.path.startswith("/api/auth"):
            headers.setdefault("Cache-Control", "no-store")
            headers.setdefault("Pragma", "no-cache")
        if self.hsts:
            headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self.hsts_max_age}; includeSubDomains",
            )
        # Docs endpoints need to load their own JS/CSS; skip CSP there.
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            headers.setdefault("Content-Security-Policy", self.csp)
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF protection for cookie-authenticated requests.

    Creator OS authenticates with ``Authorization``/``X-API-Key`` headers,
    which are not attached automatically by a browser and are therefore not
    CSRF-able. This middleware exists for the case that *is* vulnerable: a
    request carrying a session cookie and no explicit auth header.

    Such a request must present a ``X-CSRF-Token`` header matching the CSRF
    cookie. Safe methods and header-authenticated requests pass through
    untouched, so existing API clients are unaffected.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

    def __init__(
        self,
        app: ASGIApp,
        *,
        header_name: str = "X-CSRF-Token",
        cookie_name: str = "creator_os_csrf",
        api_key_header: str = "X-API-Key",
        exempt_paths: Iterable[str] = ("/api/auth/login", "/api/auth/refresh"),
    ) -> None:
        super().__init__(app)
        self.header_name = header_name
        self.cookie_name = cookie_name
        self.api_key_header = api_key_header
        self.exempt_paths = tuple(exempt_paths)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in self.SAFE_METHODS:
            return await call_next(request)
        if request.url.path.startswith(self.exempt_paths):
            return await call_next(request)

        # Header-based credentials cannot be replayed cross-site by a browser.
        if request.headers.get("Authorization") or request.headers.get(
            self.api_key_header
        ):
            return await call_next(request)

        cookie_token = request.cookies.get(self.cookie_name)
        if not cookie_token:
            # No cookie session: nothing for an attacker to ride on.
            return await call_next(request)

        header_token = request.headers.get(self.header_name, "")
        if not header_token or not secrets.compare_digest(header_token, cookie_token):
            logger.warning(
                "CSRF validation failed for %s %s",
                request.method,
                request.url.path,
                extra={"path": request.url.path, "method": request.method},
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "csrf_failed",
                        "message": (
                            "CSRF token missing or invalid. Send the value of the "
                            f"{self.cookie_name} cookie in the {self.header_name} header."
                        ),
                    }
                },
            )
        return await call_next(request)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared body exceeds ``max_bytes``."""

    def __init__(self, app: ASGIApp, max_bytes: int = 25 * 1024 * 1024,
                 exempt_paths: Iterable[str] = ()) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes
        self.exempt_paths = tuple(exempt_paths)

    async def dispatch(self, request: Request, call_next) -> Response:
        if self.exempt_paths and request.url.path.startswith(self.exempt_paths):
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": (
                                    f"Request body exceeds the {self.max_bytes} byte limit."
                                ),
                            }
                        },
                    )
            except ValueError:
                pass
        return await call_next(request)


def client_identity(request: Request, *, trust_proxy: bool = False) -> str:
    """Best-available identifier for the caller.

    Prefers the authenticated credential over the network address, because an
    IP is both too coarse (everyone behind one NAT shares a bucket) and too
    easy to rotate. Falls back to ``X-Forwarded-For`` only when the deployment
    explicitly says it sits behind a trusted proxy — honouring that header
    unconditionally would let any client spoof its own identity and evade the
    limiter entirely.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{hashlib.sha256(api_key.encode()).hexdigest()[:16]}"

    authorization = request.headers.get("Authorization")
    if authorization:
        return f"tok:{hashlib.sha256(authorization.encode()).hexdigest()[:16]}"

    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Left-most entry is the original client.
            first = forwarded.split(",")[0].strip()
            if first:
                return f"ip:{first}"
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return f"ip:{real_ip}"

    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "ip:unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter keyed by credential, then client address.

    Intended for a single-process backend. For multi-process deployments this
    should be swapped for a shared store (Redis) — see docs/DEPLOYMENT.md.

    ``auth_paths`` get a separate, much stricter budget so credential stuffing
    against the login endpoint is throttled independently of normal API use.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int = 300,
        window_seconds: float = 60.0,
        exempt_paths: Iterable[str] = (
            "/health",
            "/health/live",
            "/health/ready",
            "/metrics",
        ),
        *,
        trust_proxy: bool = False,
        auth_paths: Iterable[str] = ("/api/auth/login", "/api/auth/register"),
        auth_max_requests: int = 10,
        auth_window_seconds: float = 60.0,
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.exempt_paths = tuple(exempt_paths)
        self.trust_proxy = trust_proxy
        self.auth_paths = tuple(auth_paths)
        self.auth_max_requests = auth_max_requests
        self.auth_window_seconds = auth_window_seconds
        self._hits: Dict[str, Deque[float]] = {}

    def _client_key(self, request: Request) -> str:
        return client_identity(request, trust_proxy=self.trust_proxy)

    def _budget(self, path: str) -> tuple:
        """Return ``(max_requests, window_seconds, bucket_suffix)`` for a path."""
        if path.startswith(self.auth_paths):
            return self.auth_max_requests, self.auth_window_seconds, "|auth"
        return self.max_requests, self.window_seconds, ""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in self.exempt_paths:
            return await call_next(request)

        max_requests, window_seconds, suffix = self._budget(path)
        key = f"{self._client_key(request)}{suffix}"
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= max_requests:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            logger.warning(
                "Rate limit exceeded for %s on %s", key, path,
                extra={"client": key, "path": path},
            )
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                },
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Please slow down.",
                        "details": {"retry_after_seconds": retry_after},
                    }
                },
            )

        bucket.append(now)
        # Opportunistically drop idle buckets so memory stays bounded.
        if len(self._hits) > 1024:
            for stale_key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                self._hits.pop(stale_key, None)

        response = await call_next(request)
        remaining = max(0, max_requests - len(bucket))
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
