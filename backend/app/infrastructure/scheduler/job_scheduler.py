from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from app.infrastructure.config.settings import settings
import logging

class JobScheduler:
    def __init__(self):
        jobstores = {
            'default': SQLAlchemyJobStore(url=settings.DATABASE_URL)
        }
        self.scheduler = BackgroundScheduler(jobstores=jobstores)
        
    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()
            logging.getLogger("creator_os").info("Job scheduler started")
            
    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logging.getLogger("creator_os").info("Job scheduler stopped")

job_scheduler = JobScheduler()
