"""Background job scheduler wrapper (APScheduler).

Backwards compatible with V1.0 (``start`` / ``shutdown`` / ``job_scheduler``).

V1.1 adds an ``is_running`` property, job add/remove helpers, misfire handling
and defensive start-up so a scheduler failure never blocks API boot.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger("scheduler")


class JobScheduler:
    def __init__(self) -> None:
        jobstores = {"default": SQLAlchemyJobStore(url=settings.DATABASE_URL)}
        job_defaults = {
            "coalesce": True,        # collapse missed runs into one
            "max_instances": 1,      # never overlap the same job
            "misfire_grace_time": 60,
        }
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores, job_defaults=job_defaults
        )

    @property
    def is_running(self) -> bool:
        return bool(self.scheduler.running)

    def start(self) -> bool:
        if self.scheduler.running:
            return False
        try:
            self.scheduler.start()
        except Exception:
            logger.exception("Job scheduler failed to start.")
            return False
        logger.info("Job scheduler started")
        return True

    def shutdown(self, wait: bool = False) -> bool:
        if not self.scheduler.running:
            return False
        try:
            self.scheduler.shutdown(wait=wait)
        except Exception:
            logger.exception("Job scheduler failed to shut down cleanly.")
            return False
        logger.info("Job scheduler stopped")
        return True

    # -- job management ---------------------------------------------------- #
    def add_interval_job(
        self,
        func: Callable,
        seconds: float,
        job_id: Optional[str] = None,
        **kwargs: Any,
    ):
        return self.scheduler.add_job(
            func, "interval", seconds=seconds, id=job_id, replace_existing=True, **kwargs
        )

    def add_cron_job(self, func: Callable, cron: str, job_id: Optional[str] = None, **kwargs: Any):
        from apscheduler.triggers.cron import CronTrigger

        return self.scheduler.add_job(
            func,
            CronTrigger.from_crontab(cron),
            id=job_id,
            replace_existing=True,
            **kwargs,
        )

    def remove_job(self, job_id: str) -> bool:
        try:
            self.scheduler.remove_job(job_id)
            return True
        except Exception:
            return False

    def list_jobs(self) -> List[Dict[str, Any]]:
        if not self.scheduler.running:
            return []
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            }
            for job in self.scheduler.get_jobs()
        ]


job_scheduler = JobScheduler()
