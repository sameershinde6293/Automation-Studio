"""Keyword analyzer module: mood, SFX, transition, and animation detection.

Module 25 (optional). Loads keyword_emotion_map.json and scores scene text
to drive automatic documentary decisions. Can be disabled without breaking
the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: I001

from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "keyword_analyzer"

# All 28 TTS emotion presets (tts_engine_manager) — analyzer must support each.
TTS_EMOTIONS: Tuple[str, ...] = (
    "neutral",
    "calm",
    "serious",
    "dramatic",
    "mysterious",
    "excited",
    "sad",
    "angry",
    "fearful",
    "whisper",
    "tense",
    "reverent",
    "investigative",
    "authoritative",
    "conspiratorial",
    "ominous",
    "shocked",
    "melancholic",
    "urgent",
    "nostalgic",
    "cold",
    "haunted",
    "solemn",
    "contemplative",
    "incredulous",
    "compassionate",
    "detached",
    "accusatory",
)


class KeywordAnalyzer(BaseModule):
    """Analyze script text for mood keywords and automatic style suggestions."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize analyzer and load keyword maps from config.

        Args:
            container: DI service container.
        """
        super().__init__(container, MODULE_NAME)
        self._emotion_keywords: Dict[str, List[str]] = {}
        self._sfx_keywords: Dict[str, List[str]] = {}
        self._animation_keywords: Dict[str, List[str]] = {}
        self._transition_keywords: Dict[str, List[str]] = {}
        self._mood_to_transition: Dict[str, str] = {}
        self._mood_to_animation: Dict[str, str] = {}
        self._compiled_emotions: Dict[str, List[re.Pattern[str]]] = {}
        self._compiled_sfx: Dict[str, List[re.Pattern[str]]] = {}
        self._compiled_anim: Dict[str, List[re.Pattern[str]]] = {}
        self._compiled_trans: Dict[str, List[re.Pattern[str]]] = {}
        self._load_keyword_map()

    def _load_keyword_map(self) -> None:
        """Load and compile keyword maps from ConfigService / JSON file."""
        raw: Dict[str, Any] = {}
        try:
            raw = self.config.get_config("keyword_emotion_map") or {}
        except Exception as exc:  # noqa: BLE001
            self.log.warning("ConfigService keyword map load failed: %s", exc)
        if not raw or "emotions" not in raw:
            raw = self._load_map_from_disk()
        self._emotion_keywords = {
            str(k).lower(): [str(x).lower() for x in (v or [])]
            for k, v in (raw.get("emotions") or {}).items()
        }
        # Support File 11 sfx as name -> list, and Phase A name -> single string
        sfx_raw = raw.get("sfx_keywords") or raw.get("sfx_trigger_keywords") or {}
        self._sfx_keywords = self._normalize_trigger_map(sfx_raw)
        anim_raw = (
            raw.get("animation_keywords") or raw.get("animation_trigger_keywords") or {}
        )
        self._animation_keywords = self._normalize_trigger_map(anim_raw)
        trans_raw = raw.get("transition_keywords") or {}
        self._transition_keywords = self._normalize_trigger_map(trans_raw)
        self._mood_to_transition = {
            str(k).lower(): str(v)
            for k, v in (raw.get("mood_to_transition") or {}).items()
        }
        self._mood_to_animation = {
            str(k).lower(): str(v)
            for k, v in (raw.get("mood_to_animation") or {}).items()
        }
        self._compiled_emotions = self._compile_map(self._emotion_keywords)
        self._compiled_sfx = self._compile_map(self._sfx_keywords)
        self._compiled_anim = self._compile_map(self._animation_keywords)
        self._compiled_trans = self._compile_map(self._transition_keywords)
        self.log.info(
            "Keyword map loaded: %s emotions, %s sfx groups",
            len(self._emotion_keywords),
            len(self._sfx_keywords),
        )

    def _load_map_from_disk(self) -> Dict[str, Any]:
        """Fallback load of keyword_emotion_map.json from config folder."""
        candidates = [
            Path("config/keyword_emotion_map.json"),
            Path(__file__).resolve().parents[1] / "config" / "keyword_emotion_map.json",
        ]
        for path in candidates:
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    self.log.error("Failed reading %s: %s", path, exc)
        return {"emotions": {}, "sfx_keywords": {}}

    @staticmethod
    def _normalize_trigger_map(raw: Dict[str, Any]) -> Dict[str, List[str]]:
        """Normalize trigger maps to name -> list of lowercase keywords."""
        result: Dict[str, List[str]] = {}
        for key, value in (raw or {}).items():
            name = str(key).lower()
            if isinstance(value, list):
                result[name] = [str(x).lower() for x in value]
            elif isinstance(value, str):
                # Phase A inverted form: keyword -> sfx_name
                sfx_name = value.lower()
                result.setdefault(sfx_name, []).append(name)
            else:
                result[name] = []
        return result

    @staticmethod
    def _compile_map(
        mapping: Dict[str, List[str]],
    ) -> Dict[str, List[re.Pattern[str]]]:
        """Compile keyword lists into regex patterns for whole-phrase matches."""
        compiled: Dict[str, List[re.Pattern[str]]] = {}
        for name, keywords in mapping.items():
            patterns: List[re.Pattern[str]] = []
            for keyword in keywords:
                token = re.escape(keyword.strip().lower())
                if not token:
                    continue
                if " " in keyword:
                    patterns.append(re.compile(rf"(?<!\w){token}(?!\w)", re.IGNORECASE))
                else:
                    patterns.append(re.compile(rf"\b{token}\b", re.IGNORECASE))
            compiled[name] = patterns
        return compiled

    def analyze_scene_text(self, text: str) -> Dict[str, Any]:
        """Analyze free text and return mood / SFX / style suggestions.

        Args:
            text: Scene dialogue or narrative text.

        Returns:
            Standard response with primary_mood, secondary_mood, confidence,
            all_moods, detected_sfx, recommended_animation, recommended_transition.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(
                False,
                error="keyword_analyzer is disabled",
                duration_ms=_ms(started),
            )
        cleaned = (text or "").strip()
        if not cleaned:
            payload = self._empty_analysis()
            return self.make_response(True, data=payload, duration_ms=_ms(started))

        scores = self._score_emotions(cleaned)
        primary, secondary, confidence = self._rank_moods(scores)
        sfx_resp = self.detect_sfx_keywords(cleaned)
        trans_resp = self.detect_transition_style(cleaned, primary)
        anim_resp = self.detect_animation_style(cleaned, primary)
        payload = {
            "primary_mood": primary,
            "secondary_mood": secondary,
            "confidence": confidence,
            "all_moods": scores,
            "detected_sfx": list(sfx_resp.get("data", {}).get("sfx", [])),
            "recommended_transition": str(
                trans_resp.get("data", {}).get("transition", "crossfade")
            ),
            "recommended_animation": str(
                anim_resp.get("data", {}).get("animation", "ken_burns")
            ),
            "text_length": len(cleaned),
            "word_count": len(cleaned.split()),
        }
        return self.make_response(True, data=payload, duration_ms=_ms(started))

    def analyze_all_scenes(self, project_id: str) -> Dict[str, Any]:
        """Analyze every scene in a project and persist keyword_mood.

        Args:
            project_id: Project UUID.

        Returns:
            Standard response with analyzed count and per-scene results.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(
                False,
                error="keyword_analyzer is disabled",
                duration_ms=_ms(started),
            )
        if not project_id:
            return self.make_response(
                False, error="project_id is required", duration_ms=_ms(started)
            )
        scenes = self.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number ASC",
            (project_id,),
        )
        results: List[Dict[str, Any]] = []
        for scene in scenes:
            item = self._analyze_and_persist_scene(project_id, scene)
            if item is not None:
                results.append(item)
        return self.make_response(
            True,
            data={
                "analyzed": len(results),
                "scenes": results,
                "project_id": project_id,
            },
            duration_ms=_ms(started),
        )

    def _analyze_and_persist_scene(
        self,
        project_id: str,
        scene: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Analyze one scene, cache result, and update the scenes row."""
        scene_id = str(scene["id"])
        text = self._scene_dialogue_text(project_id, scene_id)
        cache_key = self._cache_key(project_id, scene_id, text)
        analysis = self._get_cached_analysis(cache_key)
        if analysis is None:
            response = self.analyze_scene_text(text)
            if not response["success"]:
                return None
            analysis = response["data"]
            self._set_cached_analysis(cache_key, analysis)
        mood = str(analysis.get("primary_mood") or "neutral")
        self._update_scene_mood(scene_id, mood, scene, analysis)
        return {
            "scene_id": scene_id,
            "scene_number": scene.get("scene_number"),
            "primary_mood": mood,
            "confidence": analysis.get("confidence", 0.0),
            "detected_sfx": analysis.get("detected_sfx", []),
        }

    def detect_sfx_keywords(self, text: str) -> Dict[str, Any]:
        """Detect SFX names triggered by keywords in text.

        Args:
            text: Input text.

        Returns:
            Response with data.sfx list of SFX names.
        """
        started = time.perf_counter()
        cleaned = (text or "").strip()
        if not cleaned:
            return self.make_response(True, {"sfx": []}, duration_ms=_ms(started))
        hits: List[Tuple[str, int]] = []
        for sfx_name, patterns in self._compiled_sfx.items():
            count = sum(1 for pattern in patterns if pattern.search(cleaned))
            if count:
                hits.append((sfx_name, count))
        hits.sort(key=lambda item: (-item[1], item[0]))
        return self.make_response(
            True,
            {"sfx": [name for name, _ in hits], "scores": dict(hits)},
            duration_ms=_ms(started),
        )

    def detect_transition_style(self, text: str, mood: str) -> Dict[str, Any]:
        """Suggest best transition based on content and mood.

        Args:
            text: Scene text.
            mood: Primary mood string.

        Returns:
            Response with data.transition standard name.
        """
        started = time.perf_counter()
        cleaned = (text or "").strip()
        mood_key = (mood or "neutral").lower()
        # Keyword hits override mood default when strong
        best_name = self._mood_to_transition.get(mood_key, "crossfade")
        best_score = 0
        for name, patterns in self._compiled_trans.items():
            score = sum(1 for pattern in patterns if pattern.search(cleaned))
            if score > best_score:
                best_score = score
                best_name = name
        if best_score == 0 and mood_key in self._mood_to_transition:
            best_name = self._mood_to_transition[mood_key]
        return self.make_response(
            True,
            {"transition": best_name, "score": best_score, "mood": mood_key},
            duration_ms=_ms(started),
        )

    def detect_animation_style(self, text: str, mood: str) -> Dict[str, Any]:
        """Suggest best animation based on content and mood.

        Args:
            text: Scene text.
            mood: Primary mood string.

        Returns:
            Response with data.animation standard name.
        """
        started = time.perf_counter()
        cleaned = (text or "").strip()
        mood_key = (mood or "neutral").lower()
        best_name = self._mood_to_animation.get(mood_key, "ken_burns")
        best_score = 0
        for name, patterns in self._compiled_anim.items():
            score = sum(1 for pattern in patterns if pattern.search(cleaned))
            if score > best_score:
                best_score = score
                best_name = name
        if best_score == 0 and mood_key in self._mood_to_animation:
            best_name = self._mood_to_animation[mood_key]
        return self.make_response(
            True,
            {"animation": best_name, "score": best_score, "mood": mood_key},
            duration_ms=_ms(started),
        )

    def list_supported_emotions(self) -> List[str]:
        """Return the 28 TTS emotions plus any extra map emotions.

        Returns:
            Sorted unique emotion names.
        """
        names = set(TTS_EMOTIONS) | set(self._emotion_keywords.keys())
        return sorted(names)

    def is_optional_module(self) -> bool:
        """Return True — keyword_analyzer may be disabled safely."""
        return True

    # ------------------------------------------------------------------
    # Scoring and persistence helpers
    # ------------------------------------------------------------------

    def _score_emotions(self, text: str) -> Dict[str, int]:
        """Return match counts for every known emotion."""
        scores: Dict[str, int] = {}
        for emotion, patterns in self._compiled_emotions.items():
            count = 0
            for pattern in patterns:
                count += len(pattern.findall(text))
            if count:
                scores[emotion] = count
        # Ensure all 28 TTS emotions appear (zero if no hits) for callers/tests
        for emotion in TTS_EMOTIONS:
            scores.setdefault(emotion, 0)
        return scores

    def _rank_moods(self, scores: Dict[str, int]) -> Tuple[str, str, float]:
        """Pick primary/secondary mood and a simple confidence score."""
        positive = [(name, count) for name, count in scores.items() if count > 0]
        if not positive:
            return "neutral", "neutral", 0.0
        positive.sort(key=lambda item: (-item[1], item[0]))
        primary, primary_count = positive[0]
        secondary = positive[1][0] if len(positive) > 1 else primary
        total = sum(count for _, count in positive)
        confidence = round(primary_count / total, 4) if total else 0.0
        return primary, secondary, confidence

    def _empty_analysis(self) -> Dict[str, Any]:
        """Return neutral analysis payload for empty input."""
        zeros = {emotion: 0 for emotion in TTS_EMOTIONS}
        return {
            "primary_mood": "neutral",
            "secondary_mood": "neutral",
            "confidence": 0.0,
            "all_moods": zeros,
            "detected_sfx": [],
            "recommended_transition": "crossfade",
            "recommended_animation": "ken_burns",
            "text_length": 0,
            "word_count": 0,
        }

    def _scene_dialogue_text(self, project_id: str, scene_id: str) -> str:
        """Join all dialogue lines for a scene into one text blob."""
        rows = self.db.db.fetch_all(
            "SELECT text_content FROM dialogue_lines "
            "WHERE project_id = ? AND scene_id = ? ORDER BY line_number ASC",
            (project_id, scene_id),
        )
        parts = [str(row.get("text_content") or "").strip() for row in rows]
        return " ".join(part for part in parts if part)

    def _update_scene_mood(
        self,
        scene_id: str,
        mood: str,
        scene: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> None:
        """Write keyword_mood and optional style recommendations to scenes."""
        animation = str(analysis.get("recommended_animation") or "")
        transition = str(analysis.get("recommended_transition") or "")
        current_anim = str(scene.get("animation_type") or "")
        current_trans = str(scene.get("transition_in") or "")
        # Only override default-ish values, never custom explicit choices beyond defaults set
        new_anim = current_anim
        new_trans = current_trans
        if (not current_anim or current_anim in ("ken_burns", "")) and animation:
            new_anim = animation
        if (not current_trans or current_trans in ("crossfade", "")) and transition:
            new_trans = transition
        self.db.db.execute(
            "UPDATE scenes SET keyword_mood = ?, animation_type = ?, "
            "transition_in = ?, updated_at = ? WHERE id = ?",
            (mood, new_anim, new_trans, utc_now_str(), scene_id),
        )

    def _cache_key(self, project_id: str, scene_id: str, text: str) -> str:
        """Build stable cache key for a scene text analysis."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"kw:{project_id}:{scene_id}:{digest}"

    def _get_cached_analysis(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Read analysis from CacheService if present."""
        try:
            data = self.cache.get_json(cache_key)
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _set_cached_analysis(self, cache_key: str, analysis: Dict[str, Any]) -> None:
        """Store analysis JSON in CacheService."""
        try:
            self.cache.set_json(cache_key, analysis, ttl_seconds=7 * 24 * 3600)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("Cache store failed: %s", exc)


def _ms(started: float) -> float:
    """Elapsed milliseconds helper."""
    return round((time.perf_counter() - started) * 1000.0, 3)
