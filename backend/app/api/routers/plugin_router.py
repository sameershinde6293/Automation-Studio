"""Plugin registry and SDK introspection endpoints."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.domain.repositories.plugin_repository import PluginCreate, PluginUpdate, plugin_repo
from app.infrastructure.database.database import get_db
from app.services.plugin.plugin_service import plugin_service
from app.services.plugin_sdk.sdk import plugin_sdk

router = APIRouter(prefix="/plugins", tags=["Plugins"])


class ToggleRequest(BaseModel):
    is_active: bool


@router.get("/", summary="List registered plugins")
def list_plugins(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return plugin_service.list_plugins(db, skip, limit)


@router.post("/", summary="Register a plugin")
def register_plugin(plugin_in: PluginCreate, db: Session = Depends(get_db)):
    existing = plugin_repo.get_by_name(db, plugin_in.name)
    if existing:
        raise ConflictError(
            f"Plugin {plugin_in.name!r} is already registered.",
            details={"plugin_id": existing.id},
        )
    return plugin_service.register_plugin(db, plugin_in)


@router.get("/{plugin_id}", summary="Get a plugin")
def get_plugin(plugin_id: int, db: Session = Depends(get_db)):
    plugin = plugin_service.get_plugin(db, plugin_id)
    if not plugin:
        raise NotFoundError(f"Plugin {plugin_id} not found.")
    return plugin


@router.put("/{plugin_id}", summary="Update a plugin")
def update_plugin(plugin_id: int, payload: PluginUpdate, db: Session = Depends(get_db)):
    plugin = plugin_service.get_plugin(db, plugin_id)
    if not plugin:
        raise NotFoundError(f"Plugin {plugin_id} not found.")
    return plugin_repo.update(db, plugin, payload)


@router.post("/{plugin_id}/toggle", summary="Enable or disable a plugin")
def toggle_plugin(plugin_id: int, payload: ToggleRequest, db: Session = Depends(get_db)):
    plugin = plugin_service.toggle_plugin(db, plugin_id, payload.is_active)
    if not plugin:
        raise NotFoundError(f"Plugin {plugin_id} not found.")
    return plugin


@router.delete("/{plugin_id}", status_code=204, summary="Unregister a plugin")
def delete_plugin(plugin_id: int, db: Session = Depends(get_db)) -> Response:
    plugin = plugin_service.get_plugin(db, plugin_id)
    if not plugin:
        raise NotFoundError(f"Plugin {plugin_id} not found.")
    plugin_repo.delete(db, plugin_id)
    return Response(status_code=204)


@router.get("/sdk/hooks", summary="Inspect registered SDK hooks")
def list_hooks() -> Dict[str, List[Dict[str, Any]]]:
    return plugin_sdk.list_hooks()


@router.get("/sdk/node-types", summary="Node types contributed by plugins")
def plugin_node_types() -> Dict[str, str]:
    return plugin_sdk.list_node_types()
