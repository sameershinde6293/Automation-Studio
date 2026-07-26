"""Authentication and account management endpoints (M5).

Everything credential-related lives under ``/api/auth``:

* ``POST /login`` / ``POST /refresh`` / ``POST /logout`` — token lifecycle
* ``GET  /me`` — who am I, and what may I do
* ``POST /register`` — self-registration, off by default
* ``/users`` — administration, gated on ``manage_users``
* ``/api-keys`` — machine credentials for the calling user

These routes are stricter-rate-limited than the rest of the API (see
``RateLimitMiddleware``) and are marked ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_principal,
    require_authenticated,
    require_manage_users,
)
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import get_db
from app.infrastructure.observability.metrics import auth_attempts_total
from app.services.enterprise.auth import enterprise_auth
from app.services.security.auth_service import auth_service
from app.services.security.passwords import PasswordPolicyError
from app.services.security.principal import Principal

router = APIRouter(prefix="/auth", tags=["Authentication"])


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)
    password: str = Field(..., min_length=1, max_length=1024)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=150)
    password: str = Field(..., min_length=12, max_length=1024)
    email: Optional[str] = Field(None, max_length=320)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., min_length=12, max_length=1024)


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=150)
    password: str = Field(..., min_length=12, max_length=1024)
    role: str = Field("viewer", max_length=50)
    email: Optional[str] = Field(None, max_length=320)


class UserUpdateRequest(BaseModel):
    role: Optional[str] = Field(None, max_length=50)
    is_active: Optional[bool] = None


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    scopes: List[str] = Field(
        default_factory=list,
        description="Optional narrowing of the owner's permissions.",
    )
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


def _client_ip(request: Request) -> Optional[str]:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _require_auth_enabled() -> None:
    """Credential endpoints are meaningless when auth is switched off."""
    if not settings.AUTH_ENABLED:
        raise ForbiddenError(
            "Authentication is disabled on this instance.",
            details={"hint": "Set AUTH_ENABLED=true to use accounts and tokens."},
        )


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #
@router.post("/login", summary="Exchange credentials for tokens")
def login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    _require_auth_enabled()
    try:
        user = auth_service.authenticate(db, payload.username, payload.password)
    except Exception:
        auth_attempts_total.inc(outcome="failure")
        enterprise_auth.log_audit_event(
            "auth.login.failed",
            0,
            {"username": payload.username[:150], "ip": _client_ip(request)},
        )
        raise

    auth_attempts_total.inc(outcome="success")
    enterprise_auth.log_audit_event(
        "auth.login.succeeded", user.id, {"ip": _client_ip(request)}
    )
    return auth_service.issue_token_pair(
        db,
        user,
        user_agent=request.headers.get("User-Agent"),
        client_ip=_client_ip(request),
    )


@router.post("/refresh", summary="Rotate a refresh token")
def refresh(
    payload: RefreshRequest, request: Request, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    _require_auth_enabled()
    return auth_service.rotate_refresh_token(
        db,
        payload.refresh_token,
        user_agent=request.headers.get("User-Agent"),
        client_ip=_client_ip(request),
    )


@router.post("/logout", summary="Revoke a refresh token")
def logout(payload: RefreshRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_auth_enabled()
    revoked = auth_service.revoke_refresh_token(db, payload.refresh_token)
    return {"revoked": revoked}


@router.post("/logout-all", summary="Revoke every session for the caller")
def logout_all(
    principal: Principal = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _require_auth_enabled()
    if principal.user_id is None:
        return {"revoked": 0}
    count = auth_service.revoke_all_sessions(db, principal.user_id)
    enterprise_auth.log_audit_event(
        "auth.sessions.revoked_all", principal.user_id, {"count": count}
    )
    return {"revoked": count}


@router.post("/register", status_code=201, summary="Self-register an account")
def register(
    payload: RegisterRequest, request: Request, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    _require_auth_enabled()
    if not settings.AUTH_ALLOW_SELF_REGISTRATION:
        raise ForbiddenError(
            "Self-registration is disabled. Ask an administrator for an account."
        )
    try:
        user = auth_service.create_user(
            db,
            username=payload.username,
            password=payload.password,
            email=payload.email,
        )
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc
    enterprise_auth.log_audit_event(
        "auth.user.registered", user.id, {"ip": _client_ip(request)}
    )
    return auth_service.serialize_user(user)


@router.get("/me", summary="Describe the current caller")
def whoami(principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Always answers, even for anonymous callers, so a UI can decide what to
    render without first triggering a 401."""
    return {
        "authenticated": principal.is_authenticated,
        "auth_enabled": settings.AUTH_ENABLED,
        "user_id": principal.user_id,
        "username": principal.username,
        "role": principal.role,
        "auth_method": principal.auth_method,
        "permissions": sorted(principal.permissions),
    }


