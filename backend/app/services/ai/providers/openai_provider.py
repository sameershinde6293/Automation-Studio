import os
import httpx
from typing import List, Dict, Any, AsyncGenerator
from app.services.ai.providers.base import BaseAIProvider

class OpenAIProvider(BaseAIProvider):
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.base_url = "https://api.openai.com/v1"

    async def generate(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set.")
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": messages,
            **kwargs
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=60.0)
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
