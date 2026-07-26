from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer

from app.infrastructure.database.database import Base


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    Replaces ``datetime.utcnow`` which is deprecated from Python 3.12.
    Stored naive (as before) so existing SQLite rows remain comparable.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BaseModel(Base):
    __abstract__ = True
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
