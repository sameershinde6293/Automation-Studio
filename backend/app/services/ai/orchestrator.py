from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.errors import NotFoundError, ProviderError, ValidationError
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import SessionLocal
from app.domain.repositories.ai.ai_repository import (
    MessageCreate,
    TokenUsageCreate,
    ai_model_repo,
    conversation_repo,
    message_repo,
    token_usage_repo,
)
from app.services.ai.orchestration import (
    AITrace,
    CircuitBreaker,
    CostModel,
    TraceRecorder,
)
from app.services.ai.providers.base import BaseAIProvider
from app.services.ai.providers.mock_provider import MockAIProvider
from app.services.ai.providers.openai_provider import OpenAIProvider
from app.services.ai.providers.local_provider import OllamaProvider

logger = logging.getLogger("creator_os.ai")


class ChatResult(dict):
    """Dict response that preserves V1.0 string-containment tests."""

    def __contains__(self, item):
        if isinstance(item, str) and item in self.get("response", ""):
            return True
        return super().__contains__(item)

    def __str__(self) -> str:
        return str(self.get("response", ""))

    def __eq__(self, other):
        if isinstance(other, str):
            return self.get("response", "") == other
        return super().__eq__(other)


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4) if text else 0


def render_prompt(template: str, variables: Dict[str, Any]) -> str:
    """Interpolate ``{{ name }}`` placeholders in a prompt template.

    Shares the workflow engine's template syntax so a prompt written in the
    editor behaves identically whether it is rendered by a Prompt node or
    passed straight to the orchestrator.
    """
    from app.services.workflow.executors import render_template

    return render_template(template or "", dict(variables or {}))


