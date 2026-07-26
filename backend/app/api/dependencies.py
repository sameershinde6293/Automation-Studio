"""Authentication and authorization dependencies (M5).

This module is where the M0 RBAC model finally becomes *enforcement*. Routers
declare what a caller must be able to do:

.. code-block:: python

    @router.delete("/{workflow_id}", dependencies=[Depends(require_write)])
    def delete_workflow(...): ...

Backwards compatibility is explicit and deliberate: when ``AUTH_ENABLED`` is
false (the default, matching the single-user desktop product Creator OS has
been so far) every request resolves to a local admin principal and all existing
clients keep working unchanged. Setting ``AUTH_ENABLED=true`` turns the same
endpoints into authenticated, role-checked ones with no code change.

``ENVIRONMENT=production`` with ``AUTH_ENABLED=false`` is rejected at startup by
``app.core.startup``, so the permissive default cannot silently reach a server.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, UnauthorizedError
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import get_db
from app.services.security.auth_service import auth_service
from app.services.security.principal import (
    LOCAL_ADMIN_PRINCIPAL,
    Principal,
)

#: Permission names, mirroring ``ROLE_PERMISSIONS``.
PERM_READ = "read"
PERM_WRITE = "write"
PERM_EXECUTE = "execute"
PERM_MANAGE_USERS = "manage_users"
PERM_MANAGE_PLUGINS = "manage_plugins"
PERM_MANAGE_SETTINGS = "manage_settings"
PERM_VIEW_AUDIT = "view_audit"


def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _api_key(request: Request) -> Optional[str]:
    key = request.headers.get(settings.AUTH_API_KEY_HEADER)
    return key.strip() if key and key.strip() else None


def get_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal:
    """Resolve the caller for this request.

    Order of precedence: bearer token, then API key. When authentication is
    disabled the caller is the local admin. When it is enabled and no valid
    credential is supplied, an anonymous principal is returned — *not* an
    error, so that public endpoints (login, health) still work. Authorization
    dependencies are what reject anonymous callers.
    """
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached

    if not settings.AUTH_ENABLED:
        principal = LOCAL_ADMIN_PRINCIPAL
    else:
        token = _bearer_token(request)
        key = _api_key(request)
        if token:
            principal = auth_service.principal_from_access_token(db, token)
        elif key:
            principal = auth_service.principal_from_api_key(db, key)
        else:
            from app.services.security.principal import ANONYMOUS_PRINCIPAL

            principal = ANONYMOUS_PRINCIPAL

    request.state.principal = principal
    return principal


def require_authenticated(
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Reject anonymous callers."""
    if principal.is_anonymous:
        raise UnauthorizedError(
            "Authentication is required for this endpoint.",
            details={"hint": "Send an Authorization: Bearer <token> header."},
        )
    return principal


def require_permission(permission: str) -> Callable[..., Principal]:
    """Build a dependency that requires ``permission``.

    Returns the principal so a handler can also depend on it directly::

        def handler(principal: Principal = Depends(require_permission("write"))):
    """

    def dependency(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        if principal.is_anonymous:
            raise UnauthorizedError(
                "Authentication is required for this endpoint.",
                details={"required_permission": permission},
            )
        if not principal.has_permission(permission):
            raise ForbiddenError(
                f"Role {principal.role!r} lacks the {permission!r} permission.",
                details={
                    "role": principal.role,
                    "required_permission": permission,
                    "granted": sorted(principal.permissions),
                },
            )
        return principal

    dependency.__name__ = f"require_{permission}"
    return dependency


# Pre-built dependencies for the common cases.
require_read = require_permission(PERM_READ)
require_write = require_permission(PERM_WRITE)
require_execute = require_permission(PERM_EXECUTE)
require_manage_users = require_permission(PERM_MANAGE_USERS)
require_manage_plugins = require_permission(PERM_MANAGE_PLUGINS)
require_manage_settings = require_permission(PERM_MANAGE_SETTINGS)
require_view_audit = require_permission(PERM_VIEW_AUDIT)


#: HTTP methods that only read state.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_method_permission(
    *,
    read: str = PERM_READ,
    write: str = PERM_WRITE,
) -> Callable[..., Principal]:
    """Build a router-level dependency that picks a permission by HTTP method.

    Applied with ``APIRouter(dependencies=[...])`` so it covers **every** route
    on a router, including ones added later. This is deliberate: the M5
    self-audit found that annotating endpoints individually had left 7 of 9
    routers completely unprotected, which let a ``viewer`` create and delete
    workflows, register plugins and write audit events. A router-level default
    fails closed — a new endpoint is protected the moment it is added, rather
    than whenever someone remembers to annotate it.

    Individual routes can still tighten this (for example an execution trigger
    requiring ``execute``) by declaring their own dependency; both run, and
    both must pass.
    """

    def dependency(
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        permission = read if request.method in SAFE_METHODS else write
        if principal.is_anonymous:
            raise UnauthorizedError(
                "Authentication is required for this endpoint.",
                details={"required_permission": permission},
            )
        if not principal.has_permission(permission):
            raise ForbiddenError(
                f"Role {principal.role!r} lacks the {permission!r} permission.",
                details={
                    "role": principal.role,
                    "required_permission": permission,
                    "granted": sorted(principal.permissions),
                },
            )
        return principal

    dependency.__name__ = f"require_{read}_or_{write}"
    return dependency


#: Read on safe methods, write on mutations. The default for resource routers.
require_read_write = require_method_permission()

#: Read on safe methods, execute on mutations. For control surfaces such as
#: pausing or replaying a run, which are not content edits.
require_read_execute = require_method_permission(write=PERM_EXECUTE)

#: Read on safe methods, manage_plugins on mutations.
require_read_manage_plugins = require_method_permission(write=PERM_MANAGE_PLUGINS)


def require_self_or_manage_users(
    user_id: int,
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Allow a user to act on their own record, or an admin on anyone's."""
    if principal.is_anonymous:
        raise UnauthorizedError("Authentication is required for this endpoint.")
    if principal.user_id == user_id or principal.has_permission(PERM_MANAGE_USERS):
        return principal
    raise ForbiddenError("You may only access your own account.")
