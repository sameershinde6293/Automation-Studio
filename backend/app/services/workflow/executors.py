from abc import ABC, abstractmethod
from typing import Any, Dict
from app.domain.models.workflow import Node
import asyncio

class BaseNodeExecutor(ABC):
    @abstractmethod
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        pass

class DummyNodeExecutor(BaseNodeExecutor):
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        await asyncio.sleep(0.1) # Simulate async work
        return {"status": "ok", "node": node.name}

class MathAddExecutor(BaseNodeExecutor):
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        # Example to show input parsing from context based on config mapping
        # Expected config: {"a": <node_id_or_val>, "b": <node_id_or_val>}
        a = node.config.get("a", 0)
        b = node.config.get("b", 0)
        
        # If string, assume it might be a node id reference (e.g. "{node_1.result}")
        # Simplified: if integer config matches a context key
        val_a = context.get(a, {}).get("result", a) if isinstance(a, int) else a
        val_b = context.get(b, {}).get("result", b) if isinstance(b, int) else b
        
        await asyncio.sleep(0.05)
        return {"result": val_a + val_b}

class ExecutorRegistry:
    def __init__(self):
        self.executors = {
            "dummy": DummyNodeExecutor(),
            "math_add": MathAddExecutor()
        }
        
    def get_executor(self, node_type: str) -> BaseNodeExecutor:
        executor = self.executors.get(node_type)
        if not executor:
            raise ValueError(f"No executor found for node type: {node_type}")
        return executor

executor_registry = ExecutorRegistry()
