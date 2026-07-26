from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import require_read_write
from app.core.errors import NotFoundError
from app.domain.repositories.media.media_repository import MediaAssetCreate, MediaAssetUpdate, media_asset_repo, processing_job_repo
from app.infrastructure.database.database import get_db
from app.services.media.ffmpeg import ffmpeg_status, probe_media
from app.services.media.pipeline import media_pipeline
from app.services.media.storage import delete_file, resolve_media_path, write_stream

# Router-level authorization. Applies to every route here, including any
# added later, so a new endpoint cannot ship unprotected by omission.
# Media assets are content.
router = APIRouter(prefix="/media", tags=["Media"], dependencies=[Depends(require_read_write)])


def _asset_or_404(db: Session, asset_id: int):
    asset = media_asset_repo.get(db, asset_id)
    if not asset:
        raise NotFoundError(f"Media asset {asset_id} not found.")
    return asset


def _job_or_404(db: Session, job_id: int):
    job = processing_job_repo.get(db, job_id)
    if not job:
        raise NotFoundError(f"Media processing job {job_id} not found.")
    return job


@router.get("/ffmpeg", summary="Inspect FFmpeg availability")
def inspect_ffmpeg() -> Dict[str, Any]:
    return ffmpeg_status()


@router.post("/assets", status_code=201, summary="Upload a media asset")
async def upload_asset(
    file: UploadFile = File(...),
    process: bool = Query(False, description="Enqueue processing after upload"),
    wait: bool = Query(False, description="Compatibility mode: wait for processing to finish"),
    db: Session = Depends(get_db),
):
    info = write_stream(file.file, file.filename or "upload.bin")
    asset = media_asset_repo.create(db, MediaAssetCreate(**info, metadata_={"upload_content_type": file.content_type}))
    response: Dict[str, Any] = {"asset": asset}
    if process:
        job = media_pipeline.enqueue_asset(asset.id)
        response["job"] = job
        if wait:
            await media_pipeline.wait_for_job(job.id)
            response["job"] = media_pipeline.get_job(job.id)
        else:
            response["status"] = "accepted"
            return JSONResponse(
                content={"asset": _as_dict(asset), "job": _as_dict(job), "status": "accepted"},
                status_code=202,
            )
    return response


@router.post("/assets/register", status_code=201, summary="Register an existing file under MEDIA_ROOT")
def register_asset(payload: MediaAssetCreate, db: Session = Depends(get_db)):
    resolve_media_path(payload.file_path, must_exist=True)
    return media_asset_repo.create(db, payload)


@router.get("/assets", summary="List media assets")
def list_assets(skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return media_asset_repo.get_all(db, skip, limit)


@router.get("/assets/{asset_id}", summary="Get a media asset")
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    return _asset_or_404(db, asset_id)


@router.put("/assets/{asset_id}", summary="Update a media asset")
def update_asset(asset_id: int, payload: MediaAssetUpdate, db: Session = Depends(get_db)):
    asset = _asset_or_404(db, asset_id)
    return media_asset_repo.update(db, asset, payload)


@router.delete("/assets/{asset_id}", status_code=204, summary="Delete a media asset and stored file")
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> Response:
    asset = _asset_or_404(db, asset_id)
    delete_file(asset.file_path)
    media_asset_repo.delete(db, asset_id)
    return Response(status_code=204)


@router.get("/assets/{asset_id}/content", summary="Download a media asset")
def download_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = _asset_or_404(db, asset_id)
    path = resolve_media_path(asset.file_path, must_exist=True)
    return FileResponse(path, media_type=asset.content_type or "application/octet-stream", filename=asset.filename)


@router.post("/{asset_id}/process", status_code=202, summary="Process a media asset asynchronously")
async def process_media(asset_id: int, wait: bool = Query(False), db: Session = Depends(get_db)):
    _asset_or_404(db, asset_id)
    job = media_pipeline.enqueue_asset(asset_id)
    if wait:
        await media_pipeline.wait_for_job(job.id)
        return {"status": "completed", "asset_id": asset_id, "job": media_pipeline.get_job(job.id)}
    return {"status": "accepted", "asset_id": asset_id, "job": job}


@router.post("/{asset_id}/probe", summary="Run ffprobe for an asset")
def probe_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = _asset_or_404(db, asset_id)
    return probe_media(asset.file_path)


@router.get("/jobs", summary="List processing jobs")
def list_jobs(skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return processing_job_repo.get_all(db, skip, limit)


@router.get("/jobs/{job_id}", summary="Get processing job status")
def get_job(job_id: int, db: Session = Depends(get_db)):
    return _job_or_404(db, job_id)


def _as_dict(obj):
    data = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data
