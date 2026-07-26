import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.infrastructure.database.database import Base
from app.domain.repositories.media.media_repository import media_asset_repo, MediaAssetCreate, processing_job_repo
from app.services.media.pipeline import media_pipeline

test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

@pytest.fixture(autouse=True)
def override_db(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr("app.services.media.pipeline.SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=test_engine)

@pytest.mark.asyncio
async def test_media_pipeline():
    db = TestingSessionLocal()
    asset = media_asset_repo.create(db, MediaAssetCreate(
        filename="video.mp4", file_path="/tmp/video.mp4", media_type="video"
    ))
    db.close()
    
    await media_pipeline.process_asset(asset.id)
    
    db = TestingSessionLocal()
    processed_asset = media_asset_repo.get(db, asset.id)
    assert processed_asset.is_processed is True
    
    jobs = db.query(processing_job_repo.model).filter_by(asset_id=asset.id).all()
    assert len(jobs) == 1
    assert jobs[0].status == "COMPLETED"
    db.close()
