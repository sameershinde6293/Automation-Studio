from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.media.models import MediaAsset, ProcessingJob
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, Dict, Any


class MediaAssetCreate(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    file_path: str = Field(..., min_length=1)
    media_type: str
    content_type: Optional[str] = None
    size_bytes: int = Field(0, ge=0)
    checksum_sha256: Optional[str] = None
    metadata_: Optional[Dict[str, Any]] = None
    is_processed: bool = False


class MediaAssetUpdate(BaseModel):
    filename: Optional[str] = Field(None, min_length=1, max_length=255)
    metadata_: Optional[Dict[str, Any]] = None
    is_processed: Optional[bool] = None


class ProcessingJobCreate(BaseModel):
    asset_id: int
    job_type: str = Field(..., min_length=1, max_length=64)
    status: str = "PENDING"
    progress: int = Field(0, ge=0, le=100)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ProcessingJobUpdate(BaseModel):
    status: Optional[str] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


media_asset_repo = BaseRepository[MediaAsset, MediaAssetCreate, MediaAssetUpdate](MediaAsset)
processing_job_repo = BaseRepository[ProcessingJob, ProcessingJobCreate, ProcessingJobUpdate](ProcessingJob)
