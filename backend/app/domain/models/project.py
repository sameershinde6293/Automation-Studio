from sqlalchemy import Column, String
from app.domain.models.base import BaseModel

class Project(BaseModel):
    __tablename__ = "projects"
    name = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
