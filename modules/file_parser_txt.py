"""TXT state-machine script parser engine."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from modules.file_parser_helpers import (
    ANIMATION_ALIASES,
    TRANSITION_ALIASES,
    _elapsed_ms,
    _read_text,
    empty_parsed_data,
    ensure_voice_defaults,
)
from modules.file_parser_txt_handlers import TxtLineHandlerMixin


class TxtScriptEngine(TxtLineHandlerMixin):
    """Parse Autopilot TXT documentary scripts."""

    def normalize_transition_name(self, name: str) -> str:
        """Normalize transition aliases to a standard id."""
        key = str(name or "").strip().lower().replace("-", "_")
        key = re.sub(r"\s+", " ", key)
        if key in TRANSITION_ALIASES:
            return TRANSITION_ALIASES[key]
        return TRANSITION_ALIASES.get(key.replace(" ", "_"), "crossfade")

    def normalize_animation_name(self, name: str) -> str:
        """Normalize animation aliases to a standard id."""
        key = str(name or "").strip().lower().replace("-", "_")
        key = re.sub(r"\s+", " ", key)
        if key in ANIMATION_ALIASES:
            return ANIMATION_ALIASES[key]
        return ANIMATION_ALIASES.get(key.replace(" ", "_"), "ken_burns")

    def _parse_txt_content(self, text: str) -> Dict[str, Any]:
        """Run the TXT state-machine parser on full script text."""
        data = empty_parsed_data()
        state = "header"
        current_scene: Optional[Dict[str, Any]] = None
        current_dialogue: Optional[Dict[str, Any]] = None
        scene_number = 0
        pending_pause_before = "none"
        for raw_line in text.splitlines():
            (
                state,
                current_scene,
                current_dialogue,
                scene_number,
                pending_pause_before,
            ) = self._advance_txt_state(
                raw_line.strip(),
                state,
                data,
                current_scene,
                current_dialogue,
                scene_number,
                pending_pause_before,
            )
        self._finalize_open_blocks(data, current_scene, current_dialogue)
        ensure_voice_defaults(data)
        return data

    def _advance_txt_state(
        self,
        line: str,
        state: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        scene_number: int,
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int, str]:
        """Advance TXT parser state machine by one line."""
        handlers = {
            "header": self._txt_state_header,
            "voice_setup": self._txt_state_voice,
            "scene": self._txt_state_scene,
            "dialogue": self._txt_state_dialogue,
        }
        handler = handlers.get(state)
        if handler is None:
            return (
                state,
                current_scene,
                current_dialogue,
                scene_number,
                pending_pause_before,
            )
        return handler(
            line,
            data,
            current_scene,
            current_dialogue,
            scene_number,
            pending_pause_before,
        )

    def parse_txt_file(self, file_path, make_response):
        """Parse TXT file path into standard response."""
        started = time.perf_counter()
        try:
            text = _read_text(Path(file_path))
            data = self._parse_txt_content(text)
            if not data["scenes"]:
                return make_response(
                    False,
                    data=data,
                    error="No scenes found in TXT script",
                    duration_ms=_elapsed_ms(started),
                )
            return make_response(True, data=data, duration_ms=_elapsed_ms(started))
        except Exception as exc:  # noqa: BLE001
            return make_response(
                False, error=str(exc), duration_ms=_elapsed_ms(started)
            )
