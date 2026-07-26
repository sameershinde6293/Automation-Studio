"""Enterprise endpoints: RBAC introspection and audit log querying."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.domain.models.enterprise import AuditEvent
from app.infrastructure.database.database import get_db
from app.services.enterprise.auth import enterprise_auth

router = APIRouter(prefix="/enterprise", tags=["Enterprise"])


class PermissionCheck(BaseModel):
    role: str
    permission: str


class AuditEventCreate(BaseModel):
    event_name: str = Field(..., max_length=200)
    user_id: int = 0
    details: Optional[Dict[str, Any]] = None


@router.get("/roles", summary="List roles and their permissions")
def list_roles() -> Dict[str, List[str]]:
    return enterprise_auth.roles()


@router.post("/permissions/check", summary="Check a role/permission pair")
def check_permission(payload: PermissionCheck) -> Dict[str, Any]:
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
def create_audit_event(payload: AuditEventCreate) -> Dict[str, Any]:
    ok = enterprise_auth.log_audit_event(
        payload.event_name, payload.user_id, payload.details or {}
    )
    return {"recorded": ok, "event_name": payload.event_name}
