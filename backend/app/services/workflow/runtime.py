"""Unified node runtime: schemas, validation, metrics and error classification.

Every node in Creator OS executes through this layer. It sits *on top of* the
existing ``BaseNodeExecutor`` contract from M1 rather than replacing it, so all
V1.0/V1.1 executors keep working unchanged.

What this module adds
---------------------
* :class:`NodeContext` — the execution context handed to a node. Carries run
  variables, upstream outputs, the loop stack, conversation memory and a
  cancellation/pause handle. Behaves like the plain ``dict`` context that older
  executors expect, so ``resolve_reference`` and friends keep working.
* :class:`FieldSpec` / :class:`NodeSchema` — declarative input and output
  schemas with coercion and validation.
* :class:`NodeResult` — a node's output plus metrics (duration, tokens, cost).
* :class:`NodeErrorCode` — stable machine-readable failure classification so the
  UI and retry policy can reason about *why* a node failed.
* :class:`RuntimeNodeExecutor` — the base class new M4 nodes derive from. It
  validates inputs against the schema, runs the node, validates and truncates
  outputs, and records metrics.

Design note: validation errors are *not* retryable, transient errors are. The
engine consults :meth:`NodeErrorCode.is_retryable` so a bad config fails fast
instead of burning three attempts.
"""

from __future__ import annotations

import asyncio
import enum
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from app.core.errors import ExecutionError, SecurityError, ValidationError
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger("workflow.runtime")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #
class NodeErrorCode(str, enum.Enum):
    """Stable failure taxonomy surfaced to the UI and the retry policy."""

    VALIDATION = "validation"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    NETWORK = "network"
    PROVIDER = "provider"
    RATE_LIMIT = "rate_limit"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    DISABLED = "disabled"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"

    @property
    def is_retryable(self) -> bool:
        """Whether re-running the node unchanged could plausibly succeed."""
        return self in {
            NodeErrorCode.TIMEOUT,
            NodeErrorCode.NETWORK,
            NodeErrorCode.PROVIDER,
            NodeErrorCode.RATE_LIMIT,
            NodeErrorCode.RUNTIME,
            NodeErrorCode.UNKNOWN,
        }


def classify_exception(exc: BaseException) -> NodeErrorCode:
    """Best-effort mapping from an exception to a :class:`NodeErrorCode`."""
    from app.core.errors import NotFoundError, ProviderError

    if isinstance(exc, asyncio.CancelledError):
        return NodeErrorCode.CANCELLED
    if isinstance(exc, asyncio.TimeoutError):
        return NodeErrorCode.TIMEOUT
    if isinstance(exc, ValidationError):
        return NodeErrorCode.VALIDATION
    if isinstance(exc, SecurityError):
        return NodeErrorCode.PERMISSION
    if isinstance(exc, NotFoundError):
        return NodeErrorCode.NOT_FOUND
    if isinstance(exc, ProviderError):
        return NodeErrorCode.PROVIDER
    if isinstance(exc, NodeExecutionError):
        return exc.code

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return NodeErrorCode.TIMEOUT
    if "connect" in name or "network" in name or "dns" in name:
        return NodeErrorCode.NETWORK
    if isinstance(exc, (ConnectionError, OSError)):
        return NodeErrorCode.NETWORK
    if isinstance(exc, PermissionError):
        return NodeErrorCode.PERMISSION
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        return NodeErrorCode.RUNTIME
    if isinstance(exc, ExecutionError):
        return NodeErrorCode.RUNTIME
    return NodeErrorCode.UNKNOWN


