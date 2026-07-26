"""Typed application error hierarchy and FastAPI exception handlers.

Every error leaving the API uses a single stable envelope::

    {
      "error": {
        "code": "not_found",
        "message": "Workflow 12 not found",
        "details": {...},
        "request_id": "b2f1..."
      }
    }

This keeps clients (and the desktop UI) from having to parse ad-hoc shapes and
stops internal stack traces from leaking to callers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("creator_os.errors")


class CreatorOSError(Exception):
    """Base class for all deliberate, expected application errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        *,
        details: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code

    def to_dict(self, request_id: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        if request_id:
            payload["request_id"] = request_id
        return {"error": payload}


class NotFoundError(CreatorOSError):
    status_code = 404
    code = "not_found"


class ValidationError(CreatorOSError):
    status_code = 422
    code = "validation_error"


class ConflictError(CreatorOSError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(CreatorOSError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(CreatorOSError):
    status_code = 403
    code = "forbidden"


class RateLimitError(CreatorOSError):
    status_code = 429
    code = "rate_limited"


class SecurityError(CreatorOSError):
    """Raised when an operation is blocked by a security policy."""

    status_code = 400
    code = "security_policy_violation"


class ProviderError(CreatorOSError):
    """An upstream AI/media provider failed."""

    status_code = 502
    code = "provider_error"


class TimeoutError_(CreatorOSError):
    status_code = 504
    code = "timeout"


class ExecutionError(CreatorOSError):
    """A workflow / node execution failed."""

    status_code = 500
    code = "execution_error"


class ConfigurationError(CreatorOSError):
    status_code = 500
    code = "configuration_error"


def _request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)


def _aggregate(request: Request, exc: BaseException, request_id: Optional[str]) -> None:
    """Feed an error into the aggregator, never failing the response if it errors."""
    try:
        from app.infrastructure.observability.errors import error_aggregator

        error_aggregator.record(
            exc,
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
    except Exception:  # pragma: no cover - observability must never break serving
        logger.debug("Error aggregation failed", exc_info=True)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the standard error envelope handlers to a FastAPI app."""

    @app.exception_handler(CreatorOSError)
    async def _handle_app_error(request: Request, exc: CreatorOSError) -> JSONResponse:
        rid = _request_id(request)
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "%s on %s %s: %s",
            exc.code,
            request.method,
            request.url.path,
            exc.message,
            extra={"request_id": rid, "error_code": exc.code},
        )
        if exc.status_code >= 500:
            _aggregate(request, exc, rid)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict(rid))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        rid = _request_id(request)
        code = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            429: "rate_limited",
        }.get(exc.status_code, "http_error")
        body: Dict[str, Any] = {"code": code, "message": str(exc.detail)}
        if rid:
            body["request_id"] = rid
        return JSONResponse(status_code=exc.status_code, content={"error": body})

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = _request_id(request)
        body: Dict[str, Any] = {
            "code": "validation_error",
            "message": "Request validation failed.",
            "details": {"errors": _safe_validation_errors(exc)},
        }
        if rid:
            body["request_id"] = rid
        return JSONResponse(status_code=422, content={"error": body})

    @app.exception_handler(SQLTimeoutError)
    async def _handle_pool_exhaustion(
        request: Request, exc: SQLTimeoutError
    ) -> JSONResponse:
        """Connection-pool exhaustion is overload, not a bug (M6-F6).

        M6 load testing saw this surface as an opaque HTTP 500 with a leaked
        stack trace in the logs. Overload is a *retryable* condition and the
        correct signal is 503 with Retry-After, so a load balancer backs off
        and takes the instance out of rotation instead of hammering it.
        """
        rid = _request_id(request)
        logger.error(
            "Database connection pool exhausted on %s %s",
            request.method,
            request.url.path,
            extra={"request_id": rid, "error_code": "database_unavailable"},
        )
        body: Dict[str, Any] = {
            "code": "database_unavailable",
            "message": (
                "The server is at capacity and could not obtain a database "
                "connection. Retry shortly."
            ),
        }
        if rid:
            body["request_id"] = rid
        return JSONResponse(
            status_code=503, content={"error": body}, headers={"Retry-After": "1"}
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        rid = _request_id(request)
        logger.exception(
            "Unhandled error on %s %s",
            request.method,
            request.url.path,
            extra={"request_id": rid},
        )
        _aggregate(request, exc, rid)
        body: Dict[str, Any] = {
            "code": "internal_error",
            # Never leak the raw exception text to the client.
            "message": "An internal error occurred.",
        }
        if rid:
            body["request_id"] = rid
        return JSONResponse(status_code=500, content={"error": body})


def _safe_validation_errors(exc: RequestValidationError) -> list:
    """Strip non-JSON-serialisable context from pydantic validation errors."""
    cleaned = []
    for err in exc.errors():
        cleaned.append(
            {
                "loc": [str(p) for p in err.get("loc", [])],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
        )
    return cleaned
