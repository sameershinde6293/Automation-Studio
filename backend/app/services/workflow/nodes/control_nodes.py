"""Control-flow nodes: start, end, variable, condition, loop, delay."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from app.core.errors import ValidationError
from app.infrastructure.config.settings import settings
from app.services.workflow.executors import (
    coerce_number,
    is_truthy,
    render_template,
    render_value,
    resolve_reference,
)
from app.services.workflow.runtime import (
    FieldSpec,
    NodeContext,
    NodeResult,
    NodeSchema,
    RuntimeNodeExecutor,
)


class StartNode(RuntimeNodeExecutor):
    """Entry point. Seeds run variables from its config."""

    label = "Start"
    category = "control"
    description = "Workflow entry point. Optionally seeds initial variables."
    aliases = ("start_node",)
    schema = NodeSchema(
        inputs=[
            FieldSpec("name", "string", default="Main", description="Workflow label"),
            FieldSpec(
                "variables",
                "object",
                description="Initial variables merged into the run context",
            ),
        ],
        outputs=[
            FieldSpec("started", "boolean", description="Always true"),
            FieldSpec("variables", "object", description="Seeded variables"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        variables = config.get("variables") or {}
        if isinstance(context, NodeContext) and isinstance(variables, dict):
            for key, value in variables.items():
                # Do not clobber variables supplied by the caller at run time.
                if key not in context.variables:
                    context.set_variable(key, render_value(value, context))
        return {
            "started": True,
            "name": config.get("name") or "Main",
            "variables": dict(variables),
        }


class EndNode(RuntimeNodeExecutor):
    """Terminal node. Collects a final result payload for the run."""

    label = "End"
    category = "control"
    description = "Workflow exit point. Captures the run's final output."
    aliases = ("end_node",)
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "output",
                "any",
                description="Templated final output, e.g. '{{ Summarise.result }}'",
            ),
            FieldSpec("status", "string", default="success"),
        ],
        outputs=[
            FieldSpec("completed", "boolean"),
            FieldSpec("output", "any", description="Resolved final output"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        output = render_value(config.get("output"), context)
        result = {
            "completed": True,
            "status": config.get("status") or "success",
            "output": output,
        }
        if isinstance(context, NodeContext):
            context.set_variable("__result__", output)
        return result


class VariableNode(RuntimeNodeExecutor):
    """Reads, sets or mutates a run variable."""

    label = "Variable"
    category = "data"
    description = "Set, get, append to or increment a workflow variable."
    aliases = ("variable_node", "set_variable")
    schema = NodeSchema(
        inputs=[
            FieldSpec("name", "string", required=True, description="Variable name"),
            FieldSpec(
                "operation",
                "string",
                default="set",
                enum=["set", "get", "append", "increment", "delete"],
            ),
            FieldSpec("value", "any", description="Value (templates supported)"),
        ],
        outputs=[
            FieldSpec("name", "string"),
            FieldSpec("value", "any", description="Resulting variable value"),
            FieldSpec("operation", "string"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        name = str(config.get("name") or "").strip()
        if not name:
            raise ValidationError("Variable node requires a 'name'.")
        operation = str(config.get("operation") or "set").lower()
        value = render_value(config.get("value"), context)

        if not isinstance(context, NodeContext):
            # Legacy plain-dict context: degrade to storing on the dict itself.
            store: Dict[str, Any] = context.setdefault("vars", {})  # type: ignore[assignment]
        else:
            store = context.variables

        current = store.get(name)

        if operation == "get":
            resolved = current
        elif operation == "delete":
            store.pop(name, None)
            resolved = None
        elif operation == "append":
            if isinstance(current, list):
                resolved = current + [value]
            elif current is None:
                resolved = [value]
            else:
                resolved = [current, value]
            store[name] = resolved
        elif operation == "increment":
            resolved = coerce_number(current, 0.0) + coerce_number(value, 1.0)
            if float(resolved).is_integer():
                resolved = int(resolved)
            store[name] = resolved
        else:  # set
            resolved = value
            store[name] = resolved

        if isinstance(context, NodeContext):
            context._sync_reserved()
        return {"name": name, "value": resolved, "operation": operation}


class ConditionNode(RuntimeNodeExecutor):
    """Branch gate.

    Returns the branch label to activate. The engine reads ``branches`` and only
    follows outgoing edges whose ``label`` matches (gap R3 — before M4 the
    engine ignored branch results entirely and ran *both* sides).
    """

    label = "Condition"
    category = "control"
    description = (
        "Evaluates a comparison and activates only the matching outgoing edge "
        "(edge label 'true' or 'false')."
    )
    aliases = ("condition_node", "if")
    OPERATORS = [
        "==", "!=", ">", ">=", "<", "<=",
        "contains", "not_contains", "starts_with", "ends_with",
        "is_empty", "is_not_empty", "truthy",
    ]
    schema = NodeSchema(
        inputs=[
            FieldSpec("left", "any", description="Left operand (templates supported)"),
            FieldSpec("operator", "string", default="truthy", enum=OPERATORS),
            FieldSpec("right", "any", description="Right operand"),
            FieldSpec(
                "true_label",
                "string",
                default="true",
                description="Edge label followed when the condition holds",
            ),
            FieldSpec("false_label", "string", default="false"),
        ],
        outputs=[
            FieldSpec("result", "boolean"),
            FieldSpec("branch", "string", description="Activated edge label"),
        ],
    )

    @staticmethod
    def evaluate(operator: str, left: Any, right: Any) -> bool:
        if operator == "truthy":
            return is_truthy(left)
        if operator == "is_empty":
            return not is_truthy(left)
        if operator == "is_not_empty":
            return is_truthy(left)
        if operator == "==":
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return float(left) == float(right)
            return str(left) == str(right)
        if operator == "!=":
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return float(left) != float(right)
            return str(left) != str(right)
        if operator == "contains":
            if isinstance(left, (list, tuple, dict)):
                return right in left
            return str(right) in str(left)
        if operator == "not_contains":
            if isinstance(left, (list, tuple, dict)):
                return right not in left
            return str(right) not in str(left)
        if operator == "starts_with":
            return str(left).startswith(str(right))
        if operator == "ends_with":
            return str(left).endswith(str(right))
        if operator in {">", ">=", "<", "<="}:
            lnum, rnum = coerce_number(left), coerce_number(right)
            return {
                ">": lnum > rnum,
                ">=": lnum >= rnum,
                "<": lnum < rnum,
                "<=": lnum <= rnum,
            }[operator]
        raise ValidationError(
            f"Unsupported condition operator {operator!r}.",
            details={"operator": operator, "supported": ConditionNode.OPERATORS},
        )

    async def run(self, node, context, config) -> Any:
        operator = str(config.get("operator") or "truthy")
        left = render_value(config.get("left"), context)
        right = render_value(config.get("right"), context)
        outcome = self.evaluate(operator, left, right)

        true_label = str(config.get("true_label") or "true")
        false_label = str(config.get("false_label") or "false")
        branch = true_label if outcome else false_label

        return NodeResult(
            output={"result": outcome, "branch": branch, "operator": operator},
            branches=[branch],
        )


class LoopNode(RuntimeNodeExecutor):
    """Iterates a collection or a fixed count.

    Two modes:

    ``collect`` (default)
        Evaluate a templated expression once per item and return the list of
        rendered values. Self-contained, no graph cycle required.

    ``fanout``
        Emit the resolved item list for downstream nodes and expose
        ``{{ loop.item }}``; the engine's loop support re-runs the loop body
        sub-graph once per item when a back-edge is present.

    A hard iteration cap (``WORKFLOW_MAX_LOOP_ITERATIONS``) guards runaway loops.
    """

    label = "Loop"
    category = "control"
    description = "Iterates over a collection or a fixed count."
    aliases = ("loop_node", "for_each")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "mode", "string", default="collect", enum=["collect", "fanout"]
            ),
            FieldSpec(
                "items",
                "any",
                description="Array, JSON array string, or '{{ ref }}' to a list",
            ),
            FieldSpec(
                "count",
                "integer",
                minimum=0,
                description="Iterate 0..count-1 when 'items' is not supplied",
            ),
            FieldSpec(
                "template",
                "string",
                description="Rendered per item in 'collect' mode; '{{ loop.item }}' available",
            ),
            FieldSpec(
                "max_iterations",
                "integer",
                minimum=1,
                description="Per-node cap (still bounded by the global limit)",
            ),
        ],
        outputs=[
            FieldSpec("items", "array", description="Resolved iteration items"),
            FieldSpec("results", "array", description="Per-item rendered results"),
            FieldSpec("count", "integer"),
        ],
    )

    @staticmethod
    def resolve_items(config: Dict[str, Any], context: Any) -> List[Any]:
        raw = config.get("items")
        items: List[Any]
        if raw is None or raw == "":
            count = config.get("count")
            total = int(coerce_number(count, 0))
            return list(range(max(0, total)))
        if isinstance(raw, str):
            text = raw.strip()
            # A bare "{{ ref }}" should resolve to the referenced object itself
            # rather than its string rendering.
            if text.startswith("{{") and text.endswith("}}") and text.count("{{") == 1:
                resolved = resolve_reference(context, text[2:-2].strip())
                if isinstance(resolved, (list, tuple)):
                    return list(resolved)
                if isinstance(resolved, dict):
                    return list(resolved.items())
                if resolved is None:
                    return []
                return [resolved]
            rendered = render_template(text, context)
            try:
                parsed = json.loads(rendered)
            except json.JSONDecodeError:
                return [
                    part.strip() for part in rendered.split(",") if part.strip()
                ]
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return list(parsed.items())
            return [parsed]
        if isinstance(raw, (list, tuple)):
            items = list(raw)
            return [render_value(item, context) for item in items]
        if isinstance(raw, dict):
            return list(raw.items())
        return [raw]

    async def run(self, node, context, config) -> Any:
        items = self.resolve_items(config, context)

        cap = settings.WORKFLOW_MAX_LOOP_ITERATIONS
        requested = config.get("max_iterations")
        if requested:
            cap = min(cap, max(1, int(coerce_number(requested, cap))))
        truncated = len(items) > cap
        if truncated:
            items = items[:cap]

        mode = str(config.get("mode") or "collect").lower()
        template = config.get("template")
        results: List[Any] = []

        if mode == "collect" and template:
            for index, item in enumerate(items):
                if isinstance(context, NodeContext):
                    context.push_loop(item, index, len(items))
                    try:
                        results.append(render_value(template, context))
                    finally:
                        context.pop_loop()
                else:
                    results.append(render_value(template, context))
                if index % 50 == 49:
                    # Yield so a long loop cannot starve the event loop.
                    await asyncio.sleep(0)
        else:
            results = list(items)

        return {
            "items": items,
            "results": results,
            "count": len(items),
            "mode": mode,
            "truncated": truncated,
        }


class DelayNode(RuntimeNodeExecutor):
    """Waits for a fixed duration, remaining responsive to cancellation."""

    label = "Delay"
    category = "control"
    description = "Pauses the branch for a number of seconds."
    aliases = ("delay_node", "wait", "sleep")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "seconds", "number", default=1.0, minimum=0.0, maximum=3600.0
            ),
        ],
        outputs=[FieldSpec("slept_seconds", "number")],
    )

    async def run(self, node, context, config) -> Any:
        seconds = coerce_number(config.get("seconds", 1.0), 1.0)
        seconds = max(0.0, min(seconds, 3600.0))

        stop_event = None
        if isinstance(context, NodeContext):
            stop_event = context.cancel_event
        if stop_event is not None:
            # Wake early if the run is stopped, instead of sleeping it out.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=seconds)
                return {"slept_seconds": seconds, "interrupted": True}
            except asyncio.TimeoutError:
                return {"slept_seconds": seconds, "interrupted": False}
        await asyncio.sleep(seconds)
        return {"slept_seconds": seconds, "interrupted": False}
