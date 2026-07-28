"""TXT line handlers for Autopilot script parsing."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from modules.file_parser_helpers import (
    HEADER_PREFIXES,
    VOICE_LINE_RE,
    empty_dialogue,
    empty_scene,
)


class TxtHeaderVoiceMixin:
    """Header, voice setup, and state helpers."""

    def _txt_state_header(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        scene_number: int,
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int, str]:
        """Handle one line in TXT header state."""
        state, current_scene, scene_number = self._handle_header_line(
            line, data, current_scene, scene_number
        )
        return (
            state,
            current_scene,
            current_dialogue,
            scene_number,
            pending_pause_before,
        )

    def _txt_state_voice(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        scene_number: int,
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int, str]:
        """Handle one line in TXT voice_setup state."""
        if line == "//VOICE_SETUP_END":
            return "header", current_scene, current_dialogue, scene_number, "none"
        if line:
            voice = self._parse_voice_line(line)
            if voice:
                data["voice_instructions"].append(voice)
        return (
            "voice_setup",
            current_scene,
            current_dialogue,
            scene_number,
            pending_pause_before,
        )

    def _txt_state_scene(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        scene_number: int,
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int, str]:
        """Handle one line in TXT scene state."""
        state, current_scene, current_dialogue, pending_pause_before = (
            self._handle_scene_line(
                line, data, current_scene, current_dialogue, pending_pause_before
            )
        )
        return (
            state,
            current_scene,
            current_dialogue,
            scene_number,
            pending_pause_before,
        )

    def _txt_state_dialogue(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        scene_number: int,
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int, str]:
        """Handle one line in TXT dialogue state."""
        state, current_scene, current_dialogue, pending_pause_before = (
            self._handle_dialogue_line(
                line, data, current_scene, current_dialogue, pending_pause_before
            )
        )
        return (
            state,
            current_scene,
            current_dialogue,
            scene_number,
            pending_pause_before,
        )

    def _handle_header_line(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        scene_number: int,
    ) -> Tuple[str, Optional[Dict[str, Any]], int]:
        """Process a line while in header state."""
        if not line or line.startswith("//AUTOPILOT") or line.startswith("//AUTODOKU"):
            return "header", current_scene, scene_number
        for prefix, key in HEADER_PREFIXES:
            if line.upper().startswith(prefix):
                data["project_settings"][key] = line[len(prefix) :].strip()
                return "header", current_scene, scene_number
        if line == "//VOICE_SETUP_START":
            return "voice_setup", current_scene, scene_number
        if line.upper().startswith("//SCENE_START"):
            scene_number += 1
            scene_id = (
                line.split(":", 1)[1].strip()
                if ":" in line
                else f"scene_{scene_number:02d}"
            )
            current_scene = empty_scene(
                scene_id or f"scene_{scene_number:02d}", scene_number
            )
            return "scene", current_scene, scene_number
        # Minimal plain-text fallback: accumulate later via paragraph path
        if line and not line.startswith("//") and not line.startswith("["):
            # defer to simple mode only if no scenes later; store as free text blob
            data.setdefault("_free_text", []).append(line)
        return "header", current_scene, scene_number

    def _finalize_open_blocks(
        self,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
    ) -> None:
        """Flush any open scene/dialogue at end of file."""
        if current_scene is not None:
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
            data["scenes"].append(current_scene)
        # Plain text fallback if no structured scenes
        if not data["scenes"] and data.get("_free_text"):
            scene = empty_scene("scene_01", 1)
            scene["image"] = "placeholder.jpg"
            scene["dialogue"].append(
                {
                    **empty_dialogue("NARRATOR", "neutral"),
                    "text": " ".join(data["_free_text"]),
                }
            )
            data["scenes"].append(scene)
        data.pop("_free_text", None)

    def _parse_voice_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse CHARACTER: key=value, key=value voice setup line."""
        match = VOICE_LINE_RE.match(line)
        if not match:
            return None
        character = match.group(1).upper()
        pairs = match.group(2)
        voice: Dict[str, Any] = {
            "character": character,
            "voice": "",
            "engine": "piper",
            "emotion": "neutral",
            "speed": 1.0,
            "pitch": 0,
            "reverb": "none",
            "echo": "none",
            "breathing": False,
            "pause_sentence": 0.6,
            "pause_paragraph": 1.8,
        }
        for part in pairs.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in ("speed", "pitch", "pause_sentence", "pause_paragraph", "volume"):
                try:
                    voice[key] = float(value)
                except ValueError:
                    voice[key] = value
            elif key == "breathing":
                voice[key] = value.lower() in ("on", "true", "1", "yes")
            elif key == "emotion":
                voice[key] = value.lower()
            else:
                voice[key] = value
        return voice
