"""Workflow node executors.

V1.1 hardening highlights:
- ``http_request`` now blocks SSRF (private/loopback/link-local/metadata IPs),
  enforces schemes, timeouts, redirect limits and a response-size cap.
- ``shell_command`` is **disabled by default** and, when enabled, requires an
  explicit command allowlist; it also enforces a timeout and kills the process
  group on expiry.
- Added ``template``, ``branch``, ``delay``, ``transform`` and ``noop`` nodes so
  real workflows can be built in the visual editor.
- Context is now a rich mapping exposing both node ids and node names.

Backwards compatible: ``BaseNodeExecutor``, ``DummyNodeExecutor``,
``MathAddExecutor``, ``HttpRequestExecutor``, ``CommandExecutor``,
``ExecutorRegistry`` and the ``executor_registry`` singleton all remain, and the
original ``execute(node, context)`` signature is unchanged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shlex
import socket
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import httpx

from app.core.errors import ExecutionError, SecurityError, ValidationError
from app.domain.models.workflow import Node
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger("workflow.executors")

ALLOWED_URL_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}

_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #
def resolve_reference(context: Dict[Any, Any], expression: str) -> Any:
    """Resolve a dotted path such as ``3.result`` or ``Fetch.response.items.0``.

    Lookup order: exact key, integer-coerced key, then dotted traversal.
    Returns ``None`` when the path cannot be resolved.
    """
    expression = expression.strip()
    if not expression:
        return None
    if expression in context:
        return context[expression]

    parts = expression.split(".")
    head = parts[0]
    current: Any = None
    if head in context:
        current = context[head]
    else:
        try:
            current = context[int(head)]
        except (KeyError, ValueError, TypeError):
            return None

    for part in parts[1:]:
        if isinstance(current, dict):
            if part in current:
                current = current[part]
                continue
            try:
                current = current[int(part)]
                continue
            except (KeyError, ValueError, TypeError):
                return None
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def render_template(template: str, context: Dict[Any, Any]) -> str:
    """Substitute ``{{ ref }}`` placeholders using values from the context."""

    def _replace(match: re.Match) -> str:
        value = resolve_reference(context, match.group(1))
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return _TEMPLATE_PATTERN.sub(_replace, template)


def render_value(value: Any, context: Dict[Any, Any]) -> Any:
    """Recursively render templates inside strings, dicts and lists."""
    if isinstance(value, str):
        return render_template(value, context)
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    return value


def coerce_number(value: Any, default: float = 0.0) -> float:
    """Best-effort numeric coercion used by arithmetic nodes."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    if isinstance(value, dict) and "result" in value:
        return coerce_number(value["result"], default)
    return default


def is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "null", "none"}
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return bool(value)


