from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import logger
from app.api.routers import workflow_router, ai_router, media_router, project_router

app = FastAPI(title=settings.APP_NAME, version=settings.VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workflow_router.router, prefix="/api")
app.include_router(ai_router.router, prefix="/api")
app.include_router(media_router.router, prefix="/api")
app.include_router(project_router.router, prefix="/api")

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
