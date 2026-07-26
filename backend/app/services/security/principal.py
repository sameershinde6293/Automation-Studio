"""The authenticated caller (M5).

:class:`Principal` is the single object the rest of the application reasons
about. Whether a request authenticated with a JWT, an API key, or not at all,
the routers only ever see a ``Principal`` and ask it questions.

Permissions are resolved against the existing ``ROLE_PERMISSIONS`` map in
``app.services.enterprise.auth``, so M5 enforces the RBAC model M0 defined
rather than inventing a second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

#: How the caller proved who they are.
AUTH_METHOD_JWT = "jwt"
AUTH_METHOD_API_KEY = "api_key"
AUTH_METHOD_ANONYMOUS = "anonymous"
#: Auth is disabled by configuration; the caller is treated as a local admin.
AUTH_METHOD_DISABLED = "disabled"

#: Role assumed when authentication is switched off (single-user desktop mode).
LOCAL_ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class Principal:
    """An authenticated (or explicitly anonymous) caller."""

    user_id: Optional[int] = None
    username: str = "anonymous"
    role: str = ""
    auth_method: str = AUTH_METHOD_ANONYMOUS
    #: Permission subset when authenticating with a scoped API key. Empty means
    #: "inherit everything the role grants".
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    api_key_id: Optional[int] = None
    session_id: Optional[str] = None

    # -- identity ---------------------------------------------------------- #
    @property
    def is_authenticated(self) -> bool:
        return self.auth_method != AUTH_METHOD_ANONYMOUS

    @property
    def is_anonymous(self) -> bool:
        return self.auth_method == AUTH_METHOD_ANONYMOUS

    # -- authorisation ----------------------------------------------------- #
    @property
    def permissions(self) -> FrozenSet[str]:
        """Effective permissions: the role's grants, narrowed by key scopes.

        A scoped API key can only ever *reduce* what its owner may do — the
        intersection is taken deliberately so a key cannot be used to escalate.
        """
        from app.services.enterprise.auth import ROLE_PERMISSIONS

        granted = frozenset(ROLE_PERMISSIONS.get((self.role or "").lower(), ()))
        if not self.scopes:
            return granted
        return granted & self.scopes

    def has_permission(self, permission: str) -> bool:
        return bool(permission) and permission in self.permissions

    def to_log_context(self) -> Dict[str, Any]:
        """Fields attached to log lines and audit records for this caller."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "auth_method": self.auth_method,
        }


#: Used when ``AUTH_ENABLED=false`` (the local single-user desktop default).
LOCAL_ADMIN_PRINCIPAL = Principal(
    user_id=None,
    username="local",
    role=LOCAL_ADMIN_ROLE,
    auth_method=AUTH_METHOD_DISABLED,
)

ANONYMOUS_PRINCIPAL = Principal()