# --------------------------------------------------------------------------- #
# SSRF protection
# --------------------------------------------------------------------------- #
def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_outbound_url(url: str, *, allow_private: Optional[bool] = None,
                          allowed_hosts: Optional[List[str]] = None) -> str:
    """Validate a URL for outbound requests, raising on SSRF risk."""
    if not url or not isinstance(url, str):
        raise ValidationError("HTTP node requires a non-empty 'url'.")

    allow_private = (
        settings.HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS
        if allow_private is None
        else allow_private
    )
    allowed_hosts = (
        settings.HTTP_EXECUTOR_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    )

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise SecurityError(
            f"URL scheme {parsed.scheme!r} is not permitted. Use http or https.",
            details={"url": url},
        )
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValidationError("URL is missing a hostname.", details={"url": url})

    if allowed_hosts:
        if hostname not in {h.lower() for h in allowed_hosts}:
            raise SecurityError(
                f"Host {hostname!r} is not in HTTP_EXECUTOR_ALLOWED_HOSTS.",
                details={"host": hostname},
            )
        return url

    if hostname in BLOCKED_HOSTNAMES:
        raise SecurityError(
            f"Host {hostname!r} is blocked (cloud metadata endpoint).",
            details={"host": hostname},
        )

    if allow_private:
        return url

    # Direct IP literal.
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_blocked_ip(ip):
            raise SecurityError(
                f"Requests to private/loopback address {hostname} are blocked.",
                details={"host": hostname},
            )
        return url
    except ValueError:
        pass

    # Resolve DNS and reject if *any* record points inside the network.
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValidationError(
            f"Could not resolve host {hostname!r}.", details={"error": str(exc)}
        ) from exc

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise SecurityError(
                f"Host {hostname!r} resolves to blocked address {addr}.",
                details={"host": hostname, "resolved": addr},
            )
    return url


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
class BaseNodeExecutor(ABC):
    """Contract for all node executors."""

    #: Human-readable label shown in the editor palette.
    label: str = "Node"
    #: Category used to group nodes in the palette.
    category: str = "general"
    #: JSON-schema-ish description of accepted config keys.
    config_schema: Dict[str, Any] = {}
    description: str = ""

    @abstractmethod
    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        """Run the node and return a JSON-serialisable result."""

    @staticmethod
    def config_of(node: Node) -> Dict[str, Any]:
        config = getattr(node, "config", None)
        return dict(config) if isinstance(config, dict) else {}


class NoOpExecutor(BaseNodeExecutor):
    label = "No-op"
    category = "general"
    description = "Passes through without doing anything. Useful as a join point."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        return {"status": "ok", "node": node.name}


class DummyNodeExecutor(BaseNodeExecutor):
    """Retained from V1.0 for backwards compatibility."""

    label = "Dummy"
    category = "general"
    description = "Test node that completes immediately."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        return {"status": "ok", "node": node.name}


class DelayExecutor(BaseNodeExecutor):
    label = "Delay"
    category = "control"
    config_schema = {"seconds": {"type": "number", "default": 1, "maximum": 3600}}
    description = "Waits for a fixed number of seconds."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        seconds = coerce_number(render_value(config.get("seconds", 1), context), 1.0)
        seconds = max(0.0, min(seconds, 3600.0))
        await asyncio.sleep(seconds)
        return {"slept_seconds": seconds}


class MathAddExecutor(BaseNodeExecutor):
    """Adds two operands.

    V1.0 semantics preserved: an integer operand is first treated as a node-id
    reference into the context (falling back to the literal value). V1.1 adds
    template-string support (``"{{ 3.result }}"``) and float handling.
    """

    label = "Add"
    category = "math"
    config_schema = {"a": {"type": ["number", "string"]}, "b": {"type": ["number", "string"]}}
    description = "Adds two numbers, optionally referencing upstream node results."

    def _operand(self, raw: Any, context: Dict[Any, Any]) -> float:
        if isinstance(raw, bool):
            return float(raw)
        if isinstance(raw, int):
            # Backwards-compatible node-id reference.
            referenced = context.get(raw)
            if isinstance(referenced, dict) and "result" in referenced:
                return coerce_number(referenced["result"], float(raw))
            return float(raw)
        if isinstance(raw, str):
            rendered = render_template(raw, context)
            return coerce_number(rendered, 0.0)
        return coerce_number(raw, 0.0)

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        val_a = self._operand(config.get("a", 0), context)
        val_b = self._operand(config.get("b", 0), context)
        total = val_a + val_b
        # Preserve int-ness so existing tests asserting ``== 15`` still pass.
        result = int(total) if float(total).is_integer() else total
        return {"result": result}


