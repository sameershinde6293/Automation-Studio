"""M4 node library.

Every node type the visual editor can place is implemented here and registered
into the existing ``executor_registry``. Before M4 the editor exposed 22 node
types while the backend implemented 10 *different* ones — the intersection was
``{delay}``, so saving any editor-built workflow returned HTTP 422 (gap I1).

Registration uses the editor's camelCase type names as the canonical key and
adds snake_case aliases, so both ``httpRequest`` and ``http_request`` resolve.
The M1 executors (``dummy``, ``math_add``, ``template``, ...) are untouched.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from app.services.workflow.runtime import RuntimeNodeExecutor

from .ai_nodes import (
    AIChatNode,
    AICompletionNode,
    ImageGenerationNode,
    PromptNode,
)
from .control_nodes import (
    ConditionNode,
    DelayNode,
    EndNode,
    LoopNode,
    StartNode,
    VariableNode,
)
from .data_nodes import (
    DatabaseNode,
    EmailNode,
    FileNode,
    FolderNode,
    JavaScriptNode,
    PythonNode,
)
from .media_nodes import (
    FFmpegNode,
    MediaProcessingNode,
    STTNode,
    TTSNode,
)
from .network_nodes import HTTPRequestNode, WebhookNode

#: Canonical editor type -> executor instance.
NODE_LIBRARY: Dict[str, RuntimeNodeExecutor] = {
    # Control flow
    "start": StartNode(),
    "end": EndNode(),
    "variable": VariableNode(),
    "condition": ConditionNode(),
    "loop": LoopNode(),
    "delay": DelayNode(),
    # AI
    "prompt": PromptNode(),
    "aiChat": AIChatNode(),
    "aiCompletion": AICompletionNode(),
    "imageGeneration": ImageGenerationNode(),
    # Network
    "httpRequest": HTTPRequestNode(),
    "webhook": WebhookNode(),
    # Data / scripting / IO
    "python": PythonNode(),
    "javascript": JavaScriptNode(),
    "database": DatabaseNode(),
    "email": EmailNode(),
    "file": FileNode(),
    "folder": FolderNode(),
    # Media
    "tts": TTSNode(),
    "stt": STTNode(),
    "ffmpeg": FFmpegNode(),
    "mediaProcessing": MediaProcessingNode(),
}


def iter_registrations() -> List[Tuple[str, RuntimeNodeExecutor, bool]]:
    """Yield ``(type_name, executor, is_alias)`` for every registrable name."""
    registrations: List[Tuple[str, RuntimeNodeExecutor, bool]] = []
    for node_type, executor in NODE_LIBRARY.items():
        registrations.append((node_type, executor, False))
        for alias in executor.aliases:
            if alias != node_type:
                registrations.append((alias, executor, True))
    return registrations


def register_all(registry, *, override: bool = False) -> List[str]:
    """Register the whole library into an ``ExecutorRegistry``.

    Existing node types are left alone unless ``override=True``, so the M1
    executors (notably ``delay``) keep working for already-saved workflows.
    Returns the list of names that were newly registered.
    """
    registered: List[str] = []
    for node_type, executor, is_alias in iter_registrations():
        if registry.has(node_type) and not override:
            continue
        registry.register(node_type, executor, override=True)
        registered.append(node_type)
    return registered


__all__ = [
    "NODE_LIBRARY",
    "iter_registrations",
    "register_all",
    "StartNode",
    "EndNode",
    "VariableNode",
    "ConditionNode",
    "LoopNode",
    "DelayNode",
    "PromptNode",
    "AIChatNode",
    "AICompletionNode",
    "ImageGenerationNode",
    "HTTPRequestNode",
    "WebhookNode",
    "PythonNode",
    "JavaScriptNode",
    "DatabaseNode",
    "EmailNode",
    "FileNode",
    "FolderNode",
    "TTSNode",
    "STTNode",
    "FFmpegNode",
    "MediaProcessingNode",
]
