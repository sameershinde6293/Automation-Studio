from typing import List, Dict, Any, AsyncGenerator
from app.infrastructure.database.database import SessionLocal
from app.domain.repositories.ai.ai_repository import (
    ai_model_repo, conversation_repo, message_repo, token_usage_repo,
    MessageCreate, TokenUsageCreate
)
from app.services.ai.providers.base import BaseAIProvider
from app.services.ai.providers.mock_provider import MockAIProvider
import logging

logger = logging.getLogger("creator_os.ai")

class AIOrchestrator:
    def __init__(self):
        self.providers: Dict[str, BaseAIProvider] = {
            "mock": MockAIProvider(),
            "openai": MockAIProvider(), # Abstracted for now
            "local": MockAIProvider()   # Abstracted for now
        }
        self._loaded_models = set()

    def get_provider_for_model(self, db, model_name: str) -> BaseAIProvider:
        # Resolve from registry
        models = db.query(ai_model_repo.model).filter_by(name=model_name, is_active=True).all()
        if not models:
            logger.warning(f"Model {model_name} not found in registry. Falling back to mock.")
            return self.providers["mock"]
        
        provider_name = models[0].provider
        provider = self.providers.get(provider_name)
        if not provider:
            raise ValueError(f"Provider {provider_name} not configured.")
        
        # Lazy load logic could be triggered here if dealing with local models
        if model_name not in self._loaded_models and provider_name == "local":
            logger.info(f"Lazy loading model {model_name}...")
            self._loaded_models.add(model_name)
            
        return provider

    async def chat(self, conversation_id: int, model_name: str, message: str) -> str:
        with SessionLocal() as db:
            # 1. Get history
            history = db.query(message_repo.model).filter_by(conversation_id=conversation_id).order_by(message_repo.model.created_at).all()
            messages = [{"role": msg.role, "content": msg.content} for msg in history]
            messages.append({"role": "user", "content": message})
            
            # 2. Save user message
            message_repo.create(db, MessageCreate(
                conversation_id=conversation_id, role="user", content=message
            ))
            
            provider = self.get_provider_for_model(db, model_name)
        
        # 3. Generate response outside db context
        try:
            result = await provider.generate(model_name, messages)
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            raise

        content = result["content"]
        usage = result.get("usage", {})

        with SessionLocal() as db:
            # 4. Save AI message
            message_repo.create(db, MessageCreate(
                conversation_id=conversation_id, role="assistant", content=content, tokens_used=usage.get("completion_tokens", 0)
            ))
            
            # 5. Track tokens
            if usage:
                token_usage_repo.create(db, TokenUsageCreate(
                    model_name=model_name,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0)
                ))
                
        return content

ai_orchestrator = AIOrchestrator()
