"""JSON/CSV/table/paragraph script parsers."""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional

from core.errors import ScriptParseError
from modules.file_parser_helpers import (
    ANIMATION_ALIASES,
    CSV_COLUMN_ALIASES,
    SIMPLE_SPEAKER_RE,
    TRANSITION_ALIASES,
    _apply_project_settings,
    _map_json_scene,
    _map_json_voice,
    empty_dialogue,
    empty_parsed_data,
    empty_scene,
    ensure_voice_defaults,
)
from modules.file_parser_structured_io import StructuredScriptIOMixin


class StructuredScriptEngine(StructuredScriptIOMixin):
    """JSON/CSV/table/paragraph script parsers."""

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

    def _map_json_to_parsed(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Map JSON script document to standard structure."""
        data = empty_parsed_data()
        project = raw.get("project") or raw.get("project_settings") or {}
        _apply_project_settings(data, project)
        for voice in raw.get("voice_profiles") or raw.get("voice_instructions") or []:
            data["voice_instructions"].append(_map_json_voice(voice))
        for index, scene in enumerate(raw.get("scenes") or [], start=1):
            data["scenes"].append(
                _map_json_scene(
                    scene,
                    index,
                    self.normalize_transition_name,
                    self.normalize_animation_name,
                )
            )
        ensure_voice_defaults(data)
        return data

    def _parse_csv_text(self, text: str) -> Dict[str, Any]:
        """Parse CSV text into scenes grouped by image."""
        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise ScriptParseError("CSV has no header row")
        mapping = self._map_csv_columns(list(reader.fieldnames))
        data = empty_parsed_data()
        scenes_by_image: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for row in reader:
            self._ingest_csv_row(row, mapping, scenes_by_image, order)
        data["scenes"] = [scenes_by_image[key] for key in order]
        data["project_settings"]["title"] = "Csv Script"
        ensure_voice_defaults(data)
        return data

    def _ingest_csv_row(
        self,
        row: Dict[str, Any],
        mapping: Dict[str, str],
        scenes_by_image: Dict[str, Dict[str, Any]],
        order: List[str],
    ) -> None:
        """Add one CSV row into the scene map."""
        image = (row.get(mapping.get("image", ""), "") or "scene_image.jpg").strip()
        if image not in scenes_by_image:
            scene_number = len(order) + 1
            scene = empty_scene(f"scene_{scene_number:02d}", scene_number)
            scene["image"] = image
            self._apply_csv_scene_fields(scene, row, mapping)
            scenes_by_image[image] = scene
            order.append(image)
        dialogue = self._dialogue_from_csv_row(row, mapping)
        if dialogue is not None:
            scenes_by_image[image]["dialogue"].append(dialogue)

    def _apply_csv_scene_fields(
        self,
        scene: Dict[str, Any],
        row: Dict[str, Any],
        mapping: Dict[str, str],
    ) -> None:
        """Apply optional scene-level CSV columns."""
        if mapping.get("transition"):
            scene["transition_in"] = self.normalize_transition_name(
                row.get(mapping["transition"], "crossfade") or "crossfade"
            )
        if mapping.get("transition_out"):
            scene["transition_out"] = self.normalize_transition_name(
                row.get(mapping["transition_out"], "crossfade") or "crossfade"
            )
        if mapping.get("animation"):
            scene["animation"] = self.normalize_animation_name(
                row.get(mapping["animation"], "ken_burns") or "ken_burns"
            )
        if mapping.get("sfx"):
            scene["sfx"] = (row.get(mapping["sfx"]) or "").strip()
        chapter_key = mapping.get("chapter")
        if chapter_key and (row.get(chapter_key) or "").strip():
            scene["is_chapter_start"] = True
            scene["chapter_title"] = (row.get(chapter_key) or "").strip()

    def _dialogue_from_csv_row(
        self,
        row: Dict[str, Any],
        mapping: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        """Build a dialogue dict from a CSV row, or None if empty text."""
        character = "NARRATOR"
        emotion = "neutral"
        if mapping.get("character"):
            character = (row.get(mapping["character"]) or "NARRATOR").strip()
        if mapping.get("emotion"):
            emotion = (row.get(mapping["emotion"]) or "neutral").strip()
        text_col = mapping.get("text")
        line_text = (row.get(text_col, "") if text_col else "").strip()
        if not line_text:
            return None
        dialogue = empty_dialogue(character, emotion)
        if mapping.get("pause"):
            dialogue["pause_after"] = (
                (row.get(mapping["pause"]) or "short").strip().lower()
            )
        if mapping.get("speed"):
            try:
                dialogue["speed"] = float(row.get(mapping["speed"]) or 1.0)
            except ValueError:
                dialogue["speed"] = None
        dialogue["text"] = line_text
        return dialogue

    def _map_csv_columns(self, fieldnames: List[str]) -> Dict[str, str]:
        """Map CSV headers to logical field names."""
        lower_map = {name.lower().strip(): name for name in fieldnames}
        result: Dict[str, str] = {}
        for logical, aliases in CSV_COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in lower_map:
                    result[logical] = lower_map[alias]
                    break
        if "text" not in result and fieldnames:
            # last column often text
            result["text"] = fieldnames[-1]
        return result

    def _parse_table_rows(self, rows: List[List[str]]) -> Dict[str, Any]:
        """Parse a 2D table (DOCX/PDF) like CSV."""
        if not rows:
            return empty_parsed_data()
        header = [c.strip() for c in rows[0]]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(header)
        for row in rows[1:]:
            writer.writerow(row)
        return self._parse_csv_text(output.getvalue())

    def _parse_paragraph_script(self, paragraphs: List[str]) -> Dict[str, Any]:
        """Parse NAME: text / NAME (emotion): text paragraphs."""
        data = empty_parsed_data()
        scene = empty_scene("scene_01", 1)
        scene["image"] = "placeholder.jpg"
        for paragraph in paragraphs:
            match = SIMPLE_SPEAKER_RE.match(paragraph.strip())
            if match:
                dialogue = empty_dialogue(match.group(1), match.group(2) or "neutral")
                dialogue["text"] = match.group(3).strip()
                scene["dialogue"].append(dialogue)
            else:
                dialogue = empty_dialogue("NARRATOR", "neutral")
                dialogue["text"] = paragraph.strip()
                scene["dialogue"].append(dialogue)
        data["scenes"].append(scene)
        ensure_voice_defaults(data)
        return data
