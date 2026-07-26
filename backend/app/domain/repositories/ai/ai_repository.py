from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.ai.models import AIModelRegistry, Conversation, Message, TokenUsage
from pydantic import BaseModel
from typing import Optional, Dict, Any

class AIModelRegistryCreate(BaseModel):
    name: str
    provider: str
    model_type: str
    config: Optional[Dict[str, Any]] = None
    is_active: bool = True

class ConversationCreate(BaseModel):
    title: Optional[str] = None
    metadata_: Optional[Dict[str, Any]] = None

class MessageCreate(BaseModel):
    conversation_id: int
    role: str
    content: str
    tokens_used: int = 0

class TokenUsageCreate(BaseModel):
    model_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

ai_model_repo = BaseRepository[AIModelRegistry, AIModelRegistryCreate, BaseModel](AIModelRegistry)
conversation_repo = BaseRepository[Conversation, ConversationCreate, BaseModel](Conversation)
message_repo = BaseRepository[Message, MessageCreate, BaseModel](Message)
token_usage_repo = BaseRepository[TokenUsage, TokenUsageCreate, BaseModel](TokenUsage)
