from sqlalchemy import Column, Integer, String, JSON, Boolean
from app.domain.models.base import BaseModel

class MediaAsset(BaseModel):
    __tablename__ = "media_assets"
    filename = Column(String, nullable=False)
    file_path = Column(String, nullable=False, unique=True)
    media_type = Column(String, nullable=False) # 'image', 'video', 'audio', 'document'
    metadata_ = Column(JSON, nullable=True)
    is_processed = Column(Boolean, default=False)

class ProcessingJob(BaseModel):
    __tablename__ = "media_processing_jobs"
    asset_id = Column(Integer, nullable=False)
    job_type = Column(String, nullable=False) # 'transcode', 'thumbnail', 'transcribe'
    status = Column(String, default="PENDING")
    error = Column(String, nullable=True)
