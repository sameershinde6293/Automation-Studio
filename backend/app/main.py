from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.infrastructure.config.settings import settings
import logging

# Ensure logging is initialized
from app.infrastructure.logging.logger import logger

app = FastAPI(title=settings.APP_NAME, version=settings.VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    logger.info("Application starting up...")
    from app.infrastructure.scheduler.job_scheduler import job_scheduler
    job_scheduler.start()

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutting down...")
    from app.infrastructure.scheduler.job_scheduler import job_scheduler
    job_scheduler.shutdown()

@app.get("/")
def read_root():
    return {"status": "ok", "message": f"{settings.APP_NAME} is running"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}
