from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.media.models import MediaAsset, ProcessingJob
from pydantic import BaseModel
from typing import Optional, Dict, Any

class MediaAssetCreate(BaseModel):
    filename: str
    file_path: str
    media_type: str
    metadata_: Optional[Dict[str, Any]] = None
    is_processed: bool = False

class ProcessingJobCreate(BaseModel):
    asset_id: int
    job_type: str
    status: str = "PENDING"
    error: Optional[str] = None

media_asset_repo = BaseRepository[MediaAsset, MediaAssetCreate, BaseModel](MediaAsset)
processing_job_repo = BaseRepository[ProcessingJob, ProcessingJobCreate, BaseModel](ProcessingJob)
