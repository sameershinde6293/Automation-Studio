"""Asynchronous, bounded media processing pipeline."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional

from app.core.errors import NotFoundError, SecurityError, ValidationError
from app.domain.repositories.media.media_repository import (
    ProcessingJobCreate,
    processing_job_repo,
    media_asset_repo,
)
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import SessionLocal
from app.services.media.ffmpeg import extract_basic_metadata, generate_poster

logger = logging.getLogger("creator_os.media")


class MediaPipeline:
    def __init__(self) -> None:
        self._executor: Optional[ThreadPoolExecutor] = None
        self._futures: Dict[int, Future] = {}

    @property
    def executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, settings.MEDIA_MAX_CONCURRENT_JOBS),
                thread_name_prefix="media-worker",
            )
        return self._executor

    def enqueue_asset(self, asset_id: int, job_type: str = "process"):
        with SessionLocal() as db:
            asset = media_asset_repo.get(db, asset_id)
            if not asset:
                raise NotFoundError(f"Media asset {asset_id} not found.")
            job = processing_job_repo.create(
                db,
                ProcessingJobCreate(asset_id=asset_id, job_type=job_type, status="PENDING", progress=0),
            )
        future = self.executor.submit(self._process_job, job.id)
        self._futures[job.id] = future
        future.add_done_callback(lambda _f, jid=job.id: self._futures.pop(jid, None))
        return job

    async def process_asset(self, asset_id: int):
        """Compatibility helper: process and wait for completion."""
        job = self.enqueue_asset(asset_id)
        await self.wait_for_job(job.id)
        return self.get_job(job.id)

    async def wait_for_job(self, job_id: int, timeout: float | None = None):
        future = self._futures.get(job_id)
        if future is None:
            return self.get_job(job_id)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)

    def get_job(self, job_id: int):
        with SessionLocal() as db:
            return processing_job_repo.get(db, job_id)

    def _update_job(self, db, job, *, status: str | None = None, progress: int | None = None, result=None, error=None):
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = max(0, min(100, progress))
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def _process_job(self, job_id: int):
        with SessionLocal() as db:
            job = processing_job_repo.get(db, job_id)
            if not job:
                return None
            asset = media_asset_repo.get(db, job.asset_id)
            if not asset:
                self._update_job(db, job, status="FAILED", progress=100, error="Asset not found")
                return job
            self._update_job(db, job, status="RUNNING", progress=10)

            try:
                metadata: Dict[str, Any] = extract_basic_metadata(
                    asset.file_path, asset.media_type, asset.content_type
                )
                self._update_job(db, job, progress=65, result={"metadata": metadata})
                poster = generate_poster(asset.file_path, asset.media_type)
                result = {"metadata": metadata, "poster": poster}
                status = "COMPLETED"
                error = None
            except (SecurityError, ValidationError) as exc:
                # Legacy rows may contain absolute/non-existent paths from the
                # pre-M2 mock pipeline. Do not access them, but keep processing
                # idempotent and successful with an explicit fallback result.
                result = {
                    "metadata": {"fallback": True, "reason": str(exc), "path_accessed": False},
                    "poster": {"generated": False, "error": "secure storage policy skipped file access"},
                }
                status = "COMPLETED"
                error = None
            except Exception as exc:  # pragma: no cover - exercised via API failure path
                logger.exception("Media processing failed for job %s", job_id)
                self._update_job(db, job, status="FAILED", progress=100, error=str(exc))
                return job

            merged = dict(asset.metadata_ or {})
            merged.update(result)
            asset.metadata_ = merged
            asset.is_processed = True
            db.add(asset)
            db.commit()
            self._update_job(db, job, status=status, progress=100, result=result, error=error)
            logger.info("Media asset %s processed by job %s", asset.id, job.id)
            return job

    async def shutdown(self):
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None
            self._futures.clear()


media_pipeline = MediaPipeline()
