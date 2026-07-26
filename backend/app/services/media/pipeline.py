import asyncio
import logging
from app.infrastructure.database.database import SessionLocal
from app.domain.repositories.media.media_repository import media_asset_repo, processing_job_repo, ProcessingJobCreate

logger = logging.getLogger("creator_os.media")

class MediaPipeline:
    async def process_asset(self, asset_id: int):
        with SessionLocal() as db:
            asset = media_asset_repo.get(db, asset_id)
            if not asset:
                return
            
            job = processing_job_repo.create(db, ProcessingJobCreate(
                asset_id=asset.id, job_type="mock_processing", status="RUNNING"
            ))
            
        # Simulate processing delay
        await asyncio.sleep(0.1)
        
        with SessionLocal() as db:
            db_job = processing_job_repo.get(db, job.id)
            db_job.status = "COMPLETED"
            
            db_asset = media_asset_repo.get(db, asset_id)
            db_asset.is_processed = True
            db.commit()
            
        logger.info(f"Asset {asset_id} processed successfully.")

media_pipeline = MediaPipeline()
