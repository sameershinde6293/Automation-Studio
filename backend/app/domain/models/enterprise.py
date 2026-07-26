from sqlalchemy import Column, Integer, String, JSON
from app.domain.models.base import BaseModel

class AuditEvent(BaseModel):
    __tablename__ = "audit_events"
    user_id = Column(Integer, nullable=False)
    event_name = Column(String, nullable=False)
    details = Column(JSON, nullable=True)
