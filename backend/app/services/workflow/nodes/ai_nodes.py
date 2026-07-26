"""AI nodes: prompt templating, chat, completion and image generation.

Before M4 there was no way to call the AI runtime from a workflow at all
(gap I3): the orchestrator was reachable only over its REST API. These nodes
bridge the engine to :mod:`app.services.ai.orchestrator`, including provider
fallback, conversation memory, token accounting and cost estimation.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.errors import ValidationError
from app.services.workflow.executors import render_template
from app.services.workflow.runtime import (
    FieldSpec,
    NodeContext,
    NodeErrorCode,
    NodeExecutionError,
    NodeMetrics,
    NodeResult,
    NodeSchema,
    RuntimeNodeExecutor,
)


class PromptNode(RuntimeNodeExecutor):
    """Renders a prompt template without calling a model.

    Useful for composing a prompt from upstream outputs and feeding it into an
    AI node, and for testing templates cheaply.
    """

    label = "Prompt"
    category = "ai"
    description = "Renders a prompt template from context variables."
    aliases = ("prompt_node", "prompt_template")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "template",
                "string",
                required=True,
                description="Prompt text with '{{ ref }}' placeholders",
            ),
            FieldSpec("system", "string", description="Optional system prompt"),
            FieldSpec(
                "variables",
                "object",
                description="Extra values available to the template",
            ),
        ],
        outputs=[
            FieldSpec("prompt", "string", description="Rendered prompt"),
            FieldSpec("system", "string"),
            FieldSpec("estimated_tokens", "integer"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        from app.services.ai.orchestrator import estimate_tokens

        extra = config.get("variables") or {}
        scope: Dict[Any, Any] = dict(context)
        if isinstance(extra, dict):
            scope.update(extra)

        template = str(config.get("template") or "")
        if not template.strip():
            raise ValidationError("Prompt node requires a non-empty 'template'.")

        prompt = render_template(template, scope)
        system = render_template(str(config.get("system") or ""), scope) or None

        return {
            "prompt": prompt,
            "system": system,
            "estimated_tokens": estimate_tokens(prompt) + estimate_tokens(system or ""),
        }


class _BaseAINode(RuntimeNodeExecutor):
    """Shared plumbing for chat/completion nodes."""

    category = "ai"

    def _build_messages(
        self, config: Dict[str, Any], context: Any, memory_key: Any
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []
        system = render_template(str(config.get("system") or ""), context).strip()
        if system:
            messages.append({"role": "system", "content": system})

        if config.get("use_memory") and isinstance(context, NodeContext):
            messages.extend(context.recall(memory_key))

        prompt = render_template(str(config.get("prompt") or ""), context).strip()
        if not prompt:
            raise ValidationError(
                f"{self.label} node requires a non-empty 'prompt'."
            )
        messages.append({"role": "user", "content": prompt})
        return messages

    @staticmethod
    def _provider_options(config: Dict[str, Any]) -> Dict[str, Any]:
        options: Dict[str, Any] = {}
        if config.get("temperature") is not None:
            options["temperature"] = config["temperature"]
        if config.get("max_tokens"):
            options["max_tokens"] = int(config["max_tokens"])
        extra = config.get("options")
        if isinstance(extra, dict):
            options.update(extra)
        return options

    async def _generate(
        self, config: Dict[str, Any], context: Any, memory_key: Any
    ) -> NodeResult:
        from app.services.ai.orchestrator import ai_orchestrator

        messages = self._build_messages(config, context, memory_key)
        model = str(config.get("model") or "").strip() or None
        provider = str(config.get("provider") or "").strip() or None
        options = self._provider_options(config)

        try:
            outcome = await ai_orchestrator.generate(
                messages,
                model_name=model,
                provider=provider,
                allow_fallback=bool(config.get("allow_fallback", True)),
                trace_label=self.label,
                **options,
            )
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError(
                f"AI generation failed: {exc}",
                code=NodeErrorCode.PROVIDER,
                details={"model": model, "provider": provider},
            ) from exc

        content = outcome.get("content", "")
        usage = outcome.get("usage", {}) or {}

        if config.get("use_memory") and isinstance(context, NodeContext):
            context.remember(memory_key, "user", messages[-1]["content"])
            context.remember(memory_key, "assistant", content)

        metrics = NodeMetrics(
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
            cost_usd=float(outcome.get("cost_usd", 0.0) or 0.0),
            provider=outcome.get("provider"),
            model=outcome.get("model"),
        )
        return NodeResult(
            output={
                "response": content,
                "content": content,
                "result": content,
                "model": outcome.get("model"),
                "provider": outcome.get("provider"),
                "usage": usage,
                "cost_usd": metrics.cost_usd,
                "fallback_used": outcome.get("fallback_used", False),
                "attempts": outcome.get("attempts", []),
            },
            metrics=metrics,
        )


class AIChatNode(_BaseAINode):
    """Multi-turn chat with optional in-run conversation memory."""

    label = "AI Chat"
    description = "Sends a chat request, optionally retaining conversation memory."
    aliases = ("ai_chat", "aichat", "chat")
    schema = NodeSchema(
        inputs=[
            FieldSpec("prompt", "string", required=True, description="User message"),
            FieldSpec("system", "string", description="System prompt"),
            FieldSpec("model", "string", description="Model name (blank = default)"),
            FieldSpec(
                "provider",
                "string",
                description="Pin a provider; blank uses the fallback chain",
            ),
            FieldSpec("temperature", "number", minimum=0.0, maximum=2.0),
            FieldSpec("max_tokens", "integer", minimum=1, maximum=200000),
            FieldSpec(
                "use_memory",
                "boolean",
                default=True,
                description="Retain history across loop iterations",
            ),
            FieldSpec("allow_fallback", "boolean", default=True),
            FieldSpec("options", "object", description="Extra provider options"),
        ],
        outputs=[
            FieldSpec("response", "string"),
            FieldSpec("model", "string"),
            FieldSpec("provider", "string"),
            FieldSpec("usage", "object"),
            FieldSpec("cost_usd", "number"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        memory_key = f"chat:{getattr(node, 'id', 'node')}"
        return await self._generate(config, context, memory_key)


class AICompletionNode(_BaseAINode):
    """Single-shot completion. Never uses conversation memory."""

    label = "AI Completion"
    description = "Single-shot text completion with no conversation memory."
    aliases = ("ai_completion", "aicompletion", "completion")
    schema = NodeSchema(
        inputs=[
            FieldSpec("prompt", "string", required=True),
            FieldSpec("system", "string"),
            FieldSpec("model", "string"),
            FieldSpec("provider", "string"),
            FieldSpec("temperature", "number", minimum=0.0, maximum=2.0),
            FieldSpec("max_tokens", "integer", minimum=1, maximum=200000),
            FieldSpec("allow_fallback", "boolean", default=True),
            FieldSpec("options", "object"),
        ],
        outputs=[
            FieldSpec("response", "string"),
            FieldSpec("model", "string"),
            FieldSpec("usage", "object"),
            FieldSpec("cost_usd", "number"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        config = dict(config)
        config["use_memory"] = False
        return await self._generate(config, context, f"completion:{getattr(node, 'id', 0)}")


class ImageGenerationNode(RuntimeNodeExecutor):
    """Generates an image via a configured image provider.

    No image provider ships with Creator OS, so unless one is registered this
    node fails with a clear ``PROVIDER`` error rather than silently returning a
    fake asset. Registering a provider is described in EXECUTION_ENGINE.md.
    """

    label = "Image Generation"
    category = "ai"
    description = "Generates an image from a prompt using a registered image provider."
    aliases = ("image_generation", "imagegen", "image")
    schema = NodeSchema(
        inputs=[
            FieldSpec("prompt", "string", required=True),
            FieldSpec("model", "string", description="Image model name"),
            FieldSpec("provider", "string"),
            FieldSpec("width", "integer", default=1024, minimum=64, maximum=4096),
            FieldSpec("height", "integer", default=1024, minimum=64, maximum=4096),
            FieldSpec("count", "integer", default=1, minimum=1, maximum=4),
            FieldSpec("options", "object"),
        ],
        outputs=[
            FieldSpec("images", "array", description="Generated asset references"),
            FieldSpec("prompt", "string", description="Rendered prompt"),
            FieldSpec("provider", "string"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        from app.services.ai.orchestrator import ai_orchestrator

        prompt = render_template(str(config.get("prompt") or ""), context).strip()
        if not prompt:
            raise ValidationError("Image node requires a non-empty 'prompt'.")

        provider_name = str(config.get("provider") or "").strip() or None
        provider = ai_orchestrator.get_image_provider(provider_name)
        if provider is None:
            raise NodeExecutionError(
                "No image generation provider is configured. Register one via "
                "ai_orchestrator.register_image_provider() to enable this node.",
                code=NodeErrorCode.PROVIDER,
                details={"requested_provider": provider_name},
            )

        options = dict(config.get("options") or {})
        result = await provider.generate_image(
            prompt=prompt,
            model=config.get("model"),
            width=int(config.get("width") or 1024),
            height=int(config.get("height") or 1024),
            count=int(config.get("count") or 1),
            **options,
        )
        images = result.get("images", []) if isinstance(result, dict) else result
        return {
            "images": images,
            "prompt": prompt,
            "provider": provider_name or getattr(provider, "name", "custom"),
            "count": len(images) if isinstance(images, list) else 0,
        }
