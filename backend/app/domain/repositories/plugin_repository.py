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
    def get_by_name(self, db, name: str) -> Optional[Plugin]:
        return db.query(self.model).filter(self.model.name == name).first()

    def get_active(self, db) -> list[Plugin]:
        return db.query(self.model).filter(self.model.is_active.is_(True)).all()

plugin_repo = PluginRepository(Plugin)
