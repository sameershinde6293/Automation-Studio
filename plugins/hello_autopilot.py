"""hello_autopilot: the reference plugin for the D.8 plugin interface.

Contract (docs/PLUGINS.md):
  * one file in ``plugins/``, registered in ``config/plugins_config.json``
  * module-level ``PLUGIN_API = 1``
  * exactly ONE ``BaseModule`` subclass with ``run(context) -> dict``
  * ``run`` receives a plain dict of CLI ``--arg k=v`` pairs and returns
    either a standard response dict or any plain dict (the orchestrator
    normalizes it). RULE 1 applies: import core services only via the
    injected container (``self.db``, ``self.config``, ...), never from
    ``modules/*``.
"""

from __future__ import annotations

from typing import Any, Dict

from core.service_container import BaseModule, ServiceContainer

PLUGIN_API = 1

MODULE_NAME = "hello_autopilot"


class HelloAutopilotPlugin(BaseModule):
    """Echo back who called us + a project-count health read."""

    def __init__(
        self, container: ServiceContainer, module_name: str = MODULE_NAME
    ) -> None:
        super().__init__(container, module_name)

    def is_optional_module(self) -> bool:
        return True

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        projects = 0
        try:
            row = self.db.db.fetch_one(
                "SELECT COUNT(*) AS n FROM projects"
            )
            projects = int((row or {}).get("n") or 0)
        except Exception as exc:  # noqa: BLE001 - demo stays honest
            return self.make_response(
                False, error=f"project count failed: {exc}"
            )
        return {
            "success": True,
            "data": {
                "message": f"Hello from {self.module_name}!",
                "context_keys": sorted(str(k) for k in context),
                "projects_in_db": projects,
            },
        }
