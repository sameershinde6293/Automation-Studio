from sqlalchemy.orm import Session
from app.domain.repositories.plugin_repository import plugin_repo, PluginCreate, PluginUpdate
from app.domain.models.plugin import Plugin
from typing import List, Optional

class PluginService:
    def register_plugin(self, db: Session, plugin_in: PluginCreate) -> Plugin:
        return plugin_repo.create(db=db, obj_in=plugin_in)

    def get_plugin(self, db: Session, plugin_id: int) -> Optional[Plugin]:
        return plugin_repo.get(db=db, id=plugin_id)

    def list_plugins(self, db: Session, skip: int = 0, limit: int = 100) -> List[Plugin]:
        return plugin_repo.get_all(db=db, skip=skip, limit=limit)

    def toggle_plugin(self, db: Session, plugin_id: int, is_active: bool) -> Optional[Plugin]:
        db_plugin = self.get_plugin(db, plugin_id)
        if not db_plugin:
            return None
        return plugin_repo.update(db=db, db_obj=db_plugin, obj_in=PluginUpdate(is_active=is_active))

plugin_service = PluginService()
