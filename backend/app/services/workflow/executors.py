from abc import ABC, abstractmethod
from typing import Any, Dict
from app.domain.models.workflow import Node
import asyncio
import httpx
import subprocess

class BaseNodeExecutor(ABC):
    @abstractmethod
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        pass

class DummyNodeExecutor(BaseNodeExecutor):
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        await asyncio.sleep(0.1) 
        return {"status": "ok", "node": node.name}

class MathAddExecutor(BaseNodeExecutor):
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        a = node.config.get("a", 0)
        b = node.config.get("b", 0)
        val_a = context.get(a, {}).get("result", a) if isinstance(a, int) else a
        val_b = context.get(b, {}).get("result", b) if isinstance(b, int) else b
        return {"result": val_a + val_b}

class HttpRequestExecutor(BaseNodeExecutor):
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        url = node.config.get("url")
        method = node.config.get("method", "GET")
        headers = node.config.get("headers", {})
        
        async with httpx.AsyncClient() as client:
            request_kwargs = {"url": url, "headers": headers}
            if method.upper() in ["POST", "PUT", "PATCH"]:
                request_kwargs["json"] = node.config.get("body", {})
                
            response = await client.request(method, **request_kwargs)
            try:
                data = response.json()
            except:
                data = response.text
                
            return {"status_code": response.status_code, "response": data}

class CommandExecutor(BaseNodeExecutor):
    async def execute(self, node: Node, context: Dict[int, Any]) -> Any:
        command = node.config.get("command")
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode().strip(),
            "stderr": stderr.decode().strip()
        }

class ExecutorRegistry:
    def __init__(self):
        self.executors = {
            "dummy": DummyNodeExecutor(),
            "math_add": MathAddExecutor(),
            "http_request": HttpRequestExecutor(),
            "shell_command": CommandExecutor()
        }
        
    def get_executor(self, node_type: str) -> BaseNodeExecutor:
        executor = self.executors.get(node_type)
        if not executor:
            raise ValueError(f"No executor found for node type: {node_type}")
        return executor

executor_registry = ExecutorRegistry()
