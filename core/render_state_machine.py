"""Finite state machine controlling Autopilot render lifecycle.

Prevents impossible transitions and enforces allowed UI actions per state.
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("autopilot.state_machine")


class RenderState(Enum):
    """All valid render pipeline states."""

    IDLE = auto()
    LOADING = auto()
    VALIDATING = auto()
    GENERATING = auto()
    PROCESSING = auto()
    RENDERING = auto()
    EXPORTING = auto()
    PAUSED = auto()
    CANCELLED = auto()
    RECOVERING = auto()
    COMPLETE = auto()
    FAILED = auto()


VALID_TRANSITIONS: Dict[RenderState, List[RenderState]] = {
    RenderState.IDLE: [RenderState.LOADING, RenderState.RECOVERING],
    RenderState.LOADING: [
        RenderState.VALIDATING,
        RenderState.CANCELLED,
        RenderState.FAILED,
    ],
    RenderState.VALIDATING: [
        RenderState.GENERATING,
        RenderState.IDLE,
        RenderState.FAILED,
    ],
    RenderState.GENERATING: [
        RenderState.PROCESSING,
        RenderState.PAUSED,
        RenderState.CANCELLED,
        RenderState.FAILED,
    ],
    RenderState.PROCESSING: [
        RenderState.RENDERING,
        RenderState.PAUSED,
        RenderState.CANCELLED,
        RenderState.FAILED,
    ],
    RenderState.RENDERING: [
        RenderState.EXPORTING,
        RenderState.PAUSED,
        RenderState.CANCELLED,
        RenderState.FAILED,
    ],
    RenderState.EXPORTING: [
        RenderState.COMPLETE,
        RenderState.CANCELLED,
        RenderState.FAILED,
    ],
    RenderState.PAUSED: [
        RenderState.RENDERING,
        RenderState.GENERATING,
        RenderState.PROCESSING,
        RenderState.CANCELLED,
    ],
    RenderState.CANCELLED: [RenderState.IDLE],
    RenderState.RECOVERING: [
        RenderState.RENDERING,
        RenderState.GENERATING,
        RenderState.IDLE,
    ],
    RenderState.COMPLETE: [RenderState.IDLE, RenderState.LOADING],
    RenderState.FAILED: [RenderState.IDLE, RenderState.LOADING],
}

ALLOWED_ACTIONS: Dict[RenderState, List[str]] = {
    RenderState.IDLE: ["render", "import", "settings", "open_project", "all_ui"],
    RenderState.LOADING: ["cancel"],
    RenderState.VALIDATING: ["cancel"],
    RenderState.GENERATING: ["pause", "cancel", "view_log"],
    RenderState.PROCESSING: ["pause", "cancel", "view_log"],
    RenderState.RENDERING: ["pause", "cancel", "view_log"],
    RenderState.EXPORTING: ["cancel"],
    RenderState.PAUSED: ["resume", "cancel"],
    RenderState.CANCELLED: [],
    RenderState.RECOVERING: ["resume_from_checkpoint", "start_over", "ignore"],
    RenderState.COMPLETE: ["open_folder", "play_video", "new_project", "render_again"],
    RenderState.FAILED: ["view_error", "try_again", "start_over", "view_log"],
}

STATE_LABELS: Dict[RenderState, str] = {
    RenderState.IDLE: "Ready",
    RenderState.LOADING: "Loading files...",
    RenderState.VALIDATING: "Checking quality...",
    RenderState.GENERATING: "Generating audio...",
    RenderState.PROCESSING: "Processing...",
    RenderState.RENDERING: "Rendering video...",
    RenderState.EXPORTING: "Exporting...",
    RenderState.PAUSED: "Paused",
    RenderState.CANCELLED: "Cancelling...",
    RenderState.RECOVERING: "Resuming...",
    RenderState.COMPLETE: "Complete!",
    RenderState.FAILED: "Failed",
}

STATE_COLORS: Dict[RenderState, str] = {
    RenderState.IDLE: "#A0A0B0",
    RenderState.LOADING: "#FDCB6E",
    RenderState.VALIDATING: "#FDCB6E",
    RenderState.GENERATING: "#1E90FF",
    RenderState.PROCESSING: "#1E90FF",
    RenderState.RENDERING: "#E94560",
    RenderState.EXPORTING: "#E94560",
    RenderState.PAUSED: "#FDCB6E",
    RenderState.CANCELLED: "#E17055",
    RenderState.RECOVERING: "#FDCB6E",
    RenderState.COMPLETE: "#00B894",
    RenderState.FAILED: "#E17055",
}


class RenderStateMachine:
    """Finite state machine controlling all render states."""

    def __init__(self, event_bus: Any) -> None:
        """Create state machine bound to an event bus.

        Args:
            event_bus: Object with publish(event_name, data) method.
        """
        self._state = RenderState.IDLE
        self._previous_state: Optional[RenderState] = None
        self._event_bus = event_bus
        self._state_callbacks: Dict[RenderState, List[Callable[..., Any]]] = {}
        self._transition_callbacks: List[Callable[..., Any]] = []
        self.log = logger

    @property
    def state(self) -> RenderState:
        """Current render state."""
        return self._state

    @property
    def state_name(self) -> str:
        """Current state name string."""
        return self._state.name

    @property
    def previous_state(self) -> Optional[RenderState]:
        """Previous state if any."""
        return self._previous_state

    def can_transition_to(self, new_state: RenderState) -> bool:
        """Check if transition from current to new state is valid."""
        return new_state in VALID_TRANSITIONS.get(self._state, [])

    def _notify_transition(
        self,
        old_state: RenderState,
        new_state: RenderState,
        reason: str,
    ) -> None:
        """Publish event and run transition callbacks."""
        if self._event_bus is not None:
            self._event_bus.publish(
                "render_state_changed",
                {
                    "old_state": old_state.name,
                    "new_state": new_state.name,
                    "reason": reason,
                },
            )
        for callback in self._state_callbacks.get(new_state, []):
            try:
                callback(old_state, new_state)
            except Exception as exc:  # noqa: BLE001
                self.log.error("State callback error: %s", exc)
        for callback in self._transition_callbacks:
            try:
                callback(old_state, new_state)
            except Exception as exc:  # noqa: BLE001
                self.log.error("Transition callback error: %s", exc)

    def transition_to(self, new_state: RenderState, reason: str = "") -> bool:
        """Attempt to transition to a new state.

        Args:
            new_state: Target state.
            reason: Optional human-readable reason for logs.

        Returns:
            True if transition applied; False if blocked.
        """
        if not self.can_transition_to(new_state):
            self.log.warning(
                "Invalid state transition blocked: %s → %s (reason: %s)",
                self._state.name,
                new_state.name,
                reason,
            )
            return False

        old_state = self._state
        self._previous_state = old_state
        self._state = new_state
        message = f"State transition: {old_state.name} → {new_state.name}"
        if reason:
            message += f" ({reason})"
        self.log.info(message)
        self._notify_transition(old_state, new_state, reason)
        return True

    def is_action_allowed(self, action: str) -> bool:
        """Check if a UI/action is allowed in the current state."""
        allowed = ALLOWED_ACTIONS.get(self._state, [])
        return action in allowed or "all_ui" in allowed

    def on_enter_state(
        self,
        state: RenderState,
        callback: Callable[..., Any],
    ) -> None:
        """Register callback invoked when entering a specific state."""
        self._state_callbacks.setdefault(state, []).append(callback)

    def on_any_transition(self, callback: Callable[..., Any]) -> None:
        """Register callback invoked on any successful transition."""
        self._transition_callbacks.append(callback)

    def reset_to_idle(self) -> None:
        """Force reset to IDLE for error recovery (bypasses transition map)."""
        self._previous_state = self._state
        self._state = RenderState.IDLE
        self.log.warning(
            "Force reset to IDLE from %s",
            self._previous_state.name if self._previous_state else "None",
        )
        if self._event_bus is not None:
            self._event_bus.publish(
                "render_state_changed",
                {
                    "old_state": (
                        self._previous_state.name if self._previous_state else None
                    ),
                    "new_state": "IDLE",
                    "reason": "force_reset",
                },
            )

    def get_status_display(self) -> Dict[str, Any]:
        """Return state info for UI display."""
        return {
            "state": self._state.name,
            "label": STATE_LABELS[self._state],
            "color": STATE_COLORS[self._state],
            "allowed_actions": list(ALLOWED_ACTIONS[self._state]),
        }


def all_render_states() -> List[RenderState]:
    """Return all render states (12 total)."""
    return list(RenderState)
