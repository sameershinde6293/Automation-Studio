import httpx
from typing import List, Dict, Any, AsyncGenerator

from app.infrastructure.config.settings import settings
from app.services.ai.providers.base import BaseAIProvider


class OpenAIProvider(BaseAIProvider):
    """OpenAI chat-completions provider.

    Credentials and the base URL are read from ``settings`` (which loads
    ``.env`` via pydantic-settings) rather than straight from ``os.environ``.
    Reading the process environment directly meant a key configured the
    documented way — in ``.env`` — was invisible to this provider even though
    ``settings.OPENAI_API_KEY`` held it, so every call raised "OPENAI_API_KEY
    is not set" and the orchestrator silently fell back to another provider.
    The same applied to ``OPENAI_BASE_URL``, which was hardcoded and so could
    not be pointed at a proxy or an Azure/compatible endpoint at all.

    Both are read per request so a settings reload takes effect without
    rebuilding the orchestrator.
    """

    def __init__(self):
        # Retained as instance state for backwards compatibility with callers
        # and tests that read/patch these attributes; both fall back to the
        # live settings value when left unset.
        self.api_key = settings.OPENAI_API_KEY
        self.base_url = settings.OPENAI_BASE_URL

    def _resolve_api_key(self) -> str:
        return self.api_key or settings.OPENAI_API_KEY

    def _resolve_base_url(self) -> str:
        return (self.base_url or settings.OPENAI_BASE_URL).rstrip("/")

    async def generate(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        api_key = self._resolve_api_key()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set.")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": messages,
            **kwargs
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._resolve_base_url()}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            return {
                "content": data["choices"][0]["message"]["content"],
                "usage": data.get("usage", {})
            }

    async def generate_stream(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> AsyncGenerator[str, None]:
        raise NotImplementedError("Streaming not fully implemented in this base layer yet.")

    async def embed(self, model_name: str, text: str) -> List[float]:
        raise NotImplementedError("Embeddings not yet implemented.")
