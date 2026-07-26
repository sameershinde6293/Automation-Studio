from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.ai.models import AIModelRegistry, Conversation, Message, TokenUsage
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any

VALID_ROLES = {"system", "user", "assistant", "tool"}
VALID_MODEL_TYPES = {"llm", "embedding", "vision", "tts", "stt"}


class AIModelRegistryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    provider: str = Field(..., min_length=1, max_length=64)
    model_type: str = Field(..., min_length=1, max_length=32)
    config: Optional[Dict[str, Any]] = None
    is_active: bool = True

    @field_validator("name", "provider", "model_type")
    @classmethod
    def no_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("null bytes are not allowed")
        return value.strip()

    @field_validator("model_type")
    @classmethod
    def validate_model_type(cls, value: str) -> str:
        if value not in VALID_MODEL_TYPES:
            raise ValueError(f"model_type must be one of {sorted(VALID_MODEL_TYPES)}")
        return value


class AIModelRegistryUpdate(BaseModel):
    provider: Optional[str] = Field(None, min_length=1, max_length=64)
    model_type: Optional[str] = Field(None, min_length=1, max_length=32)
    config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None

    @field_validator("model_type")
    @classmethod
    def validate_model_type(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in VALID_MODEL_TYPES:
            raise ValueError(f"model_type must be one of {sorted(VALID_MODEL_TYPES)}")
        return value


class ConversationCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=300)
    metadata_: Optional[Dict[str, Any]] = None


class ConversationUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=300)
    metadata_: Optional[Dict[str, Any]] = None


class MessageCreate(BaseModel):
    conversation_id: int
    role: str
    content: str = Field(..., min_length=1)
    tokens_used: int = Field(0, ge=0)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return value


class TokenUsageCreate(BaseModel):
    model_name: str
    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    total_tokens: int = Field(0, ge=0)


ai_model_repo = BaseRepository[AIModelRegistry, AIModelRegistryCreate, AIModelRegistryUpdate](AIModelRegistry)
conversation_repo = BaseRepository[Conversation, ConversationCreate, ConversationUpdate](Conversation)
message_repo = BaseRepository[Message, MessageCreate, BaseModel](Message)
token_usage_repo = BaseRepository[TokenUsage, TokenUsageCreate, BaseModel](TokenUsage)