class NodeExecutionError(ExecutionError):
    """An executor failure carrying an explicit :class:`NodeErrorCode`."""

    def __init__(
        self,
        message: str,
        *,
        code: NodeErrorCode = NodeErrorCode.RUNTIME,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class NodeDisabledError(NodeExecutionError):
    """Raised when a node type is switched off by configuration."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, code=NodeErrorCode.DISABLED, details=details)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
@dataclass
class FieldSpec:
    """One field in a node's input or output schema."""

    name: str
    type: str = "string"  # string|number|integer|boolean|object|array|any
    required: bool = False
    default: Any = None
    description: str = ""
    enum: Optional[Sequence[Any]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    secret: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.enum:
            payload["enum"] = list(self.enum)
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.secret:
            payload["secret"] = True
        return payload

    # -- coercion / validation -------------------------------------------- #
    def coerce(self, value: Any) -> Any:
        """Coerce ``value`` to this field's declared type.

        Values arriving from the editor are frequently strings (every HTML
        input yields a string), so numeric and boolean fields accept their
        string spellings rather than failing.
        """
        if value is None:
            return None
        if self.type in {"any", ""}:
            return value
        if self.type == "string":
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)
        if self.type in {"number", "integer"}:
            if isinstance(value, bool):
                number = float(value)
            elif isinstance(value, (int, float)):
                number = float(value)
            else:
                text = str(value).strip()
                if not text:
                    return None
                try:
                    number = float(text)
                except ValueError as exc:
                    raise ValidationError(
                        f"Field {self.name!r} must be a number, got {value!r}.",
                        details={"field": self.name, "value": str(value)[:200]},
                    ) from exc
            return int(number) if self.type == "integer" else number
        if self.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            return str(value).strip().lower() in {"true", "1", "yes", "on"}
        if self.type == "object":
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return {}
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        f"Field {self.name!r} must be an object or JSON string.",
                        details={"field": self.name},
                    ) from exc
                if not isinstance(parsed, dict):
                    raise ValidationError(
                        f"Field {self.name!r} must decode to an object.",
                        details={"field": self.name},
                    )
                return parsed
            raise ValidationError(
                f"Field {self.name!r} must be an object.", details={"field": self.name}
            )
        if self.type == "array":
            if isinstance(value, (list, tuple)):
                return list(value)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return []
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    # Fall back to comma-separated, which is what the editor's
                    # plain text inputs produce.
                    return [item.strip() for item in text.split(",") if item.strip()]
                return parsed if isinstance(parsed, list) else [parsed]
            return [value]
        return value

    def validate(self, value: Any) -> Any:
        """Coerce then range/enum check. Returns the cleaned value."""
        if value is None or (isinstance(value, str) and value == ""):
            if self.required and self.default is None:
                raise ValidationError(
                    f"Field {self.name!r} is required.", details={"field": self.name}
                )
            value = self.default
            if value is None:
                return None
        cleaned = self.coerce(value)
        if cleaned is None:
            if self.required:
                raise ValidationError(
                    f"Field {self.name!r} is required.", details={"field": self.name}
                )
            return None
        if self.enum and cleaned not in self.enum:
            raise ValidationError(
                f"Field {self.name!r} must be one of {list(self.enum)}.",
                details={"field": self.name, "value": str(cleaned)[:200]},
            )
        if isinstance(cleaned, (int, float)) and not isinstance(cleaned, bool):
            if self.minimum is not None and cleaned < self.minimum:
                raise ValidationError(
                    f"Field {self.name!r} must be >= {self.minimum}.",
                    details={"field": self.name, "value": cleaned},
                )
            if self.maximum is not None and cleaned > self.maximum:
                raise ValidationError(
                    f"Field {self.name!r} must be <= {self.maximum}.",
                    details={"field": self.name, "value": cleaned},
                )
        return cleaned


@dataclass
class NodeSchema:
    """A node type's declared inputs and outputs."""

    inputs: List[FieldSpec] = field(default_factory=list)
    outputs: List[FieldSpec] = field(default_factory=list)

    def input_map(self) -> Dict[str, FieldSpec]:
        return {spec.name: spec for spec in self.inputs}

    def validate_inputs(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate a node config against the input schema.

        Unknown keys are passed through untouched: node configs legitimately
        carry editor-only metadata, and rejecting them would break saved graphs.
        """
        cleaned: Dict[str, Any] = dict(config or {})
        errors: List[str] = []
        for spec in self.inputs:
            try:
                value = cleaned.get(spec.name)
                validated = spec.validate(value)
                if validated is not None or spec.name in cleaned:
                    cleaned[spec.name] = validated
            except ValidationError as exc:
                errors.append(exc.message)
        if errors:
            raise ValidationError(
                "; ".join(errors), details={"errors": errors}
            )
        return cleaned

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inputs": [spec.to_dict() for spec in self.inputs],
            "outputs": [spec.to_dict() for spec in self.outputs],
        }


# --------------------------------------------------------------------------- #
# Results and metrics
# --------------------------------------------------------------------------- #
@dataclass
class NodeMetrics:
    """Per-node execution counters merged into the run's aggregate metrics."""

    duration_ms: float = 0.0
    queued_ms: float = 0.0
    attempts: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    bytes_out: int = 0
    provider: Optional[str] = None
    model: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "duration_ms": round(self.duration_ms, 3),
            "queued_ms": round(self.queued_ms, 3),
            "attempts": self.attempts,
            "bytes_out": self.bytes_out,
        }
        if self.total_tokens or self.prompt_tokens or self.completion_tokens:
            payload["tokens"] = {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens
                or (self.prompt_tokens + self.completion_tokens),
            }
        if self.cost_usd:
            payload["cost_usd"] = round(self.cost_usd, 6)
        if self.provider:
            payload["provider"] = self.provider
        if self.model:
            payload["model"] = self.model
        if self.extra:
            payload["extra"] = self.extra
        return payload