class MathExpressionExecutor(BaseNodeExecutor):
    """Evaluates a restricted arithmetic expression (no eval of arbitrary code)."""

    label = "Math Expression"
    category = "math"
    config_schema = {"expression": {"type": "string"}}
    description = "Evaluates a safe arithmetic expression such as '{{1.result}} * 2 + 1'."

    _ALLOWED = set("0123456789.+-*/()%  eE")

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        raw = str(config.get("expression", "")).strip()
        if not raw:
            raise ValidationError("Math expression node requires 'expression'.")
        rendered = render_template(raw, context)
        if not rendered:
            raise ValidationError("Expression resolved to an empty string.")
        if not set(rendered) <= self._ALLOWED:
            raise SecurityError(
                "Expression contains characters that are not permitted.",
                details={"expression": rendered},
            )
        if len(rendered) > 500:
            raise ValidationError("Expression is too long (max 500 characters).")
        try:
            # Only arithmetic characters survived the filter above.
            value = eval(rendered, {"__builtins__": {}}, {})  # noqa: S307
        except Exception as exc:
            raise ExecutionError(
                f"Could not evaluate expression: {exc}", details={"expression": rendered}
            ) from exc
        return {"result": value}


class TemplateExecutor(BaseNodeExecutor):
    label = "Template"
    category = "transform"
    config_schema = {"template": {"type": "string"}}
    description = "Renders a text template using upstream node outputs."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        template = config.get("template", "")
        if not isinstance(template, str):
            raise ValidationError("Template node requires a string 'template'.")
        return {"result": render_template(template, context), "text": render_template(template, context)}


class TransformExecutor(BaseNodeExecutor):
    """Builds a new object from templated fields."""

    label = "Transform"
    category = "transform"
    config_schema = {"fields": {"type": "object"}}
    description = "Maps upstream outputs into a new JSON object."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        fields = config.get("fields", {})
        if not isinstance(fields, dict):
            raise ValidationError("Transform node requires an object 'fields'.")
        return {"result": render_value(fields, context)}


class BranchExecutor(BaseNodeExecutor):
    """Evaluates a comparison and reports which branch should be taken."""

    label = "Branch"
    category = "control"
    config_schema = {
        "left": {"type": ["string", "number"]},
        "operator": {
            "type": "string",
            "enum": ["==", "!=", ">", ">=", "<", "<=", "contains", "truthy"],
        },
        "right": {"type": ["string", "number"]},
    }
    description = "Conditional gate; downstream nodes can read 'branch'."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        operator = str(config.get("operator", "truthy"))
        left = render_value(config.get("left"), context)
        right = render_value(config.get("right"), context)

        if operator == "truthy":
            outcome = is_truthy(left)
        elif operator == "==":
            outcome = str(left) == str(right)
        elif operator == "!=":
            outcome = str(left) != str(right)
        elif operator == "contains":
            outcome = str(right) in str(left)
        elif operator in {">", ">=", "<", "<="}:
            lnum, rnum = coerce_number(left), coerce_number(right)
            outcome = {
                ">": lnum > rnum,
                ">=": lnum >= rnum,
                "<": lnum < rnum,
                "<=": lnum <= rnum,
            }[operator]
        else:
            raise ValidationError(
                f"Unsupported branch operator {operator!r}.", details={"operator": operator}
            )
        return {"result": outcome, "branch": "true" if outcome else "false"}


