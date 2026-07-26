from sqlalchemy import Column, Integer, String, JSON, Boolean, ForeignKey
from app.domain.models.base import BaseModel


class MediaAsset(BaseModel):
    __tablename__ = "media_assets"
    filename = Column(String, nullable=False)
    file_path = Column(String, nullable=False, unique=True)
    media_type = Column(String, nullable=False)  # image | video | audio | document | binary
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, default=0)
    checksum_sha256 = Column(String, nullable=True, index=True)
    metadata_ = Column(JSON, nullable=True)
    is_processed = Column(Boolean, default=False)


class ProcessingJob(BaseModel):
    __tablename__ = "media_processing_jobs"
    asset_id = Column(Integer, ForeignKey("media_assets.id"), nullable=False)
    job_type = Column(String, nullable=False)  # probe | thumbnail | transcode | process
    status = Column(String, default="PENDING", index=True)  # PENDING | RUNNING | COMPLETED | FAILED
    progress = Column(Integer, default=0)
    result = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
