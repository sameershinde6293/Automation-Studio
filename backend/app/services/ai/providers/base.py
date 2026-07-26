from abc import ABC, abstractmethod
from typing import List, Dict, Any, AsyncGenerator

class BaseAIProvider(ABC):
    @abstractmethod
    async def generate(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """Returns generated content and token usage."""
        pass
        
    @abstractmethod
    async def generate_stream(self, model_name: str, messages: List[Dict[str, str]], **kwargs) -> AsyncGenerator[str, None]:
        """Streams text chunks."""
        pass
        
    @abstractmethod
    async def embed(self, model_name: str, text: str) -> List[float]:
        pass