@router.post("/password", summary="Change the caller's password")
def change_password(
    payload: PasswordChangeRequest,
    principal: Principal = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _require_auth_enabled()
    if principal.user_id is None:
        raise ForbiddenError("This credential is not tied to a user account.")
    user = auth_service.get_by_id(db, principal.user_id)
    if user is None:
        raise NotFoundError("Account not found.")

    # Re-authenticate before allowing the change, so a stolen access token
    # cannot be used to lock the real owner out.
    auth_service.authenticate(db, user.username, payload.current_password)
    try:
        auth_service.set_password(db, user, payload.new_password)
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc
    enterprise_auth.log_audit_event("auth.password.changed", user.id, {})
    return {"changed": True, "sessions_revoked": True}


# --------------------------------------------------------------------------- #
# User administration
# --------------------------------------------------------------------------- #
@router.get("/users", summary="List user accounts")
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: Principal = Depends(require_manage_users),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    users = auth_service.list_users(db, skip, limit)
    return {
        "items": [auth_service.serialize_user(u) for u in users],
        "total": auth_service.user_count(db),
        "skip": skip,
        "limit": limit,
    }


@router.post("/users", status_code=201, summary="Create a user account")
def create_user(
    payload: UserCreateRequest,
    principal: Principal = Depends(require_manage_users),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        user = auth_service.create_user(
            db,
            username=payload.username,
            password=payload.password,
            role=payload.role,
            email=payload.email,
        )
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc
    enterprise_auth.log_audit_event(
        "auth.user.created",
        principal.user_id or 0,
        {"created_user_id": user.id, "role": user.role},
    )
    return auth_service.serialize_user(user)


@router.patch("/users/{user_id}", summary="Update a user's role or status")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    principal: Principal = Depends(require_manage_users),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = auth_service.get_by_id(db, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")

    # Prevent an administrator from locking themselves out, which would leave
    # an instance with no way to manage users at all.
    if principal.user_id == user_id:
        if payload.is_active is False:
            raise ValidationError("You cannot deactivate your own account.")
        if payload.role is not None and payload.role.lower() != user.role:
            raise ValidationError("You cannot change your own role.")

    if payload.role is not None:
        auth_service.set_role(db, user, payload.role)
    if payload.is_active is not None:
        auth_service.set_active(db, user, payload.is_active)

    enterprise_auth.log_audit_event(
        "auth.user.updated",
        principal.user_id or 0,
        {"target_user_id": user_id, "role": payload.role, "is_active": payload.is_active},
    )
    return auth_service.serialize_user(user)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
@router.get("/api-keys", summary="List the caller's API keys")
def list_api_keys(
    principal: Principal = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if principal.user_id is None:
        return {"items": []}
    keys = auth_service.list_api_keys(db, principal.user_id)
    return {"items": [auth_service.serialize_api_key(k) for k in keys]}


@router.post("/api-keys", status_code=201, summary="Create an API key")
def create_api_key(
    payload: ApiKeyCreateRequest,
    principal: Principal = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _require_auth_enabled()
    if principal.user_id is None:
        raise ForbiddenError("This credential is not tied to a user account.")
    user = auth_service.get_by_id(db, principal.user_id)
    if user is None:
        raise NotFoundError("Account not found.")

    record, plaintext = auth_service.create_api_key(
        db,
        user,
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
    )
    enterprise_auth.log_audit_event(
        "auth.api_key.created", user.id, {"api_key_id": record.id, "name": record.name}
    )
    return {
        **auth_service.serialize_api_key(record),
        # Shown exactly once; only a hash is persisted.
        "key": plaintext,
        "warning": "Store this key now. It cannot be retrieved again.",
    }


@router.delete("/api-keys/{key_id}", status_code=204, summary="Revoke an API key")
def revoke_api_key(
    key_id: int,
    principal: Principal = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> Response:
    if principal.user_id is None:
        raise ForbiddenError("This credential is not tied to a user account.")
    if not auth_service.revoke_api_key(db, principal.user_id, key_id):
        raise NotFoundError(f"API key {key_id} not found.")
    enterprise_auth.log_audit_event(
        "auth.api_key.revoked", principal.user_id, {"api_key_id": key_id}
    )
    return Response(status_code=204)
