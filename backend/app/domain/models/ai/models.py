from sqlalchemy import Column, Integer, String, Boolean, JSON, ForeignKey, DateTime
from app.domain.models.base import BaseModel

class AIModelRegistry(BaseModel):
    __tablename__ = "ai_model_registry"
    name = Column(String, index=True, nullable=False, unique=True)
    provider = Column(String, nullable=False) # e.g., 'openai', 'local', 'anthropic'
    model_type = Column(String, nullable=False) # e.g., 'llm', 'embedding', 'vision', 'tts', 'stt'
    config = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)

class Conversation(BaseModel):
    __tablename__ = "ai_conversations"
    title = Column(String, nullable=True)
    metadata_ = Column(JSON, nullable=True)

class Message(BaseModel):
    __tablename__ = "ai_messages"
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id"), nullable=False)
    role = Column(String, nullable=False) # 'system', 'user', 'assistant'
    content = Column(String, nullable=False)
    tokens_used = Column(Integer, default=0)

class TokenUsage(BaseModel):
    __tablename__ = "ai_token_usage"
    model_name = Column(String, nullable=False)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
