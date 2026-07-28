"""Filename exact and fuzzy image matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.file_parser_helpers import (  # noqa: F401
    ANIMATION_ALIASES,
    CHARACTER_TAG_RE,
    CSV_COLUMN_ALIASES,
    HEADER_PREFIXES,
    INLINE_PAUSE_RE,
    MODULE_NAME,
    PAUSE_LINE_RE,
    SCENE_DIRECTIVES,
    SIMPLE_SPEAKER_RE,
    SUPPORTED_AUDIO_FORMATS,
    SUPPORTED_IMAGE_FORMATS,
    SUPPORTED_SCRIPT_FORMATS,
    SUPPORTED_SUBTITLE_FORMATS,
    TRANSITION_ALIASES,
    VOICE_LINE_RE,
    _apply_project_settings,
    _elapsed_ms,
    _map_json_scene,
    _map_json_voice,
    _match_result,
    _normalize_name,
    _read_text,
    empty_dialogue,
    empty_parsed_data,
    empty_scene,
    ensure_voice_defaults,
)


class ImageMatchEngine:
    """Filename exact and fuzzy image matching."""

    def _build_image_index(self, folder: Path) -> List[Dict[str, str]]:
        """Build searchable index of images in folder."""
        index: List[Dict[str, str]] = []
        if not folder.exists():
            return index
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower().strip(".") not in SUPPORTED_IMAGE_FORMATS:
                continue
            stem = path.stem
            index.append(
                {
                    "path": str(path),
                    "filename": path.name,
                    "stem": stem,
                    "lower": path.name.lower(),
                    "stem_lower": stem.lower(),
                    "normalized": _normalize_name(stem),
                }
            )
        return index

    def _match_single_image(
        self,
        requested: str,
        index: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Match one requested image name against the index."""
        if not requested:
            return {
                "status": "unmatched",
                "path": None,
                "matched_name": None,
                "confidence": 0.0,
            }
        req = {
            "name": Path(requested).name,
            "stem": Path(requested).stem,
            "lower": Path(requested).name.lower(),
            "stem_lower": Path(requested).stem.lower(),
            "normalized": _normalize_name(Path(requested).stem),
        }
        exact = self._exact_image_match(req, index)
        if exact is not None:
            return exact
        return self._fuzzy_image_match(req, index)

    def _exact_image_match(
        self,
        req: Dict[str, str],
        index: List[Dict[str, str]],
    ) -> Optional[Dict[str, Any]]:
        """Return exact/normalized match result or None."""
        rules = (
            ("filename", req["name"], 1.0),
            ("lower", req["lower"], 0.99),
            ("stem_lower", req["stem_lower"], 0.98),
            ("normalized", req["normalized"], 0.95),
        )
        for field, value, confidence in rules:
            for item in index:
                if item[field] == value:
                    return _match_result("exact", item, confidence)
        return None

    def _fuzzy_image_match(
        self,
        req: Dict[str, str],
        index: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Fuzzy-match requested image using thefuzz ratios."""
        try:
            from thefuzz import fuzz
        except ImportError:
            return {
                "status": "unmatched",
                "path": None,
                "matched_name": None,
                "confidence": 0.0,
            }
        best_score = 0
        best_item: Optional[Dict[str, str]] = None
        for item in index:
            score = max(
                fuzz.ratio(req["stem_lower"], item["stem_lower"]),
                fuzz.ratio(req["normalized"], item["normalized"]),
            )
            if score > best_score:
                best_score = score
                best_item = item
        if best_item and best_score >= 75:
            status = "fuzzy" if best_score < 90 else "exact"
            return _match_result(status, best_item, best_score / 100.0)
        return {
            "status": "unmatched",
            "path": None,
            "matched_name": None,
            "confidence": best_score / 100.0 if best_item else 0.0,
        }

    def match_images(self, scenes, image_folder, make_response):
        """Match scene image filenames to files (exact + fuzzy)."""
        import time
        from pathlib import Path

        started = time.perf_counter()
        index = self._build_image_index(Path(image_folder))
        matches = []
        exact = fuzzy = unmatched = 0
        for scene in scenes:
            requested = str(scene.get("image") or "")
            result = self._match_single_image(requested, index)
            matches.append(
                {"scene_id": scene.get("id"), "requested": requested, **result}
            )
            if result["status"] == "exact":
                exact += 1
            elif result["status"] == "fuzzy":
                fuzzy += 1
            else:
                unmatched += 1
        return make_response(
            True,
            {
                "matches": matches,
                "exact": exact,
                "fuzzy": fuzzy,
                "unmatched": unmatched,
                "total": len(matches),
            },
            warnings=(
                [f"{unmatched} scenes have no matching images"] if unmatched else []
            ),
            duration_ms=_elapsed_ms(started),
        )
