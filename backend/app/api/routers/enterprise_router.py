"""Enterprise endpoints: RBAC introspection and audit log querying."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import (
    require_authenticated,
    require_manage_settings,
    require_view_audit,
)
from app.domain.models.enterprise import AuditEvent
from app.infrastructure.database.database import get_db
from app.services.enterprise.auth import enterprise_auth
from app.services.security.principal import Principal

# Authorization here is per-route rather than router-wide: reading the audit
# log, writing to it and introspecting the role model are three different
# privileges. Every route still carries one, so nothing is anonymous.
router = APIRouter(prefix="/enterprise", tags=["Enterprise"])


class PermissionCheck(BaseModel):
    role: str
    permission: str


class AuditEventCreate(BaseModel):
    event_name: str = Field(..., max_length=200)
    user_id: int = 0
    details: Optional[Dict[str, Any]] = None


@router.get("/roles", summary="List roles and their permissions")
def list_roles(
    _: Principal = Depends(require_authenticated),
) -> Dict[str, List[str]]:
    return enterprise_auth.roles()


@router.post("/permissions/check", summary="Check a role/permission pair")
def check_permission(
    payload: PermissionCheck,
    _: Principal = Depends(require_authenticated),
) -> Dict[str, Any]:
    allowed = enterprise_auth.check_permissions(payload.role, payload.permission)
    return {
        "role": payload.role,
        "permission": payload.permission,
        "allowed": allowed,
    }


@router.get("/audit", summary="Query audit events")
def list_audit_events(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    event_name: str = Query("", description="Optional exact event name filter"),
    user_id: Optional[int] = Query(None),
    _: Principal = Depends(require_view_audit),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    query = db.query(AuditEvent)
    if event_name:
        query = query.filter(AuditEvent.event_name == event_name)
    if user_id is not None:
        query = query.filter(AuditEvent.user_id == user_id)
    events = query.order_by(AuditEvent.id.desc()).offset(skip).limit(limit).all()
    return [
        {
            "id": e.id,
            "user_id": e.user_id,
            "event_name": e.event_name,
            "details": e.details,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.post("/audit", summary="Record an audit event", status_code=201)
def create_audit_event(
    payload: AuditEventCreate,
    principal: Principal = Depends(require_manage_settings),
) -> Dict[str, Any]:
    """Record a first-party audit event.

    The actor is taken from the authenticated principal, **not** from the
    request body. The M5 audit flagged that this endpoint accepted a
    caller-supplied ``user_id``, which made the audit trail forgeable by
    anyone who could reach the API. ``payload.user_id`` is now retained only
    as a subject reference inside ``details``.
    """
    details = dict(payload.details or {})
    if payload.user_id:
        details.setdefault("subject_user_id", payload.user_id)
    ok = enterprise_auth.log_audit_event(
        payload.event_name, principal.user_id or 0, details
    )
    return {"recorded": ok, "event_name": payload.event_name}