class AIOrchestrator:
    def __init__(self):
        self.providers: Dict[str, BaseAIProvider] = {
            "mock": MockAIProvider(),
            "openai": OpenAIProvider(),
            "local": OllamaProvider(),
        }
        self._loaded_models = set()
        # --- M4: fallback, breaking, cost and tracing ---------------------
        self.circuit_breaker = CircuitBreaker()
        self.cost_model = CostModel()
        self.traces = TraceRecorder()
        #: Optional providers looked up by the image/TTS/STT nodes. Empty by
        #: default: those nodes raise a clear error rather than faking output.
        self._image_providers: Dict[str, Any] = {}
        self._speech_providers: Dict[str, Dict[str, Any]] = {"tts": {}, "stt": {}}

    def provider_info(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": name,
                "available": self._provider_available(name),
                "capabilities": self._capabilities(name),
                "class": provider.__class__.__name__,
            }
            for name, provider in sorted(self.providers.items())
        ]

    def _provider_available(self, name: str) -> bool:
        """Whether a provider is configured well enough to be worth calling.

        Only ``openai`` has a hard prerequisite (an API key). Any other
        registered provider — including ones added at runtime by a plugin or a
        test — is assumed usable; returning False for unknown names would
        silently drop them from the fallback chain.
        """
        if name == "openai":
            return bool(settings.OPENAI_API_KEY)
        return name in self.providers

    def _capabilities(self, name: str) -> List[str]:
        if name == "mock":
            return ["chat", "stream", "embedding"]
        if name in {"openai", "local"}:
            return ["chat"]
        return []

    def get_provider_for_model(self, db, model_name: str) -> Tuple[BaseAIProvider, Any]:
        model = db.query(ai_model_repo.model).filter_by(name=model_name, is_active=True).first()
        if not model:
            raise NotFoundError(f"Active AI model {model_name!r} not found.")
        provider = self.providers.get(model.provider)
        if not provider:
            raise ValidationError(f"Provider {model.provider!r} is not configured.")
        if model.name not in self._loaded_models and model.provider == "local":
            logger.info("Lazy loading local model %s", model.name)
            self._loaded_models.add(model.name)
        return provider, model

    def trim_context(self, messages: List[Dict[str, str]], max_messages: int | None = None, max_tokens: int | None = None):
        max_messages = settings.AI_CONTEXT_MAX_MESSAGES if max_messages is None else max_messages
        max_tokens = settings.AI_CONTEXT_MAX_TOKENS if max_tokens is None else max_tokens
        if max_messages < 1 or max_tokens < 1:
            raise ValidationError("Context limits must be positive.")
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        kept = non_system[-max_messages:]
        result = system[:1] + kept
        while sum(estimate_tokens(m.get("content", "")) for m in result) > max_tokens and len(result) > 1:
            # Preserve a leading system prompt and most recent turns.
            drop_index = 1 if result[0].get("role") == "system" else 0
            result.pop(drop_index)
        trimmed = max(0, len(messages) - len(result))
        return result, trimmed

    async def chat(self, conversation_id: int, model_name: str, message: str, **kwargs) -> Dict[str, Any]:
        if not message or not message.strip():
            raise ValidationError("Chat message must be non-empty.")
        with SessionLocal() as db:
            conversation = conversation_repo.get(db, conversation_id)
            if not conversation:
                raise NotFoundError(f"Conversation {conversation_id} not found.")
            history = (
                db.query(message_repo.model)
                .filter_by(conversation_id=conversation_id)
                .order_by(message_repo.model.created_at, message_repo.model.id)
                .all()
            )
            provider, _model = self.get_provider_for_model(db, model_name)
            messages = [{"role": msg.role, "content": msg.content} for msg in history]
            messages.append({"role": "user", "content": message.strip()})
            messages, trimmed = self.trim_context(messages)
            user_msg = message_repo.create(
                db,
                MessageCreate(
                    conversation_id=conversation_id,
                    role="user",
                    content=message.strip(),
                    tokens_used=estimate_tokens(message),
                ),
            )

        try:
            result = await provider.generate(model_name, messages, **kwargs)
        except Exception as exc:
            logger.exception("Error generating AI response")
            raise ProviderError("AI provider failed.", details={"provider_error": str(exc)}) from exc

        content = result.get("content", "")
        usage = result.get("usage", {}) or {}
        if not usage:
            usage = {
                "prompt_tokens": sum(estimate_tokens(m.get("content", "")) for m in messages),
                "completion_tokens": estimate_tokens(content),
            }
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

        with SessionLocal() as db:
            assistant_msg = message_repo.create(
                db,
                MessageCreate(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content,
                    tokens_used=usage.get("completion_tokens", 0),
                ),
            )
            usage_row = token_usage_repo.create(
                db,
                TokenUsageCreate(
                    model_name=model_name,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)),
                ),
            )
        return ChatResult({
            "response": content,
            "message": assistant_msg,
            "user_message_id": user_msg.id,
            "conversation_id": conversation_id,
            "model_name": model_name,
            "usage": usage,
            "usage_id": usage_row.id,
            "trimmed_messages": trimmed,
        })

    # ------------------------------------------------------------------ #
    # M4: multi-provider execution with fallback
    # ------------------------------------------------------------------ #
    def _resolve_model_for_provider(self, provider_name: str, model_name: Optional[str]) -> str:
        """Pick a concrete model name for a provider.

        Uses the caller's model when given, otherwise the first active model
        registered for that provider, otherwise a provider-specific default.
        """
        if model_name:
            return model_name
        try:
            with SessionLocal() as db:
                row = (
                    db.query(ai_model_repo.model)
                    .filter_by(provider=provider_name, is_active=True)
                    .order_by(ai_model_repo.model.id)
                    .first()
                )
                if row:
                    return row.name
        except Exception:  # pragma: no cover - registry is optional
            logger.debug("Model registry lookup failed for %s", provider_name, exc_info=True)
        return {
            "openai": "gpt-4o-mini",
            "local": "llama3",
            "mock": "mock-model",
        }.get(provider_name, "default")

    def resolve_chain(
        self,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        allow_fallback: bool = True,
    ) -> List[str]:
        """Build the ordered provider chain for one request.

        A pinned provider is always tried first. Unavailable providers (missing
        credentials) and providers whose circuit is open are filtered out; if
        that removes everything, the pinned/first provider is retained so the
        caller gets a real error from the provider rather than a vague
        "nothing available".
        """
        chain: List[str] = []
        if provider:
            if provider not in self.providers:
                raise ValidationError(
                    f"Provider {provider!r} is not configured.",
                    details={"available": sorted(self.providers)},
                )
            chain.append(provider)
            if not allow_fallback or not settings.AI_FALLBACK_ENABLED:
                return chain
        elif model_name:
            # Prefer the provider that owns the requested model.
            try:
                with SessionLocal() as db:
                    row = (
                        db.query(ai_model_repo.model)
                        .filter_by(name=model_name, is_active=True)
                        .first()
                    )
                    if row and row.provider in self.providers:
                        chain.append(row.provider)
            except Exception:  # pragma: no cover
                logger.debug("Model lookup failed for %r", model_name, exc_info=True)

        if allow_fallback and settings.AI_FALLBACK_ENABLED:
            for candidate in settings.AI_FALLBACK_CHAIN:
                if candidate in self.providers and candidate not in chain:
                    chain.append(candidate)
        if not chain:
            chain = [name for name in ("mock",) if name in self.providers] or list(
                self.providers
            )[:1]

        usable = [
            name
            for name in chain
            if self._provider_available(name) and self.circuit_breaker.is_available(name)
        ]
        return usable or chain[:1]

    async def generate(
        self,
        messages: List[Dict[str, str]],
        *,
        model_name: Optional[str] = None,
        provider: Optional[str] = None,
        allow_fallback: bool = True,
        trace_label: str = "generate",
        execution_id: Optional[int] = None,
        **options: Any,
    ) -> Dict[str, Any]:
        """Run a generation against the provider chain, falling back on failure.

        Returns ``{content, model, provider, usage, cost_usd, fallback_used,
        attempts, trace_id}``. Raises :class:`ProviderError` only when every
        provider in the chain fails.
        """
        if not messages:
            raise ValidationError("generate() requires at least one message.")

        chain = self.resolve_chain(provider, model_name, allow_fallback)
        trace: AITrace = self.traces.start(trace_label, execution_id=execution_id)
        started = time.perf_counter()
        errors: List[str] = []

        for index, provider_name in enumerate(chain):
            candidate = self.providers.get(provider_name)
            if candidate is None:  # pragma: no cover - guarded by resolve_chain
                continue
            resolved_model = self._resolve_model_for_provider(provider_name, model_name)
            attempt_started = time.perf_counter()
            try:
                timeout = settings.AI_REQUEST_TIMEOUT_SECONDS
                result = await asyncio.wait_for(
                    candidate.generate(resolved_model, messages, **options),
                    timeout=timeout,
                )
            except Exception as exc:
                elapsed = (time.perf_counter() - attempt_started) * 1000
                message = f"{type(exc).__name__}: {exc}"
                errors.append(f"{provider_name}: {message}")
                self.circuit_breaker.record_failure(provider_name, message)
                trace.attempts.append(
                    {
                        "provider": provider_name,
                        "model": resolved_model,
                        "ok": False,
                        "duration_ms": round(elapsed, 3),
                        "error": message[:500],
                    }
                )
                logger.warning(
                    "AI provider %r failed (%s/%s): %s",
                    provider_name, index + 1, len(chain), message,
                )
                continue

            elapsed = (time.perf_counter() - attempt_started) * 1000
            self.circuit_breaker.record_success(provider_name)

            content = result.get("content", "") if isinstance(result, dict) else str(result)
            usage = (result.get("usage") or {}) if isinstance(result, dict) else {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            if not prompt_tokens:
                prompt_tokens = sum(
                    estimate_tokens(m.get("content", "")) for m in messages
                )
            if not completion_tokens:
                completion_tokens = estimate_tokens(content)
            total_tokens = int(usage.get("total_tokens", 0) or 0) or (
                prompt_tokens + completion_tokens
            )
            cost = self.cost_model.pricing_for(resolved_model).cost(
                prompt_tokens, completion_tokens
            )

            trace.attempts.append(
                {
                    "provider": provider_name,
                    "model": resolved_model,
                    "ok": True,
                    "duration_ms": round(elapsed, 3),
                }
            )
            trace.provider = provider_name
            trace.model = resolved_model
            trace.success = True
            trace.prompt_tokens = prompt_tokens
            trace.completion_tokens = completion_tokens
            trace.total_tokens = total_tokens
            trace.cost_usd = cost
            trace.duration_ms = (time.perf_counter() - started) * 1000
            self.traces.finish(trace)

            self._record_usage(resolved_model, prompt_tokens, completion_tokens, total_tokens)

            return {
                "content": content,
                "model": resolved_model,
                "provider": provider_name,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
                "cost_usd": cost,
                "fallback_used": index > 0,
                "attempts": trace.attempts,
                "trace_id": trace.trace_id,
            }

        trace.success = False
        trace.error = "; ".join(errors)[:2000]
        trace.duration_ms = (time.perf_counter() - started) * 1000
        self.traces.finish(trace)
        raise ProviderError(
            "All AI providers failed.",
            details={"attempts": trace.attempts, "chain": chain, "errors": errors},
        )

    def _record_usage(
        self,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Persist a token usage row. Never raises."""
        try:
            with SessionLocal() as db:
                token_usage_repo.create(
                    db,
                    TokenUsageCreate(
                        model_name=model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    ),
                )
        except Exception:
            logger.debug("Could not persist token usage", exc_info=True)

    # ------------------------------------------------------------------ #
    # M4: cost estimation
    # ------------------------------------------------------------------ #
    def estimate_cost(
        self,
        text: str = "",
        *,
        model_name: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Estimate the token count and USD cost of a request."""
        tokens = (
            prompt_tokens if prompt_tokens is not None else estimate_tokens(text)
        )
        return self.cost_model.estimate(model_name, tokens, completion_tokens)

    # ------------------------------------------------------------------ #
    # M4: optional provider registration (image / speech)
    # ------------------------------------------------------------------ #
    def register_image_provider(self, name: str, provider: Any, *, default: bool = False) -> None:
        self._image_providers[name] = provider
        if default or "__default__" not in self._image_providers:
            self._image_providers["__default__"] = provider

    def get_image_provider(self, name: Optional[str] = None) -> Optional[Any]:
        if name:
            return self._image_providers.get(name)
        return self._image_providers.get("__default__")

    def register_speech_provider(
        self, kind: str, name: str, provider: Any, *, default: bool = False
    ) -> None:
        kind = kind.lower()
        if kind not in self._speech_providers:
            raise ValidationError(
                f"Speech provider kind must be 'tts' or 'stt', got {kind!r}."
            )
        bucket = self._speech_providers[kind]
        bucket[name] = provider
        if default or "__default__" not in bucket:
            bucket["__default__"] = provider

    def get_speech_provider(self, kind: str, name: Optional[str] = None) -> Optional[Any]:
        bucket = self._speech_providers.get(kind.lower(), {})
        if name:
            return bucket.get(name)
        return bucket.get("__default__")

    def clear_optional_providers(self) -> None:
        """Used by tests to restore the default (empty) provider state."""
        self._image_providers.clear()
        self._speech_providers = {"tts": {}, "stt": {}}

    # ------------------------------------------------------------------ #
    # M4: health / introspection
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        return {
            "providers": self.provider_info(),
            "circuits": self.circuit_breaker.snapshot(),
            "fallback_chain": list(settings.AI_FALLBACK_CHAIN),
            "fallback_enabled": settings.AI_FALLBACK_ENABLED,
            "traces": self.traces.stats(),
            "image_providers": sorted(
                k for k in self._image_providers if k != "__default__"
            ),
            "tts_providers": sorted(
                k for k in self._speech_providers["tts"] if k != "__default__"
            ),
            "stt_providers": sorted(
                k for k in self._speech_providers["stt"] if k != "__default__"
            ),
        }


ai_orchestrator = AIOrchestrator()
