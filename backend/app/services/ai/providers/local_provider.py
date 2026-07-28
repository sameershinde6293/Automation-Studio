import httpx
from typing import List, Dict, Any, AsyncGenerator

from app.infrastructure.config.settings import settings
from app.services.ai.providers.base import BaseAIProvider


class OllamaProvider(BaseAIProvider):
    """Ollama chat provider.

    The endpoint comes from ``settings.OLLAMA_BASE_URL``. It was previously
    hardcoded to ``http://localhost:11434/api``, so the documented
    ``OLLAMA_BASE_URL`` setting had no effect and a remote or containerised
    Ollama could not be reached at all.
    """

    def __init__(self):
        self.base_url = settings.OLLAMA_BASE_URL

    def _resolve_base_url(self) -> str:
        return (self.base_url or settings.OLLAMA_BASE_URL).rstrip("/")

    async def generate(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            **kwargs
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._resolve_base_url()}/chat", json=payload, timeout=120.0
            )
            response.raise_for_status()
            data = response.json()
            
            return {
                "content": data["message"]["content"],
                "usage": {
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "completion_tokens": data.get("eval_count", 0),
                    "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                }
            }

    async def generate_stream(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> AsyncGenerator[str, None]:
        raise NotImplementedError("Streaming not yet implemented.")

    async def embed(self, model_name: str, text: str) -> List[float]:
        raise NotImplementedError("Embeddings not yet implemented.")
