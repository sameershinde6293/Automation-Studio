from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.plugin import Plugin
from pydantic import BaseModel
from typing import Optional

class PluginCreate(BaseModel):
    name: str
    version: str
    is_active: bool = False

class PluginUpdate(BaseModel):
    version: Optional[str] = None
    is_active: Optional[bool] = None

class PluginRepository(BaseRepository[Plugin, PluginCreate, PluginUpdate]):
    pass

plugin_repo = PluginRepository(Plugin)
