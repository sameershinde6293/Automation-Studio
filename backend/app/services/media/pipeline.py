import asyncio
import logging
import os
from PIL import Image
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
                asset_id=asset.id, job_type="thumbnail_generation", status="RUNNING"
            ))
            
            # Simulated real work: Check if file exists, if image generate thumbnail
            error_msg = None
            try:
                if asset.media_type == "image" and os.path.exists(asset.file_path):
                    img = Image.open(asset.file_path)
                    img.thumbnail((200, 200))
                    thumb_path = f"{asset.file_path}_thumb.jpg"
                    img.save(thumb_path, "JPEG")
                    asset.metadata_ = asset.metadata_ or {}
                    asset.metadata_["thumbnail"] = thumb_path
                else:
                    # Mock delay for video/audio
                    await asyncio.sleep(0.1)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Media processing failed: {e}")

            db_job = processing_job_repo.get(db, job.id)
            if error_msg:
                db_job.status = "FAILED"
                db_job.error = error_msg
            else:
                db_job.status = "COMPLETED"
                asset.is_processed = True
                
            db.commit()
            
        logger.info(f"Asset {asset_id} processing finished.")

media_pipeline = MediaPipeline()
