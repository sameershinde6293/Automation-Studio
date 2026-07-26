import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.infrastructure.database.database import Base
from app.services.plugin.plugin_service import plugin_service
from app.domain.repositories.plugin_repository import PluginCreate

engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)

def test_register_and_toggle_plugin(db):
    plugin_in = PluginCreate(name="MyPlugin", version="0.0.1")
    plugin = plugin_service.register_plugin(db, plugin_in)
    
    assert plugin.id is not None
    assert plugin.is_active is False
    
    toggled = plugin_service.toggle_plugin(db, plugin.id, True)
    assert toggled.is_active is True
