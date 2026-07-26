"""Plugin SDK: hook registry and node-type extension surface.

Backwards compatible with V1.0 (``PluginSDK.register_hook`` / ``trigger_hook``
and the ``plugin_sdk`` singleton behave the same for sync hooks).

V1.1 improvements:
- hook failures are **logged** instead of silently swallowed (V1.0 used a bare
  ``pass``, which hid every plugin bug)
- ``HookResult`` records per-hook success/failure so callers can react
- async hooks via ``trigger_hook_async``
- ``unregister_hook`` / ``clear_hooks`` for clean teardown
- plugins can contribute workflow node executors through ``register_node_type``
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.infrastructure.logging.logger import get_logger

logger = get_logger("plugin_sdk")


@dataclass
class HookResult:
    """Outcome of a single hook invocation."""

    hook: str
    plugin: str
    value: Any = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class HookRegistration:
    callback: Callable
    plugin: str = "unknown"
    priority: int = 100
    metadata: Dict[str, Any] = field(default_factory=dict)


class PluginSDK:
    #: Well-known hook names core modules trigger.
    HOOK_WORKFLOW_NODE_BEFORE = "workflow.node.before"
    HOOK_WORKFLOW_NODE_AFTER = "workflow.node.after"
    HOOK_MEDIA_ASSET_PROCESSED = "media.asset.processed"
    HOOK_AI_BEFORE_GENERATE = "ai.before_generate"
    HOOK_AI_AFTER_GENERATE = "ai.after_generate"
    HOOK_APP_STARTUP = "app.startup"
    HOOK_APP_SHUTDOWN = "app.shutdown"

    def __init__(self) -> None:
        self._hooks: Dict[str, List[HookRegistration]] = {}
        self._node_types: Dict[str, str] = {}

    # -- V1.0-compatible view -------------------------------------------- #
    @property
    def registered_hooks(self) -> Dict[str, List[Callable]]:
        """Legacy accessor returning plain callables per hook name."""
        return {
            name: [reg.callback for reg in regs] for name, regs in self._hooks.items()
        }

    # -- registration ------------------------------------------------------ #
    def register_hook(
        self,
        hook_name: str,
        callback: Callable,
        *,
        plugin: str = "unknown",
        priority: int = 100,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Callable:
        if not callable(callback):
            raise TypeError("Hook callback must be callable.")
        registration = HookRegistration(
            callback=callback,
            plugin=plugin,
            priority=priority,
            metadata=metadata or {},
        )
        self._hooks.setdefault(hook_name, []).append(registration)
        # Lower priority number runs first; stable for equal priorities.
        self._hooks[hook_name].sort(key=lambda r: r.priority)
        logger.debug("Registered hook %r from plugin %r", hook_name, plugin)
        return callback

    def unregister_hook(self, hook_name: str, callback: Callable) -> bool:
        registrations = self._hooks.get(hook_name)
        if not registrations:
            return False
        remaining = [r for r in registrations if r.callback is not callback]
        if len(remaining) == len(registrations):
            return False
        if remaining:
            self._hooks[hook_name] = remaining
        else:
            self._hooks.pop(hook_name, None)
        return True

    def unregister_plugin(self, plugin: str) -> int:
        """Remove every hook registered by ``plugin``. Returns count removed."""
        removed = 0
        for hook_name in list(self._hooks):
            keep = [r for r in self._hooks[hook_name] if r.plugin != plugin]
            removed += len(self._hooks[hook_name]) - len(keep)
            if keep:
                self._hooks[hook_name] = keep
            else:
                self._hooks.pop(hook_name, None)
        for node_type, owner in list(self._node_types.items()):
            if owner == plugin:
                self._unregister_node_type(node_type)
        return removed

    def clear_hooks(self, hook_name: Optional[str] = None) -> None:
        if hook_name is None:
            self._hooks.clear()
        else:
            self._hooks.pop(hook_name, None)

    def hook_count(self, hook_name: str) -> int:
        return len(self._hooks.get(hook_name, []))

    def list_hooks(self) -> Dict[str, List[Dict[str, Any]]]:
        return {
            name: [
                {
                    "plugin": r.plugin,
                    "priority": r.priority,
                    "callback": getattr(r.callback, "__name__", repr(r.callback)),
                    "is_async": inspect.iscoroutinefunction(r.callback),
                    "metadata": r.metadata,
                }
                for r in regs
            ]
            for name, regs in self._hooks.items()
        }

    # -- invocation --------------------------------------------------------- #
    def trigger_hook(self, hook_name: str, **kwargs: Any) -> List[Any]:
        """Run every sync hook, isolating and logging failures.

        Returns the list of successful return values (V1.0 semantics).
        """
        return [r.value for r in self.trigger_hook_detailed(hook_name, **kwargs) if r.ok]

    def trigger_hook_detailed(self, hook_name: str, **kwargs: Any) -> List[HookResult]:
        results: List[HookResult] = []
        for registration in list(self._hooks.get(hook_name, [])):
            name = getattr(registration.callback, "__name__", repr(registration.callback))
            try:
                if inspect.iscoroutinefunction(registration.callback):
                    logger.warning(
                        "Hook %r from plugin %r is async; use trigger_hook_async.",
                        hook_name, registration.plugin,
                    )
                    results.append(
                        HookResult(
                            hook=hook_name,
                            plugin=registration.plugin,
                            error="async hook invoked synchronously",
                        )
                    )
                    continue
                value = registration.callback(**kwargs)
                results.append(
                    HookResult(hook=hook_name, plugin=registration.plugin, value=value)
                )
            except Exception as exc:  # noqa: BLE001 - plugin faults must not propagate
                logger.exception(
                    "Hook %r from plugin %r (%s) raised.",
                    hook_name, registration.plugin, name,
                )
                results.append(
                    HookResult(
                        hook=hook_name,
                        plugin=registration.plugin,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results

    async def trigger_hook_async(self, hook_name: str, **kwargs: Any) -> List[HookResult]:
        """Run every hook, awaiting async ones."""
        results: List[HookResult] = []
        for registration in list(self._hooks.get(hook_name, [])):
            try:
                value = registration.callback(**kwargs)
                if inspect.isawaitable(value):
                    value = await value
                results.append(
                    HookResult(hook=hook_name, plugin=registration.plugin, value=value)
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Async hook %r from plugin %r raised.", hook_name, registration.plugin
                )
                results.append(
                    HookResult(
                        hook=hook_name,
                        plugin=registration.plugin,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results

    # -- node type extension ------------------------------------------------ #
    def register_node_type(
        self, node_type: str, executor: Any, *, plugin: str = "unknown", override: bool = False
    ) -> None:
        """Contribute a workflow node executor from a plugin."""
        from app.services.workflow.executors import executor_registry

        executor_registry.register(node_type, executor, override=override)
        self._node_types[node_type] = plugin
        logger.info("Plugin %r registered node type %r", plugin, node_type)

    def _unregister_node_type(self, node_type: str) -> bool:
        from app.services.workflow.executors import executor_registry

        self._node_types.pop(node_type, None)
        return executor_registry.unregister(node_type)

    def list_node_types(self) -> Dict[str, str]:
        return dict(self._node_types)


plugin_sdk = PluginSDK()
