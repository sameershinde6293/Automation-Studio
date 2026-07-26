import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.infrastructure.database.database import Base
from app.domain.repositories.ai.ai_repository import (
    ai_model_repo, AIModelRegistryCreate,
    conversation_repo, ConversationCreate,
    message_repo, token_usage_repo
)
from app.services.ai.orchestrator import ai_orchestrator

test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

@pytest.fixture(autouse=True)
def override_db(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr("app.services.ai.orchestrator.SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=test_engine)

@pytest.mark.asyncio
async def test_ai_orchestrator_chat():
    db = TestingSessionLocal()
    
    # Register model
    ai_model_repo.create(db, AIModelRegistryCreate(
        name="test_model", provider="mock", model_type="llm"
    ))
    
    # Create conversation
    conv = conversation_repo.create(db, ConversationCreate(title="Test Chat"))
    db.close()
    
    # Run chat
    response = await ai_orchestrator.chat(conv.id, "test_model", "Hello AI!")
    
    # Verify response
    assert "Mock response from test_model" in response
    
    # Verify persistence
    db = TestingSessionLocal()
    messages = db.query(message_repo.model).filter_by(conversation_id=conv.id).all()
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == "Hello AI!"
    assert messages[1].role == "assistant"
    assert messages[1].content == response
    
    tokens = db.query(token_usage_repo.model).all()
    assert len(tokens) == 1
    assert tokens[0].model_name == "test_model"
    assert tokens[0].total_tokens > 0
    db.close()
