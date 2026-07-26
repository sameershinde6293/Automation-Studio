from app.infrastructure.scheduler.job_scheduler import JobScheduler
from sqlalchemy import create_engine
import pytest

def test_scheduler_init():
    scheduler = JobScheduler()
    assert scheduler.scheduler is not None