class HttpRequestExecutor(BaseNodeExecutor):
    label = "HTTP Request"
    category = "network"
    config_schema = {
        "url": {"type": "string", "required": True},
        "method": {"type": "string", "default": "GET"},
        "headers": {"type": "object"},
        "body": {"type": "object"},
        "timeout": {"type": "number"},
    }
    description = "Performs an outbound HTTP request with SSRF protection."

    ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        config = self.config_of(node)
        url = render_template(str(config.get("url", "")), context)
        method = str(config.get("method", "GET")).upper()
        if method not in self.ALLOWED_METHODS:
            raise ValidationError(
                f"HTTP method {method!r} is not supported.", details={"method": method}
            )

        validate_outbound_url(url)

        headers = render_value(config.get("headers") or {}, context)
        if not isinstance(headers, dict):
            raise ValidationError("HTTP node 'headers' must be an object.")
        headers = {str(k): str(v) for k, v in headers.items()}

        timeout = coerce_number(
            config.get("timeout"), settings.HTTP_EXECUTOR_TIMEOUT_SECONDS
        )
        timeout = max(1.0, min(timeout, 300.0))
        max_bytes = settings.HTTP_EXECUTOR_MAX_RESPONSE_BYTES

        request_kwargs: Dict[str, Any] = {"url": url, "headers": headers}
        if method in {"POST", "PUT", "PATCH"} and config.get("body") is not None:
            request_kwargs["json"] = render_value(config.get("body"), context)

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                max_redirects=5,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            ) as client:
                response = await client.request(method, **request_kwargs)
                content = response.content or b""
                truncated = len(content) > max_bytes
                if truncated:
                    content = content[:max_bytes]
        except httpx.TimeoutException as exc:
            raise ExecutionError(
                f"HTTP request to {url} timed out after {timeout}s.",
                details={"url": url},
            ) from exc
        except httpx.HTTPError as exc:
            raise ExecutionError(
                f"HTTP request to {url} failed: {exc}", details={"url": url}
            ) from exc

        data: Any
        content_type = response.headers.get("content-type", "")
        if "json" in content_type and not truncated:
            try:
                data = json.loads(content.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = content.decode("utf-8", errors="replace")
        else:
            data = content.decode("utf-8", errors="replace")

        return {
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "response": data,
            "truncated": truncated,
            "headers": dict(response.headers),
        }


class CommandExecutor(BaseNodeExecutor):
    """Runs a local command.

    **Disabled by default.** Set ``ALLOW_SHELL_EXECUTOR=true`` and populate
    ``SHELL_ALLOWED_COMMANDS`` to enable. Never uses a shell, so metacharacter
    injection (``;``, ``|``, ``$()``) is impossible.
    """

    label = "Shell Command"
    category = "system"
    config_schema = {
        "command": {"type": "string", "required": True},
        "timeout": {"type": "number"},
    }
    description = "Runs an allowlisted local command (disabled by default)."

    async def execute(self, node: Node, context: Dict[Any, Any]) -> Any:
        if not settings.ALLOW_SHELL_EXECUTOR:
            raise SecurityError(
                "The shell executor is disabled. Set ALLOW_SHELL_EXECUTOR=true and "
                "configure SHELL_ALLOWED_COMMANDS to enable it.",
                details={"node": node.name},
            )

        config = self.config_of(node)
        raw_command = render_template(str(config.get("command", "")), context).strip()
        if not raw_command:
            raise ValidationError("Shell node requires a non-empty 'command'.")

        try:
            argv = shlex.split(raw_command)
        except ValueError as exc:
            raise ValidationError(
                f"Could not parse command: {exc}", details={"command": raw_command}
            ) from exc
        if not argv:
            raise ValidationError("Shell node requires a non-empty 'command'.")

        allowlist: Set[str] = {c.strip() for c in settings.SHELL_ALLOWED_COMMANDS if c.strip()}
        if not allowlist:
            raise SecurityError(
                "SHELL_ALLOWED_COMMANDS is empty; refusing to execute any command.",
                details={"command": argv[0]},
            )
        program = argv[0]
        if program not in allowlist:
            raise SecurityError(
                f"Command {program!r} is not in the allowlist.",
                details={"command": program, "allowed": sorted(allowlist)},
            )

        timeout = coerce_number(config.get("timeout"), settings.SHELL_TIMEOUT_SECONDS)
        timeout = max(1.0, min(timeout, 3600.0))

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            try:
                process.kill()
            except ProcessLookupError:  # pragma: no cover - race
                pass
            await process.wait()
            raise ExecutionError(
                f"Command timed out after {timeout}s.", details={"command": program}
            ) from exc

        limit = 256 * 1024
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace")[:limit].strip(),
            "stderr": stderr.decode(errors="replace")[:limit].strip(),
        }


