from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from app.core.errors import NotFoundError, ProviderError, ValidationError
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import SessionLocal
from app.domain.repositories.ai.ai_repository import (
    MessageCreate,
    TokenUsageCreate,
    ai_model_repo,
    conversation_repo,
    message_repo,
    token_usage_repo,
)
from app.services.ai.providers.base import BaseAIProvider
from app.services.ai.providers.mock_provider import MockAIProvider
from app.services.ai.providers.openai_provider import OpenAIProvider
from app.services.ai.providers.local_provider import OllamaProvider

logger = logging.getLogger("creator_os.ai")


class ChatResult(dict):
    """Dict response that preserves V1.0 string-containment tests."""

    def __contains__(self, item):
        if isinstance(item, str) and item in self.get("response", ""):
            return True
        return super().__contains__(item)

    def __str__(self) -> str:
        return str(self.get("response", ""))

    def __eq__(self, other):
        if isinstance(other, str):
            return self.get("response", "") == other
        return super().__eq__(other)


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4) if text else 0


class AIOrchestrator:
    def __init__(self):
        self.providers: Dict[str, BaseAIProvider] = {
            "mock": MockAIProvider(),
            "openai": OpenAIProvider(),
            "local": OllamaProvider(),
        }
        self._loaded_models = set()

    def provider_info(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": name,
                "available": self._provider_available(name),
                "capabilities": self._capabilities(name),
                "class": provider.__class__.__name__,
            }
            for name, provider in sorted(self.providers.items())
        ]

    def _provider_available(self, name: str) -> bool:
        if name == "mock":
            return True
        if name == "openai":
            return bool(settings.OPENAI_API_KEY)
        if name == "local":
            return True
        return False

    def _capabilities(self, name: str) -> List[str]:
        if name == "mock":
            return ["chat", "stream", "embedding"]
        if name in {"openai", "local"}:
            return ["chat"]
        return []

    def get_provider_for_model(self, db, model_name: str) -> Tuple[BaseAIProvider, Any]:
        model = db.query(ai_model_repo.model).filter_by(name=model_name, is_active=True).first()
        if not model:
            raise NotFoundError(f"Active AI model {model_name!r} not found.")
        provider = self.providers.get(model.provider)
        if not provider:
            raise ValidationError(f"Provider {model.provider!r} is not configured.")
        if model.name not in self._loaded_models and model.provider == "local":
            logger.info("Lazy loading local model %s", model.name)
            self._loaded_models.add(model.name)
        return provider, model

    def trim_context(self, messages: List[Dict[str, str]], max_messages: int | None = None, max_tokens: int | None = None):
        max_messages = settings.AI_CONTEXT_MAX_MESSAGES if max_messages is None else max_messages
        max_tokens = settings.AI_CONTEXT_MAX_TOKENS if max_tokens is None else max_tokens
        if max_messages < 1 or max_tokens < 1:
            raise ValidationError("Context limits must be positive.")
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        kept = non_system[-max_messages:]
        result = system[:1] + kept
        while sum(estimate_tokens(m.get("content", "")) for m in result) > max_tokens and len(result) > 1:
            # Preserve a leading system prompt and most recent turns.
            drop_index = 1 if result[0].get("role") == "system" else 0
            result.pop(drop_index)
        trimmed = max(0, len(messages) - len(result))
        return result, trimmed

    async def chat(self, conversation_id: int, model_name: str, message: str, **kwargs) -> Dict[str, Any]:
        if not message or not message.strip():
            raise ValidationError("Chat message must be non-empty.")
        with SessionLocal() as db:
            conversation = conversation_repo.get(db, conversation_id)
            if not conversation:
                raise NotFoundError(f"Conversation {conversation_id} not found.")
            history = (
                db.query(message_repo.model)
                .filter_by(conversation_id=conversation_id)
                .order_by(message_repo.model.created_at, message_repo.model.id)
                .all()
            )
            provider, _model = self.get_provider_for_model(db, model_name)
            messages = [{"role": msg.role, "content": msg.content} for msg in history]
            messages.append({"role": "user", "content": message.strip()})
            messages, trimmed = self.trim_context(messages)
            user_msg = message_repo.create(
                db,
                MessageCreate(
                    conversation_id=conversation_id,
                    role="user",
                    content=message.strip(),
                    tokens_used=estimate_tokens(message),
                ),
            )

        try:
            result = await provider.generate(model_name, messages, **kwargs)
        except Exception as exc:
            logger.exception("Error generating AI response")
            raise ProviderError("AI provider failed.", details={"provider_error": str(exc)}) from exc

        content = result.get("content", "")
        usage = result.get("usage", {}) or {}
        if not usage:
            usage = {
                "prompt_tokens": sum(estimate_tokens(m.get("content", "")) for m in messages),
                "completion_tokens": estimate_tokens(content),
            }
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

        with SessionLocal() as db:
            assistant_msg = message_repo.create(
                db,
                MessageCreate(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content,
                    tokens_used=usage.get("completion_tokens", 0),
                ),
            )
            usage_row = token_usage_repo.create(
                db,
                TokenUsageCreate(
                    model_name=model_name,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)),
                ),
            )
        return ChatResult({
            "response": content,
            "message": assistant_msg,
            "user_message_id": user_msg.id,
            "conversation_id": conversation_id,
            "model_name": model_name,
            "usage": usage,
            "usage_id": usage_row.id,
            "trimmed_messages": trimmed,
        })


ai_orchestrator = AIOrchestrator()
