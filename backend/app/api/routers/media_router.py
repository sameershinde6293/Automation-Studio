from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.infrastructure.database.database import get_db
from app.services.media.pipeline import media_pipeline

router = APIRouter(prefix="/media", tags=["Media"])

@router.post("/{asset_id}/process")
async def process_media(asset_id: int):
    await media_pipeline.process_asset(asset_id)
    return {"status": "Processing completed", "asset_id": asset_id}
