import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.infrastructure.database.database import Base
from app.domain.models.plugin import Plugin
from app.domain.repositories.plugin_repository import plugin_repo, PluginCreate

engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)

def test_plugin_repository(db):
    plugin_in = PluginCreate(name="test_plugin", version="1.0.0")
    plugin = plugin_repo.create(db=db, obj_in=plugin_in)
    
    assert plugin.id is not None
    assert plugin.name == "test_plugin"
    assert plugin.is_active is False
    
    fetched_plugin = plugin_repo.get(db=db, id=plugin.id)
    assert fetched_plugin.name == "test_plugin"
