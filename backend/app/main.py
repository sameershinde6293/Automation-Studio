from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import logger
from app.api.routers import workflow_router, ai_router, media_router, project_router
from app.infrastructure.scheduler.job_scheduler import job_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up...")
    job_scheduler.start()
    yield
    logger.info("Application shutting down...")
    job_scheduler.shutdown()

app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)

# Restrict CORS to local development and local desktop origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "app://.", "file://"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(project_router.router, prefix="/api")
app.include_router(workflow_router.router, prefix="/api")
app.include_router(ai_router.router, prefix="/api")
app.include_router(media_router.router, prefix="/api")

@app.get("/")
def read_root():
    return {"status": "ok", "message": f"{settings.APP_NAME} is running"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}