@dataclass
class NodeResult:
    """A node's output value plus the metrics gathered while producing it."""

    output: Any = None
    metrics: NodeMetrics = field(default_factory=NodeMetrics)
    #: Branch label(s) this node activates. ``None`` means "all outgoing edges".
    branches: Optional[List[str]] = None
    logs: List[str] = field(default_factory=list)

    @classmethod
    def of(cls, output: Any, **metric_kwargs: Any) -> "NodeResult":
        return cls(output=output, metrics=NodeMetrics(**metric_kwargs))


def truncate_output(value: Any, max_bytes: Optional[int] = None) -> Any:
    """Cap a node output so one huge response cannot bloat the database.

    Returns the value unchanged when it is already small enough.
    """
    limit = settings.EXECUTION_MAX_OUTPUT_BYTES if max_bytes is None else max_bytes
    if limit <= 0:
        return value
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return {"truncated": True, "reason": "output is not JSON-serialisable",
                "repr": str(value)[:limit]}
    if len(encoded) <= limit:
        return value
    if isinstance(value, str):
        return value[:limit]
    return {
        "truncated": True,
        "reason": f"output exceeded {limit} bytes",
        "size_bytes": len(encoded),
        "preview": encoded[: min(limit, 4096)],
    }


# --------------------------------------------------------------------------- #
# Execution context
# --------------------------------------------------------------------------- #
class NodeContext(dict):
    """Execution context passed to every node.

    Subclasses ``dict`` deliberately: M1 executors receive ``context`` as a
    plain mapping keyed by node id and node name, and ``resolve_reference``
    walks it directly. Everything M4 adds lives in attributes, so old executors
    are completely unaffected.

    Reserved keys exposed to templates:
      ``vars``    — run variables (seeded from the execution's ``input_data``)
      ``loop``    — the innermost loop frame: ``{item, index, total}``
      ``run``     — run metadata: ``{execution_id, workflow_id, attempt}``
    """

    def __init__(
        self,
        *args: Any,
        execution_id: Optional[int] = None,
        workflow_id: Optional[int] = None,
        variables: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.execution_id = execution_id
        self.workflow_id = workflow_id
        self.variables: Dict[str, Any] = dict(variables or {})
        self.loop_stack: List[Dict[str, Any]] = []
        #: node_id -> conversation history for AI memory.
        self.memory: Dict[Any, List[Dict[str, str]]] = {}
        #: Populated by the engine; awaited by long-running nodes.
        self.cancel_event: Optional[asyncio.Event] = None
        self.log_sink: Optional[Callable[[str, str], None]] = None
        self._sync_reserved()

    # -- reserved key mirrors --------------------------------------------- #
    def _sync_reserved(self) -> None:
        self["vars"] = self.variables
        self["loop"] = self.loop_stack[-1] if self.loop_stack else {}
        self["run"] = {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
        }

    def set_variable(self, name: str, value: Any) -> None:
        self.variables[str(name)] = value
        self._sync_reserved()

    def get_variable(self, name: str, default: Any = None) -> Any:
        return self.variables.get(str(name), default)

    def push_loop(self, item: Any, index: int, total: int) -> None:
        self.loop_stack.append({"item": item, "index": index, "total": total})
        self._sync_reserved()

    def pop_loop(self) -> None:
        if self.loop_stack:
            self.loop_stack.pop()
        self._sync_reserved()

    def record_output(self, node_id: Any, node_name: Optional[str], value: Any) -> None:
        """Store a node's output under both its id and (if free) its name."""
        self[node_id] = value
        if node_name and node_name not in {"vars", "loop", "run"}:
            self.setdefault(node_name, value)

    # -- AI memory --------------------------------------------------------- #
    def remember(self, key: Any, role: str, content: str) -> None:
        history = self.memory.setdefault(key, [])
        history.append({"role": role, "content": content})
        limit = max(1, settings.AI_MEMORY_MAX_TURNS) * 2
        if len(history) > limit:
            del history[: len(history) - limit]

    def recall(self, key: Any) -> List[Dict[str, str]]:
        return list(self.memory.get(key, []))

    def log(self, message: str, level: str = "INFO") -> None:
        if self.log_sink is not None:
            try:
                self.log_sink(message, level)
            except Exception:  # pragma: no cover - logging must never break a node
                logger.debug("Context log sink raised", exc_info=True)

    def cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    def snapshot_variables(self) -> Dict[str, Any]:
        return dict(self.variables)


# --------------------------------------------------------------------------- #
# Runtime executor base
# --------------------------------------------------------------------------- #
class RuntimeNodeExecutor:
    """Base class for M4 node executors.

    Implements the same ``execute(node, context)`` contract as M1's
    ``BaseNodeExecutor`` and is registered as a *virtual* subclass of it from
    ``executors._register_m4_library``. Virtual registration is deliberate:
    inheriting directly would force this module to import ``executors`` at
    class-creation time, and ``executors`` imports this module back, producing
    a circular import whenever ``runtime`` is loaded first.

    Layers schema-driven validation, metrics and error classification on top of
    the base contract. Subclasses implement :meth:`run`, not :meth:`execute`.
    """

    label: str = "Node"
    category: str = "general"
    description: str = ""
    #: Editor node type names that should map to this executor.
    aliases: Sequence[str] = ()
    schema: NodeSchema = NodeSchema()
    #: When False the node is advertised but refuses to run (see settings).
    requires_flag: Optional[str] = None

    # -- introspection ----------------------------------------------------- #
    @property
    def config_schema(self) -> Dict[str, Any]:
        """Legacy palette shape kept for the M1 ``catalog()`` consumers."""
        return {
            spec.name: {
                key: value
                for key, value in spec.to_dict().items()
                if key != "name"
            }
            for spec in self.schema.inputs
        }

    def describe(self, node_type: str) -> Dict[str, Any]:
        return {
            "type": node_type,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "aliases": list(self.aliases),
            "enabled": self.is_enabled(),
            "schema": self.schema.to_dict(),
            "config_schema": self.config_schema,
        }

    def is_enabled(self) -> bool:
        if not self.requires_flag:
            return True
        return bool(getattr(settings, self.requires_flag, False))

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def config_of(node: Any) -> Dict[str, Any]:
        config = getattr(node, "config", None)
        return dict(config) if isinstance(config, dict) else {}

    def validate_config(self, node: Any) -> Dict[str, Any]:
        """Validate a node's stored config without executing it."""
        return self.schema.validate_inputs(self.config_of(node))

    # -- execution --------------------------------------------------------- #
    async def execute(self, node: Any, context: Dict[Any, Any]) -> Any:
        """Engine entry point. Returns the node's output value.

        Metrics are attached to ``context`` under ``__last_metrics__`` so the
        engine can persist them without changing the executor return contract.
        """
        result = await self.execute_result(node, context)
        if isinstance(context, dict):
            context["__last_metrics__"] = result.metrics
            if result.branches is not None:
                context["__last_branches__"] = result.branches
        return result.output

    async def execute_result(self, node: Any, context: Dict[Any, Any]) -> NodeResult:
        """Validate, run and measure. Always returns a :class:`NodeResult`."""
        if not self.is_enabled():
            raise NodeDisabledError(
                f"Node type {self.label!r} is disabled. "
                f"Set {self.requires_flag}=true to enable it.",
                details={"setting": self.requires_flag},
            )

        raw_config = self.config_of(node)
        try:
            config = self.schema.validate_inputs(raw_config)
        except ValidationError:
            raise
        except Exception as exc:  # defensive: schema bugs must not look transient
            raise ValidationError(
                f"Invalid configuration for {self.label}: {exc}"
            ) from exc

        started = time.perf_counter()
        outcome = await self.run(node, context, config)
        duration_ms = (time.perf_counter() - started) * 1000

        if isinstance(outcome, NodeResult):
            result = outcome
        else:
            result = NodeResult(output=outcome)
        if not result.metrics.duration_ms:
            result.metrics.duration_ms = duration_ms
        result.output = truncate_output(result.output)
        try:
            result.metrics.bytes_out = len(json.dumps(result.output, default=str))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            result.metrics.bytes_out = 0
        return result

    async def run(
        self, node: Any, context: Dict[Any, Any], config: Dict[str, Any]
    ) -> Any:
        """Execute the node. Override in subclasses.

        ``config`` has already been validated and coerced against
        :attr:`schema`. Return either a plain value or a :class:`NodeResult`.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} label={self.label!r}>"


def merge_metrics(target: Dict[str, Any], metrics: NodeMetrics) -> Dict[str, Any]:
    """Fold one node's metrics into a run-level aggregate dict (in place)."""
    target["nodes_executed"] = target.get("nodes_executed", 0) + 1
    target["total_duration_ms"] = round(
        target.get("total_duration_ms", 0.0) + metrics.duration_ms, 3
    )
    target["total_queued_ms"] = round(
        target.get("total_queued_ms", 0.0) + metrics.queued_ms, 3
    )
    target["total_attempts"] = target.get("total_attempts", 0) + metrics.attempts
    tokens = metrics.total_tokens or (metrics.prompt_tokens + metrics.completion_tokens)
    if tokens:
        target["prompt_tokens"] = target.get("prompt_tokens", 0) + metrics.prompt_tokens
        target["completion_tokens"] = (
            target.get("completion_tokens", 0) + metrics.completion_tokens
        )
        target["total_tokens"] = target.get("total_tokens", 0) + tokens
    if metrics.cost_usd:
        target["cost_usd"] = round(target.get("cost_usd", 0.0) + metrics.cost_usd, 6)
    return target
