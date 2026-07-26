from sqlalchemy import Column, String, Boolean
from app.domain.models.base import BaseModel

class Plugin(BaseModel):
    __tablename__ = "plugins"
    name = Column(String, index=True, unique=True, nullable=False)
    version = Column(String, nullable=False)
    is_active = Column(Boolean, default=False)