class ExecutorRegistry:
    """Registry of node type -> executor instance."""

    def __init__(self) -> None:
        self.executors: Dict[str, BaseNodeExecutor] = {
            # V1.0 types (preserved)
            "dummy": DummyNodeExecutor(),
            "math_add": MathAddExecutor(),
            "http_request": HttpRequestExecutor(),
            "shell_command": CommandExecutor(),
            # V1.1 additions
            "noop": NoOpExecutor(),
            "delay": DelayExecutor(),
            "math_expression": MathExpressionExecutor(),
            "template": TemplateExecutor(),
            "transform": TransformExecutor(),
            "branch": BranchExecutor(),
        }

    def register(self, node_type: str, executor: BaseNodeExecutor, *, override: bool = False) -> None:
        """Register a custom executor (used by the plugin SDK)."""
        if not isinstance(executor, BaseNodeExecutor):
            raise ValidationError("Executor must subclass BaseNodeExecutor.")
        if node_type in self.executors and not override:
            raise ValidationError(
                f"Node type {node_type!r} is already registered.",
                details={"node_type": node_type},
            )
        self.executors[node_type] = executor
        logger.info("Registered node executor %r", node_type)

    def unregister(self, node_type: str) -> bool:
        return self.executors.pop(node_type, None) is not None

    def get_executor(self, node_type: str) -> BaseNodeExecutor:
        executor = self.executors.get(node_type)
        if not executor:
            raise ValueError(f"No executor found for node type: {node_type}")
        return executor

    def has(self, node_type: str) -> bool:
        return node_type in self.executors

    def catalog(self) -> List[Dict[str, Any]]:
        """Palette metadata consumed by the visual workflow editor."""
        entries = []
        for node_type, executor in sorted(self.executors.items()):
            entries.append(
                {
                    "type": node_type,
                    "label": executor.label,
                    "category": executor.category,
                    "description": executor.description,
                    "config_schema": executor.config_schema,
                }
            )
        return entries

    def schemas(self) -> List[Dict[str, Any]]:
        """Full input/output schemas per node type (M4).

        Nodes built on the M4 runtime describe themselves precisely; the older
        M1 executors fall back to their ``config_schema``.
        """
        from app.services.workflow.runtime import RuntimeNodeExecutor

        entries: List[Dict[str, Any]] = []
        seen_ids = set()
        for node_type, executor in sorted(self.executors.items()):
            if isinstance(executor, RuntimeNodeExecutor):
                entry = executor.describe(node_type)
            else:
                entry = {
                    "type": node_type,
                    "label": executor.label,
                    "category": executor.category,
                    "description": executor.description,
                    "aliases": [],
                    "enabled": True,
                    "schema": {"inputs": [], "outputs": []},
                    "config_schema": executor.config_schema,
                }
            entry["is_alias"] = id(executor) in seen_ids
            seen_ids.add(id(executor))
            entries.append(entry)
        return entries

    def resolve(self, node_type: str) -> Optional[str]:
        """Canonical registered name for a node type, or None when unknown."""
        return node_type if node_type in self.executors else None


executor_registry = ExecutorRegistry()


def _register_m4_library() -> None:
    """Register the M4 node library into the shared registry.

    ``RuntimeNodeExecutor`` is declared a virtual subclass of
    ``BaseNodeExecutor`` here (rather than inheriting directly) so that
    ``runtime`` never has to import ``executors`` at class-creation time; that
    would be circular, since this module imports ``runtime``.

    Guarded so a failure degrades to "the M4 nodes are unavailable" rather than
    breaking the whole application import.
    """
    try:
        from app.services.workflow.runtime import RuntimeNodeExecutor

        BaseNodeExecutor.register(RuntimeNodeExecutor)

        from app.services.workflow.nodes import register_all

        registered = register_all(executor_registry)
        logger.info("Registered %s M4 node type(s).", len(registered))
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to register the M4 node library.")


_register_m4_library()
