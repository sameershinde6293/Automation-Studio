"""HTTP middleware: request correlation, access logging, security headers,
body-size limits and a lightweight in-process rate limiter.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from typing import Deque, Dict, Iterable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.infrastructure.logging.logger import get_logger, request_id_var

logger = get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, expose it on ``request.state`` and response headers,
    and emit one structured access log line per request."""

    def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(self.header_name)
        rid = incoming if incoming and len(incoming) <= 128 else uuid.uuid4().hex
        request.state.request_id = rid
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "%s %s -> unhandled error in %.2fms",
                request.method,
                request.url.path,
                duration_ms,
                extra={"request_id": rid, "duration_ms": round(duration_ms, 2)},
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[self.header_name] = rid
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"
        log = logger.warning if response.status_code >= 500 else logger.info
        log(
            "%s %s -> %s in %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply defensive response headers suitable for a local desktop API."""

    def __init__(self, app: ASGIApp, csp: Optional[str] = None) -> None:
        super().__init__(app)
        self.csp = csp or "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

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
        # Docs endpoints need to load their own JS/CSS; skip CSP there.
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            headers.setdefault("Content-Security-Policy", self.csp)
        return response


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


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-cost sliding-window rate limiter keyed by client host.

    Intended for a single-process local desktop backend. For multi-process
    deployments this should be swapped for a shared store (Redis).
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int = 300,
        window_seconds: float = 60.0,
        exempt_paths: Iterable[str] = ("/health", "/health/live", "/health/ready"),
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.exempt_paths = tuple(exempt_paths)
        self._hits: Dict[str, Deque[float]] = {}

    def _client_key(self, request: Request) -> str:
        if request.client and request.client.host:
            return request.client.host
        return "unknown"

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        key = self._client_key(request)
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            retry_after = max(1, int(self.window_seconds - (now - bucket[0])))
            logger.warning(
                "Rate limit exceeded for %s on %s", key, request.url.path,
                extra={"client": key, "path": request.url.path},
            )
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
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
        remaining = max(0, self.max_requests - len(bucket))
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
