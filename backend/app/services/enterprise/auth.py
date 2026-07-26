from typing import Dict, Any
from app.infrastructure.database.database import SessionLocal
from app.domain.models.enterprise import AuditEvent

class EnterpriseAuth:
    def check_permissions(self, user_role: str, required_permission: str) -> bool:
        roles = {
            "admin": ["read", "write", "execute", "manage_users"],
            "editor": ["read", "write", "execute"],
            "viewer": ["read"]
        }
        return required_permission in roles.get(user_role, [])

    def log_audit_event(self, event_name: str, user_id: int, details: Dict[str, Any]):
        try:
            with SessionLocal() as db:
                event = AuditEvent(user_id=user_id, event_name=event_name, details=details)
                db.add(event)
                db.commit()
        except Exception as e:
            import logging
            logging.getLogger("creator_os.enterprise").error(f"Audit log failed: {e}")

enterprise_auth = EnterpriseAuth()
