"""RBAC checks and durable audit logging.

Backwards compatible with V1.0 (``check_permissions``, ``log_audit_event``,
``enterprise_auth``). ``log_audit_event`` now returns a boolean so callers can
detect a failed write instead of it disappearing silently.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.domain.models.enterprise import AuditEvent
from app.infrastructure.database.database import SessionLocal
from app.infrastructure.logging.logger import get_logger

logger = get_logger("enterprise")

ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "admin": [
        "read", "write", "execute", "manage_users", "manage_plugins",
        "manage_settings", "view_audit",
    ],
    "editor": ["read", "write", "execute"],
    "operator": ["read", "execute"],
    "viewer": ["read"],
}


class EnterpriseAuth:
    def roles(self) -> Dict[str, List[str]]:
        """Return a copy of the role -> permissions map."""
        return {role: list(perms) for role, perms in ROLE_PERMISSIONS.items()}

    def check_permissions(self, user_role: str, required_permission: str) -> bool:
        if not user_role or not required_permission:
            return False
        return required_permission in ROLE_PERMISSIONS.get(user_role.lower(), [])

    def require_permission(self, user_role: str, required_permission: str) -> None:
        """Raise ``ForbiddenError`` when the role lacks the permission."""
        from app.core.errors import ForbiddenError

        if not self.check_permissions(user_role, required_permission):
            raise ForbiddenError(
                f"Role {user_role!r} lacks the {required_permission!r} permission.",
                details={"role": user_role, "permission": required_permission},
            )

    def log_audit_event(
        self, event_name: str, user_id: int, details: Dict[str, Any]
    ) -> bool:
        """Persist an audit event. Returns True on success."""
        try:
            with SessionLocal() as db:
                event = AuditEvent(
                    user_id=user_id, event_name=event_name, details=details
                )
                db.add(event)
                db.commit()
            return True
        except Exception:
            logger.exception("Audit log write failed for event %r", event_name)
            return False


enterprise_auth = EnterpriseAuth()
