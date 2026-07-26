"""M4 AI orchestration primitives: fallback, circuit breaking, cost, tracing.

The M2 orchestrator resolved exactly one provider and raised on failure. M4 adds
the pieces the brief calls for, kept in a separate module so
:mod:`app.services.ai.orchestrator` stays backwards compatible and its existing
tests keep passing. ``AIOrchestrator`` mixes these in.

Contents
--------
* :class:`CircuitBreaker`  — trips a provider after repeated failures
  (``AI_CIRCUIT_BREAKER_THRESHOLD`` / ``AI_CIRCUIT_BREAKER_RESET_SECONDS``,
  which existed in settings since M1 but were never used).
* :class:`CostModel`       — token → USD conversion with per-model pricing.
* :class:`TraceRecorder`   — bounded ring buffer of recent AI calls.
* :class:`ImageProvider` / :class:`SpeechProvider` — protocols the image, TTS
  and STT nodes look up. Nothing is registered by default; nodes fail loudly
  rather than fabricating output.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Protocol, runtime_checkable

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger("ai.orchestration")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
class CircuitState:
    CLOSED = "closed"      # healthy
    OPEN = "open"          # failing; calls are short-circuited
    HALF_OPEN = "half_open"  # probing recovery


@dataclass
class _BreakerEntry:
    failures: int = 0
    opened_at: Optional[float] = None
    state: str = CircuitState.CLOSED
    last_error: Optional[str] = None
    successes: int = 0


class CircuitBreaker:
    """Per-provider failure tracking.

    After ``threshold`` consecutive failures a provider is marked OPEN and
    skipped by the fallback chain until ``reset_seconds`` have elapsed, at which
    point one probe request is allowed (HALF_OPEN).
    """

    def __init__(
        self, threshold: Optional[int] = None, reset_seconds: Optional[float] = None
    ) -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._entries: Dict[str, _BreakerEntry] = {}
        self._lock = threading.RLock()

    @property
    def threshold(self) -> int:
        return max(1, self._threshold or settings.AI_CIRCUIT_BREAKER_THRESHOLD)

    @property
    def reset_seconds(self) -> float:
        return max(
            0.1, self._reset_seconds or settings.AI_CIRCUIT_BREAKER_RESET_SECONDS
        )

    def _entry(self, provider: str) -> _BreakerEntry:
        entry = self._entries.get(provider)
        if entry is None:
            entry = _BreakerEntry()
            self._entries[provider] = entry
        return entry

    def is_available(self, provider: str) -> bool:
        """False while the breaker is open and the cooldown has not elapsed."""
        with self._lock:
            entry = self._entry(provider)
            if entry.state != CircuitState.OPEN:
                return True
            if entry.opened_at is None:
                return True
            if (time.monotonic() - entry.opened_at) >= self.reset_seconds:
                entry.state = CircuitState.HALF_OPEN
                logger.info("Circuit for provider %r is half-open (probing).", provider)
                return True
            return False

    def record_success(self, provider: str) -> None:
        with self._lock:
            entry = self._entry(provider)
            entry.failures = 0
            entry.opened_at = None
            entry.last_error = None
            entry.successes += 1
            if entry.state != CircuitState.CLOSED:
                logger.info("Circuit for provider %r closed after success.", provider)
            entry.state = CircuitState.CLOSED

    def record_failure(self, provider: str, error: str = "") -> bool:
        """Record a failure. Returns True when this call tripped the breaker."""
        with self._lock:
            entry = self._entry(provider)
            entry.failures += 1
            entry.last_error = error[:500]
            if entry.state == CircuitState.HALF_OPEN or entry.failures >= self.threshold:
                already_open = entry.state == CircuitState.OPEN
                entry.state = CircuitState.OPEN
                entry.opened_at = time.monotonic()
                if not already_open:
                    logger.warning(
                        "Circuit for provider %r opened after %s failure(s): %s",
                        provider, entry.failures, error[:200],
                    )
                    return True
            return False

    def state(self, provider: str) -> str:
        with self._lock:
            return self._entry(provider).state

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    "state": entry.state,
                    "failures": entry.failures,
                    "successes": entry.successes,
                    "last_error": entry.last_error,
                    "cooldown_remaining": (
                        max(
                            0.0,
                            self.reset_seconds - (time.monotonic() - entry.opened_at),
                        )
                        if entry.opened_at
                        else 0.0
                    ),
                }
                for name, entry in self._entries.items()
            }

    def reset(self, provider: Optional[str] = None) -> None:
        with self._lock:
            if provider is None:
                self._entries.clear()
            else:
                self._entries.pop(provider, None)


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #
@dataclass
class ModelPricing:
    """USD per 1000 tokens."""

    prompt_per_1k: float = 0.0
    completion_per_1k: float = 0.0

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            (prompt_tokens / 1000.0) * self.prompt_per_1k
            + (completion_tokens / 1000.0) * self.completion_per_1k,
            8,
        )


class CostModel:
    """Token → USD conversion.

    Ships with published list prices for a few common models purely as
    *defaults*; they are overridable per model via the model registry's
    ``config.pricing`` and should be treated as estimates, not billing truth.
    """

    DEFAULT_PRICING: Dict[str, ModelPricing] = {
        "gpt-4o": ModelPricing(0.005, 0.015),
        "gpt-4o-mini": ModelPricing(0.00015, 0.0006),
        "gpt-4-turbo": ModelPricing(0.01, 0.03),
        "gpt-3.5-turbo": ModelPricing(0.0005, 0.0015),
    }

    def __init__(self) -> None:
        self._overrides: Dict[str, ModelPricing] = {}
        self._lock = threading.RLock()

    def register(
        self, model_name: str, prompt_per_1k: float, completion_per_1k: float
    ) -> None:
        with self._lock:
            self._overrides[model_name] = ModelPricing(
                float(prompt_per_1k), float(completion_per_1k)
            )

    def pricing_for(self, model_name: Optional[str]) -> ModelPricing:
        if not model_name:
            return ModelPricing(
                settings.AI_DEFAULT_PROMPT_COST_PER_1K,
                settings.AI_DEFAULT_COMPLETION_COST_PER_1K,
            )
        with self._lock:
            if model_name in self._overrides:
                return self._overrides[model_name]
        if model_name in self.DEFAULT_PRICING:
            return self.DEFAULT_PRICING[model_name]
        # Prefix match so "gpt-4o-2024-08-06" inherits "gpt-4o" pricing.
        for known, pricing in self.DEFAULT_PRICING.items():
            if model_name.startswith(known):
                return pricing
        return ModelPricing(
            settings.AI_DEFAULT_PROMPT_COST_PER_1K,
            settings.AI_DEFAULT_COMPLETION_COST_PER_1K,
        )

    def estimate(
        self,
        model_name: Optional[str],
        prompt_tokens: int,
        completion_tokens: int = 0,
    ) -> Dict[str, Any]:
        pricing = self.pricing_for(model_name)
        cost = pricing.cost(prompt_tokens, completion_tokens)
        return {
            "model": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_cost_per_1k": pricing.prompt_per_1k,
            "completion_cost_per_1k": pricing.completion_per_1k,
            "cost_usd": cost,
            "is_estimate": True,
        }

    def known_models(self) -> List[Dict[str, Any]]:
        with self._lock:
            merged = {**self.DEFAULT_PRICING, **self._overrides}
        return [
            {
                "model": name,
                "prompt_per_1k": pricing.prompt_per_1k,
                "completion_per_1k": pricing.completion_per_1k,
            }
            for name, pricing in sorted(merged.items())
        ]


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #
@dataclass
class AITrace:
    """One AI invocation, including every fallback attempt."""

    trace_id: str
    started_at: str
    label: str = "chat"
    model: Optional[str] = None
    provider: Optional[str] = None
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    success: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None
    execution_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "label": self.label,
            "model": self.model,
            "provider": self.provider,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 3),
            "success": self.success,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
            "execution_id": self.execution_id,
        }


class TraceRecorder:
    """Bounded in-memory ring buffer of recent AI calls."""

    def __init__(self, size: Optional[int] = None) -> None:
        self._size = size
        self._traces: Deque[AITrace] = deque(
            maxlen=max(1, size or settings.AI_TRACE_BUFFER_SIZE)
        )
        self._lock = threading.RLock()

    def start(self, label: str = "chat", execution_id: Optional[int] = None) -> AITrace:
        return AITrace(
            trace_id=uuid.uuid4().hex[:16],
            started_at=utc_iso(),
            label=label,
            execution_id=execution_id,
        )

    def finish(self, trace: AITrace) -> AITrace:
        with self._lock:
            self._traces.append(trace)
        return trace

    def recent(self, limit: int = 50, only_failures: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._traces)
        if only_failures:
            items = [t for t in items if not t.success]
        return [t.to_dict() for t in items[-limit:][::-1]]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            items = list(self._traces)
        if not items:
            return {
                "count": 0, "success_rate": 0.0, "total_tokens": 0,
                "total_cost_usd": 0.0, "avg_duration_ms": 0.0,
            }
        successes = sum(1 for t in items if t.success)
        return {
            "count": len(items),
            "success_rate": round(successes / len(items), 4),
            "total_tokens": sum(t.total_tokens for t in items),
            "total_cost_usd": round(sum(t.cost_usd for t in items), 6),
            "avg_duration_ms": round(
                sum(t.duration_ms for t in items) / len(items), 3
            ),
        }

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


# --------------------------------------------------------------------------- #
# Optional provider protocols (nothing is registered by default)
# --------------------------------------------------------------------------- #
@runtime_checkable
class ImageProvider(Protocol):
    """Contract for the ``imageGeneration`` node."""

    name: str

    async def generate_image(
        self,
        *,
        prompt: str,
        model: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
        count: int = 1,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        ...


@runtime_checkable
class TTSProvider(Protocol):
    """Contract for the ``tts`` node."""

    name: str

    async def synthesize(
        self,
        *,
        text: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        language: str = "en",
        speed: float = 1.0,
        output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...


@runtime_checkable
class STTProvider(Protocol):
    """Contract for the ``stt`` node."""

    name: str

    async def transcribe(
        self,
        *,
        audio_path: str,
        model: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...
