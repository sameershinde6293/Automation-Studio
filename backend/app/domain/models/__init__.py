"""Domain model package.

Importing this package registers every ORM model with the declarative
``Base.metadata``, which is what ``init_db()`` and Alembic autogenerate rely on.
"""

from app.domain.models.ai.models import (  # noqa: F401
    AIModelRegistry,
    Conversation,
    Message,
    TokenUsage,
)
from app.domain.models.base import BaseModel  # noqa: F401
from app.domain.models.enterprise import AuditEvent  # noqa: F401
from app.domain.models.identity import ApiKey, RefreshSession, User  # noqa: F401
from app.domain.models.media.models import MediaAsset, ProcessingJob  # noqa: F401
from app.domain.models.plugin import Plugin  # noqa: F401
from app.domain.models.project import Project  # noqa: F401
from app.domain.models.workflow import (  # noqa: F401
    Edge,
    ExecutionStatus,
    Node,
    NodeExecution,
    Workflow,
    WorkflowExecution,
)

__all__ = [
    "AIModelRegistry",
    "ApiKey",
    "AuditEvent",
    "BaseModel",
    "Conversation",
    "Edge",
    "ExecutionStatus",
    "MediaAsset",
    "Message",
    "Node",
    "NodeExecution",
    "Plugin",
    "ProcessingJob",
    "Project",
    "RefreshSession",
    "TokenUsage",
    "User",
    "Workflow",
    "WorkflowExecution",
]
