"""TXT line handlers for Autopilot script parsing."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from modules.file_parser_helpers import (
    CHARACTER_TAG_RE,
    INLINE_CHARACTER_TAG_RE,
    INLINE_PAUSE_RE,
    PAUSE_LINE_RE,
    RESERVED_INLINE_TAG_NAMES,
    SCENE_DIRECTIVES,
    empty_dialogue,
    empty_scene,
)


class TxtSceneDialogueMixin:
    """Scene and dialogue line handlers."""

    @staticmethod
    def _match_inline_character(line: str):
        """Match "[Name] spoken text" on one line; None if not a real cue.

        FEATURE (v3.2.12): returns (name, emotion, text) for a genuine
        inline character cue, or None if the line either doesn't match
        the shape at all, or the bracketed word is a reserved pause/
        spell tag keyword (which must fall through to normal dialogue
        text handling instead — see RESERVED_INLINE_TAG_NAMES).
        """
        match = INLINE_CHARACTER_TAG_RE.match(line)
        if not match:
            return None
        if match.group(1).upper() in RESERVED_INLINE_TAG_NAMES:
            return None
        return match.group(1), match.group(2) or "neutral", match.group(3)

    def _handle_scene_line(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        """Process a line while in scene metadata state."""
        if current_scene is None:
            return "header", None, None, "none"
        if line == "//SCENE_END":
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
            data["scenes"].append(current_scene)
            return "header", None, None, "none"
        if self._apply_scene_directive(current_scene, line):
            return "scene", current_scene, current_dialogue, pending_pause_before
        pause_match = PAUSE_LINE_RE.match(line)
        if pause_match:
            return (
                "scene",
                current_scene,
                current_dialogue,
                pause_match.group(1).lower(),
            )
        char_match = CHARACTER_TAG_RE.match(line)
        if char_match:
            current_dialogue = empty_dialogue(
                char_match.group(1), char_match.group(2) or "neutral"
            )
            current_dialogue["pause_before"] = pending_pause_before
            return "dialogue", current_scene, current_dialogue, "none"
        inline_char = self._match_inline_character(line)
        if inline_char:
            name, emotion, text = inline_char
            current_dialogue = empty_dialogue(name, emotion)
            current_dialogue["pause_before"] = pending_pause_before
            self._append_dialogue_text(current_dialogue, text)
            return "dialogue", current_scene, current_dialogue, "none"
        if line.upper().startswith("//SCENE_START"):
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
            data["scenes"].append(current_scene)
            scene_number = len(data["scenes"]) + 1
            scene_id = (
                line.split(":", 1)[1].strip()
                if ":" in line
                else f"scene_{scene_number:02d}"
            )
            return "scene", empty_scene(scene_id, scene_number), None, "none"
        return "scene", current_scene, current_dialogue, pending_pause_before

    def _handle_dialogue_line(
        self,
        line: str,
        data: Dict[str, Any],
        current_scene: Optional[Dict[str, Any]],
        current_dialogue: Optional[Dict[str, Any]],
        pending_pause_before: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        """Process a line while collecting dialogue text."""
        if current_scene is None:
            return "header", None, None, "none"
        if line == "//SCENE_END":
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
            data["scenes"].append(current_scene)
            return "header", None, None, "none"
        pause_match = PAUSE_LINE_RE.match(line)
        if pause_match:
            if current_dialogue:
                current_dialogue["pause_after"] = pause_match.group(1).lower()
            return (
                "dialogue",
                current_scene,
                current_dialogue,
                pause_match.group(1).lower(),
            )
        char_match = CHARACTER_TAG_RE.match(line)
        if char_match:
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
            current_dialogue = empty_dialogue(
                char_match.group(1), char_match.group(2) or "neutral"
            )
            current_dialogue["pause_before"] = pending_pause_before
            return "dialogue", current_scene, current_dialogue, "none"
        inline_char = self._match_inline_character(line)
        if inline_char:
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
            name, emotion, text = inline_char
            current_dialogue = empty_dialogue(name, emotion)
            current_dialogue["pause_before"] = pending_pause_before
            self._append_dialogue_text(current_dialogue, text)
            return "dialogue", current_scene, current_dialogue, "none"
        if line.startswith("//"):
            if current_dialogue and current_dialogue.get("text"):
                current_scene["dialogue"].append(current_dialogue)
                current_dialogue = None
            return self._handle_scene_line(
                line, data, current_scene, current_dialogue, pending_pause_before
            )
        if not line:
            return "dialogue", current_scene, current_dialogue, pending_pause_before
        if current_dialogue is None:
            current_dialogue = empty_dialogue("NARRATOR", "neutral")
            current_dialogue["pause_before"] = pending_pause_before
        self._append_dialogue_text(current_dialogue, line)
        return "dialogue", current_scene, current_dialogue, "none"

    def _apply_scene_directive(self, scene: Dict[str, Any], line: str) -> bool:
        """Apply //IMAGE etc directive to scene. Return True if handled."""
        for prefix, key in SCENE_DIRECTIVES:
            if line.upper().startswith(prefix):
                value = line[len(prefix) :].strip()
                if key == "chapter_title":
                    scene["is_chapter_start"] = True
                    scene["chapter_title"] = value
                elif key in ("transition_in", "transition_out"):
                    scene[key] = self.normalize_transition_name(value)
                elif key == "animation":
                    scene[key] = self.normalize_animation_name(value)
                else:
                    scene[key] = value
                return True
        return False

    def _append_dialogue_text(self, dialogue: Dict[str, Any], line: str) -> None:
        """Append spoken text and capture inline pause tags."""
        cleaned = INLINE_PAUSE_RE.sub("", line).strip()
        if dialogue["text"]:
            dialogue["text"] += " " + cleaned
        else:
            dialogue["text"] = cleaned
        inline = INLINE_PAUSE_RE.findall(line)
        if inline:
            dialogue["pause_after"] = inline[-1].lower()
