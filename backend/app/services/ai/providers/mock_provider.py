import asyncio
from app.services.ai.providers.base import BaseAIProvider
from typing import List, Dict, Any, AsyncGenerator

class MockAIProvider(BaseAIProvider):
    async def generate(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        await asyncio.sleep(0.1)
        response_text = f"Mock response from {model_name}."
        return {
            "content": response_text,
            "usage": {
                "prompt_tokens": len(str(messages)) // 4,
                "completion_tokens": len(response_text) // 4,
                "total_tokens": (len(str(messages)) + len(response_text)) // 4
            }
        }

    async def generate_stream(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> AsyncGenerator[str, None]:
        words = ["Mock", "stream", "from", model_name]
        for word in words:
            await asyncio.sleep(0.05)
            yield word + " "

    async def embed(self, model_name: str, text: str) -> List[float]:
        return [0.1, 0.2, 0.3]
