"""Shared constants and helpers for file parsing.

Parses TXT, JSON, CSV, DOCX, and PDF scripts into the standard internal
data structure. Phase A image matching: exact + fuzzy filename only.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict

MODULE_NAME = "file_parser"

SUPPORTED_SCRIPT_FORMATS = ["txt", "json", "csv", "docx", "pdf"]
SUPPORTED_IMAGE_FORMATS = ["jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif"]
SUPPORTED_AUDIO_FORMATS = ["mp3", "wav", "flac", "aac", "ogg", "m4a"]
SUPPORTED_SUBTITLE_FORMATS = ["srt", "ass", "vtt"]

HEADER_PREFIXES = (
    ("//TITLE:", "title"),
    ("//CHANNEL:", "channel"),
    ("//GENRE:", "genre"),
    ("//COLOR_GRADE:", "color_grade"),
    ("//TRANSITION:", "default_transition"),
    ("//ANIMATION:", "default_animation"),
    ("//MUSIC:", "music_file"),
    ("//EXPORT:", "export_preset"),
)

SCENE_DIRECTIVES = (
    ("//IMAGE:", "image"),
    ("//DURATION:", "duration"),
    ("//TRANSITION_IN:", "transition_in"),
    ("//TRANSITION_OUT:", "transition_out"),
    ("//ANIMATION:", "animation"),
    ("//COLOR_GRADE:", "color_grade"),
    ("//SFX:", "sfx"),
    ("//CAPTION:", "caption"),
    ("//NOTES:", "notes"),
    ("//CHAPTER:", "chapter_title"),
)

CHARACTER_TAG_RE = re.compile(r"^\[([A-Za-z0-9_]+)(?:\|([a-zA-Z0-9_]+))?\]\s*$")
# FEATURE (v3.2.12): the standalone tag above only matches a [Name] cue
# alone on its own line. Scripts commonly write the speaker cue and
# their line together, e.g. "[Samir] Jesus, please save us." — this
# was previously unrecognized entirely, so "[Samir]" would have been
# read aloud as literal text. Matches [Name] (or [Name|emotion])
# immediately followed by the spoken text on the SAME line.
INLINE_CHARACTER_TAG_RE = re.compile(
    r"^\[([A-Za-z0-9_]+)(?:\|([a-zA-Z0-9_]+))?\]\s+(\S.*)$"
)
# BUGFIX (found during testing, v3.2.12): without this guard, a line
# like "[SHORT_PAUSE] Rest now in his peace." would be misread as a
# character named "SHORT_PAUSE" speaking — the pause-tag vocabulary
# (handled later, at TTS time, inside tts_engine_manager.py) collides
# character-for-character with what looks like a valid bracket name
# here. Any name matching one of these reserved words is never treated
# as a character switch, regardless of case.
RESERVED_INLINE_TAG_NAMES = frozenset({
    "PAUSE", "SHORT_PAUSE", "MEDIUM_PAUSE", "LONG_PAUSE", "SILENCE",
    "SPELL", "HOST_VOICE", "SCRIPTURE_VOICE",
})
PAUSE_LINE_RE = re.compile(r"^\[PAUSE:([A-Za-z0-9_]+)\]\s*$", re.IGNORECASE)
INLINE_PAUSE_RE = re.compile(r"\[PAUSE:([A-Za-z0-9_]+)\]", re.IGNORECASE)
VOICE_LINE_RE = re.compile(r"^([A-Za-z0-9_]+)\s*:\s*(.+)$")
SIMPLE_SPEAKER_RE = re.compile(
    r"^([A-Za-z0-9_]+)(?:\s*\(([a-zA-Z0-9_]+)\))?\s*:\s*(.+)$"
)

TRANSITION_ALIASES = {
    "crossfade": "crossfade",
    "cross_fade": "crossfade",
    "xfade": "crossfade",
    "fade": "fade",
    "dissolve": "dissolve",
    "dip_to_black": "dip_to_black",
    "dip_black": "dip_to_black",
    "dip to black": "dip_to_black",
    "dipblack": "dip_to_black",
    "hard_cut": "hard_cut",
    "cut": "hard_cut",
    "slide_left": "slide_left",
    "slideleft": "slide_left",
    "slide left": "slide_left",
    "slide_right": "slide_right",
    "slideright": "slide_right",
}

ANIMATION_ALIASES = {
    "ken_burns": "ken_burns",
    "kenburns": "ken_burns",
    "ken burns": "ken_burns",
    "slow_zoom_in": "slow_zoom_in",
    "zoom_in": "slow_zoom_in",
    "slow_zoom_out": "slow_zoom_out",
    "zoom_out": "slow_zoom_out",
    "pan_left": "pan_left",
    "pan_right": "pan_right",
    "static": "static",
}

CSV_COLUMN_ALIASES = {
    "character": ("character", "char", "speaker", "name"),
    "emotion": ("emotion", "mood", "style"),
    "text": ("text", "dialogue", "content", "line", "script"),
    "image": ("image", "img", "file", "picture", "filename"),
    "transition": ("transition", "trans", "transition_in"),
    "transition_out": ("transition_out",),
    "animation": ("animation", "anim", "effect"),
    "speed": ("speed", "rate", "pace"),
    "pause": ("pause", "pause_after"),
    "sfx": ("sfx", "sound"),
    "chapter": ("chapter", "chapter_title"),
}


def empty_parsed_data() -> Dict[str, Any]:
    """Return an empty standard parsed script structure."""
    return {
        "project_settings": {
            "title": "",
            "channel": "",
            "genre": "",
            "color_grade": "",
            "default_transition": "",
            "default_animation": "",
            "music_file": "",
            "export_preset": "",
        },
        "voice_instructions": [],
        "scenes": [],
    }


def empty_scene(scene_id: str, scene_number: int) -> Dict[str, Any]:
    """Return a blank scene dict with defaults."""
    return {
        "id": scene_id,
        "scene_number": scene_number,
        "image": "",
        "duration": "auto",
        "transition_in": "crossfade",
        "transition_out": "crossfade",
        "animation": "ken_burns",
        "color_grade": "",
        "sfx": "",
        "caption": "",
        "notes": "",
        "is_chapter_start": False,
        "chapter_title": "",
        "dialogue": [],
    }


def empty_dialogue(
    character: str = "NARRATOR", emotion: str = "neutral"
) -> Dict[str, Any]:
    """Return a blank dialogue line dict."""
    return {
        "character": character.upper(),
        "emotion": emotion.lower() if emotion else "neutral",
        "speed": None,
        "pause_before": "none",
        "pause_after": "short",
        "text": "",
    }


def _apply_project_settings(data: Dict[str, Any], project: Dict[str, Any]) -> None:
    """Copy project keys from JSON project block into parsed settings."""
    for key in data["project_settings"]:
        if key in project and project[key] is not None:
            data["project_settings"][key] = project[key]
    if "music" in project and not data["project_settings"]["music_file"]:
        data["project_settings"]["music_file"] = project["music"]
    if "export_preset" in project:
        data["project_settings"]["export_preset"] = project["export_preset"]


def _map_json_voice(voice: Dict[str, Any]) -> Dict[str, Any]:
    """Map one JSON voice profile entry."""
    return {
        "character": str(voice.get("character", "NARRATOR")).upper(),
        "voice": voice.get("voice", "default"),
        "engine": voice.get("engine", "piper"),
        "emotion": str(
            voice.get("default_emotion") or voice.get("emotion") or "neutral"
        ),
        "speed": float(voice.get("speed", 1.0)),
        "pitch": voice.get("pitch", 0),
        "reverb": voice.get("reverb", "none"),
        "echo": voice.get("echo", "none"),
        "breathing": bool(voice.get("breathing", False)),
        "pause_sentence": float(voice.get("pause_sentence", 0.6)),
        "pause_paragraph": float(voice.get("pause_paragraph", 1.8)),
    }


def _map_json_scene(
    scene: Dict[str, Any], index: int, normalize_transition, normalize_animation
) -> Dict[str, Any]:
    """Map one JSON scene entry to internal scene structure."""
    scene_id = str(scene.get("id") or f"scene_{index:02d}")
    mapped = empty_scene(scene_id, index)
    mapped["image"] = scene.get("image") or scene.get("image_filename") or ""
    mapped["duration"] = scene.get("duration", "auto")
    mapped["transition_in"] = normalize_transition(
        scene.get("transition_in", "crossfade")
    )
    mapped["transition_out"] = normalize_transition(
        scene.get("transition_out", "crossfade")
    )
    mapped["animation"] = normalize_animation(scene.get("animation", "ken_burns"))
    mapped["color_grade"] = scene.get("color_grade", "")
    mapped["sfx"] = scene.get("sfx") or ""
    mapped["is_chapter_start"] = bool(
        scene.get("is_chapter") or scene.get("is_chapter_start")
    )
    mapped["chapter_title"] = scene.get("chapter_title") or ""
    for dialogue in scene.get("dialogue") or []:
        mapped["dialogue"].append(
            {
                "character": str(dialogue.get("character", "NARRATOR")).upper(),
                "emotion": str(dialogue.get("emotion") or "neutral").lower(),
                "speed": dialogue.get("speed"),
                "pause_before": str(dialogue.get("pause_before") or "none"),
                "pause_after": str(dialogue.get("pause_after") or "short"),
                "text": str(dialogue.get("text") or "").strip(),
            }
        )
    return mapped


def _match_result(
    status: str, item: Dict[str, str], confidence: float
) -> Dict[str, Any]:
    """Build a match result dict."""
    return {
        "status": status,
        "path": item["path"],
        "matched_name": item["filename"],
        "confidence": confidence,
    }


def _normalize_name(name: str) -> str:
    """Normalize filename stem for comparison."""
    value = name.lower().strip()
    value = value.replace("-", "_").replace(" ", "_")
    value = re.sub(r"_+", "_", value)
    return value


def _read_text(path: Path) -> str:
    """Read text file with encoding detection fallback."""
    raw = path.read_bytes()
    try:
        import chardet

        detected = chardet.detect(raw) or {}
        encoding = detected.get("encoding") or "utf-8"
    except Exception:  # noqa: BLE001
        encoding = "utf-8"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _elapsed_ms(started: float) -> float:
    """Return elapsed milliseconds since started perf counter."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def ensure_voice_defaults(data: Dict[str, Any]) -> None:
    """Add default voice entries for characters missing instructions."""
    existing = {str(v.get("character", "")).upper() for v in data["voice_instructions"]}
    for scene in data["scenes"]:
        for line in scene.get("dialogue") or []:
            character = str(line.get("character", "NARRATOR")).upper()
            if character not in existing:
                data["voice_instructions"].append(
                    {
                        "character": character,
                        "voice": "default",
                        "engine": "piper",
                        "emotion": str(line.get("emotion") or "neutral"),
                        "speed": 1.0,
                        "pitch": 0,
                        "reverb": "none",
                        "echo": "none",
                        "breathing": False,
                        "pause_sentence": 0.6,
                        "pause_paragraph": 1.8,
                    }
                )
                existing.add(character)
