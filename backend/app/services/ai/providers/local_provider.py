import httpx
from typing import List, Dict, Any, AsyncGenerator
from app.services.ai.providers.base import BaseAIProvider

class OllamaProvider(BaseAIProvider):
    def __init__(self):
        self.base_url = "http://localhost:11434/api"

    async def generate(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            **kwargs
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{self.base_url}/chat", json=payload, timeout=120.0)
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
