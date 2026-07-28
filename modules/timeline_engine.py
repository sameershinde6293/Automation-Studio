"""Timeline engine: build scene timing, chapters, intro/outro, validation.

Required BaseModule. Computes exact start/end times for every scene, syncs
to narration audio duration, and persists timeline_data for export engines.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.narration_pacing import (
    LEGACY_PAUSE_SECONDS,
    plan_narration_pauses,
    resolve_pacing_config,
)
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "timeline_engine"

# Transition overlap defaults (seconds)
TRANSITION_OVERLAP: Dict[str, float] = {
    "crossfade": 1.0,
    "dissolve": 0.5,
    "fade": 0.8,
    "dip_to_black": 1.0,
    "hard_cut": 0.0,
    "cut": 0.0,
    "slide_left": 0.7,
    "slide_right": 0.7,
}

DEFAULT_LINE_PAUSE = 0.35
SCENE_ENTRY_PADDING = 0.15
SCENE_EXIT_PADDING = 0.15
MIN_SCENE_DURATION = 1.0
MIN_CHAPTER_LENGTH = 30.0
MAX_CHAPTERS = 10
DEFAULT_INTRO_DURATION = 5.0
# BUGFIX (v3.1.1): was 5.0 here but 20.0 in intro_outro_engine.py (the module
# that actually renders the outro clip) — two sources of truth for the same
# constant. Matched to intro_outro_engine.py's value so both modules agree.
DEFAULT_OUTRO_DURATION = 20.0


class TimelineEngine(BaseModule):
    """Build and validate the complete video timeline for a project."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize timeline engine."""
        super().__init__(container, MODULE_NAME)

    def build_timeline(
        self,
        project_id: str,
        narration_path: Optional[str] = None,
        intro_config: Optional[Dict[str, Any]] = None,
        outro_config: Optional[Dict[str, Any]] = None,
        save: bool = True,
        line_pauses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Build complete timeline structure for a project.

        Args:
            project_id: Project UUID.
            narration_path: Optional narration WAV for exact alignment.
            intro_config: Optional intro settings {enabled, duration, type}.
            outro_config: Optional outro settings.
            save: Persist to timeline_data table when True.
            line_pauses: PHASE 6 (natural pauses & human pacing) —
                optional map of ``dialogue_lines.id`` -> the gap in
                seconds that FOLLOWS that line in the rendered narration.
                Supplied by the orchestrator so scene boundaries are
                computed from the gaps the audio actually used. Omitted
                (or missing an id) falls back to the equivalent planning
                done here, and finally to the flat legacy gap.

        Returns:
            Standard response with timeline dict.
        """
        started = time.perf_counter()
        if not project_id:
            return self._err("project_id is required", started)

        scenes = self.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number ASC",
            (project_id,),
        )
        if not scenes:
            timeline = self._empty_timeline(project_id)
            if save:
                self._save_timeline(project_id, timeline)
            return self.make_response(
                True,
                {"timeline": timeline, "empty": True},
                warnings=["No scenes in project"],
                duration_ms=_ms(started),
            )

        lines_by_scene = self._load_dialogue_by_scene(project_id)
        timed_scenes = self.calculate_scene_durations(
            scenes, lines_by_scene, line_pauses=line_pauses
        )
        narration_duration = self._narration_duration(
            project_id, narration_path, timed_scenes
        )
        timed_scenes = self.align_scenes_to_audio(timed_scenes, narration_duration)
        timed_scenes = self._compensate_for_crossfade_shrink(timed_scenes)
        timeline_scenes = self._apply_transitions_and_offsets(timed_scenes)

        intro_cfg = intro_config or {
            "enabled": False,
            "duration": DEFAULT_INTRO_DURATION,
        }
        outro_cfg = outro_config or {
            "enabled": False,
            "duration": DEFAULT_OUTRO_DURATION,
        }
        if intro_cfg.get("enabled", False):
            timeline_scenes = self.insert_intro_scene(timeline_scenes, intro_cfg)
        if outro_cfg.get("enabled", False):
            timeline_scenes = self.insert_outro_scene(timeline_scenes, outro_cfg)

        chapters = self.generate_chapter_markers(timeline_scenes)
        chapters_text = self.export_youtube_chapters_text(chapters)
        total = self._total_duration(timeline_scenes, intro_cfg, outro_cfg)

        timeline = {
            "project_id": project_id,
            "total_duration": round(total, 3),
            "intro": self._intro_block(intro_cfg),
            "scenes": [s for s in timeline_scenes if s.get("type") == "scene"],
            "outro": self._outro_block(outro_cfg, total),
            "chapters": chapters,
            "youtube_chapters_text": chapters_text,
            "narration_duration": round(narration_duration, 3),
            "content_duration": round(
                sum(
                    float(s.get("duration") or 0)
                    for s in timeline_scenes
                    if s.get("type") == "scene"
                ),
                3,
            ),
            "all_items": timeline_scenes,
        }

        validation = self.validate_timeline(timeline)
        timeline["is_valid"] = bool(validation["data"].get("valid"))
        timeline["validation_issues"] = validation["data"].get("issues", [])

        if save:
            self._save_timeline(project_id, timeline)
            self._update_scene_times(timeline_scenes)

        return self.make_response(
            True,
            {
                "timeline": timeline,
                "validation": validation["data"],
            },
            warnings=list(validation["data"].get("issues") or []),
            duration_ms=_ms(started),
        )

    def calculate_scene_durations(
        self,
        scenes: Sequence[Dict[str, Any]],
        narration_lines: Dict[str, List[Dict[str, Any]]],
        line_pauses: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Compute raw duration per scene from dialogue audio + pauses.

        BUGFIX (v3.1.7): this previously used SCENE_ENTRY_PADDING/EXIT_PADDING
        (0.15s each) and a semantic pause-label lookup (defaulting to 0.4s)
        that does NOT match the flat 0.25s gap actually used when the real
        narration audio is built (core_engine._PAUSE_BETWEEN_LINES). It also
        never accounted for the pause between one scene's last line and the
        next scene's first line. Each of these made a scene's calculated
        duration diverge from where that scene's dialogue actually falls in
        the real audio — a small difference per scene that COMPOUNDS across
        scenes, confirmed via real renders (drift grows scene by scene, not
        just at the start). We now build every scene's duration using the
        exact same inter-line pause used to build the real audio, across
        the WHOLE narration (not reset per scene), so scene boundaries land
        exactly where the real audio actually transitions between scenes.

        PHASE 6 (natural pauses & human pacing): inter-line gaps are no
        longer a flat constant — they vary with punctuation, emotion,
        speaker/scene changes and breathing need. The invariant above is
        unchanged and now MORE important: this must use the very same
        per-line gaps the narration WAV was assembled with. Three tiers,
        in order:

          1. ``line_pauses`` — the authoritative map the orchestrator
             captured while rendering. Exact by construction.
          2. ``core.narration_pacing.plan_narration_pauses`` recomputed
             from these rows (deterministic — same inputs, same output),
             for callers that build a timeline without having rendered
             in the same process (UI preview, a re-run of this stage).
          3. The flat legacy gap, if planning is unavailable/disabled.

        Args:
            scenes: Scene rows, in scene_number order.
            narration_lines: ``scene_id`` -> its dialogue rows, in order.
            line_pauses: Optional ``dialogue_lines.id`` -> gap seconds.

        Returns:
            One dict per scene with ``duration``/``calculated_duration``.
        """
        # Flatten every line across every scene, in order, so we can place
        # each scene's boundary at its real cumulative position in the
        # continuous narration track (including the gap that leads into the
        # NEXT scene's first line, which previously wasn't counted at all).
        flat: List[Tuple[str, Dict[str, Any]]] = []
        for scene in scenes:
            scene_id = str(scene["id"])
            for line in narration_lines.get(scene_id, []):
                flat.append((scene_id, line))

        gaps = self._resolve_line_pauses(flat, line_pauses)

        durations_by_scene: Dict[str, float] = {}
        for i, (scene_id, line) in enumerate(flat):
            durations_by_scene.setdefault(scene_id, 0.0)
            durations_by_scene[scene_id] += float(line.get("audio_duration") or 0.0)
            if i < len(flat) - 1:
                durations_by_scene[scene_id] += gaps[i]

        result: List[Dict[str, Any]] = []
        for scene in scenes:
            scene_id = str(scene["id"])
            lines = narration_lines.get(scene_id, [])
            speech = durations_by_scene.get(scene_id, 0.0)
            if speech <= 0:
                # Fallback: estimate from text length (~0.35s/word) — no
                # real audio to sync to yet, best effort only.
                words = sum(
                    len(str(line.get("text_content") or "").split())
                    for line in lines
                )
                speech = max(MIN_SCENE_DURATION, words * 0.35)
            duration = max(MIN_SCENE_DURATION, speech)
            item = dict(scene)
            item["calculated_duration"] = round(duration, 3)
            item["duration"] = round(duration, 3)
            item["narration_lines"] = lines
            item["type"] = "scene"
            result.append(item)
        return result

    def _resolve_line_pauses(
        self,
        flat: Sequence[Tuple[str, Dict[str, Any]]],
        line_pauses: Optional[Dict[str, float]],
    ) -> List[float]:
        """Resolve the gap that follows every narration line, in order.

        PHASE 6 (natural pauses & human pacing): see
        calculate_scene_durations for the three-tier contract. Never
        raises — any failure degrades to the flat legacy gap, which is
        exactly the pre-Phase-6 behavior.
        """
        count = len(flat)
        if count == 0:
            return []

        resolved: List[Optional[float]] = [None] * count
        if line_pauses:
            for index, (_scene_id, line) in enumerate(flat):
                key = str(line.get("id") or "")
                if key and key in line_pauses:
                    try:
                        resolved[index] = float(line_pauses[key])
                    except (TypeError, ValueError):
                        resolved[index] = None
        if all(value is not None for value in resolved):
            return [float(value) for value in resolved]

        # Tier 2: recompute deterministically from these same rows.
        planned: List[float] = []
        try:
            config = resolve_pacing_config(self.config)
            planned = plan_narration_pauses(
                [
                    {
                        "text": line.get("text_content") or "",
                        "character": line.get("character_name") or "",
                        "emotion": line.get("emotion") or "neutral",
                        "pause_after": line.get("pause_after") or "",
                        "scene_id": scene_id,
                        "duration": line.get("audio_duration"),
                    }
                    for scene_id, line in flat
                ],
                config,
            )
        except Exception as exc:  # noqa: BLE001 - timing must never crash
            self.log.warning(
                "Narration pause planning unavailable (%s) — using the "
                "flat %.2fs gap for scene durations", exc, LEGACY_PAUSE_SECONDS,
            )
            planned = []
        if len(planned) != count:
            planned = [LEGACY_PAUSE_SECONDS] * count

        return [
            float(resolved[index]) if resolved[index] is not None else planned[index]
            for index in range(count)
        ]

    def _compensate_for_crossfade_shrink(
        self, scenes: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Pad each scene's rendered duration to offset xfade join shrinkage.

        BUGFIX (v3.1.9): each scene's duration up to this point exactly
        matches its real narration span (fixed in 3.1.7/3.1.8). But when
        export_engine joins clips with a crossfade transition (0.8s default),
        every transition REMOVES 0.8s from the combined video's total length
        — audio is not shortened the same way, so the video runs increasingly
        ahead of the real narration with every extra scene. Confirmed via
        real render math: scene 2 arrived 0.8s early, scene 3 arrived 1.6s
        early — exactly (scene_index) x crossfade_duration, compounding
        linearly. Padding every scene's rendered duration by the crossfade
        duration cancels this out exactly (verified algebraically: with the
        padding, each scene's "fully visible" point in the final joined
        video lands back at its real narration start time, for every scene,
        not just the first one).

        Must match export_engine.DEFAULT_TRANSITION["duration"] — same
        duplicated-constant situation as the outro/pause fixes; modules
        don't cross-import (Rule A), so this is kept in sync deliberately.
        """
        crossfade_duration = 0.8
        result = []
        for scene in scenes:
            item = dict(scene)
            if item.get("type", "scene") == "scene":
                item["duration"] = round(
                    float(item.get("duration") or 0.0) + crossfade_duration, 3
                )
            result.append(item)
        return result

    def align_scenes_to_audio(
        self,
        scenes: Sequence[Dict[str, Any]],
        narration_duration: float,
    ) -> List[Dict[str, Any]]:
        """Scale scene durations so content total matches narration length."""
        items = [dict(s) for s in scenes]
        if not items:
            return items
        total = sum(float(s.get("duration") or 0) for s in items)
        target = float(narration_duration or 0)
        if target <= 0 or total <= 0:
            return items
        # Only scale if mismatch > 50ms
        if abs(total - target) < 0.05:
            return items
        scale = target / total
        for item in items:
            item["duration"] = max(
                MIN_SCENE_DURATION * 0.5, float(item["duration"]) * scale
            )
            item["duration"] = round(float(item["duration"]), 3)
            item["aligned"] = True
            item["scale_factor"] = round(scale, 6)
        # Fix residual rounding on last scene
        new_total = sum(float(s["duration"]) for s in items)
        residual = target - new_total
        items[-1]["duration"] = round(float(items[-1]["duration"]) + residual, 3)
        return items

    def calculate_transition_overlap(
        self,
        scene_a: Dict[str, Any],
        scene_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return overlap seconds based on transition between two scenes."""
        started = time.perf_counter()
        t_out = str(scene_a.get("transition_out") or "crossfade").lower()
        t_in = str(scene_b.get("transition_in") or t_out).lower()
        # Prefer outgoing transition type of scene A
        overlap = TRANSITION_OVERLAP.get(t_out, TRANSITION_OVERLAP.get(t_in, 0.5))
        # Explicit duration fields if present
        explicit = scene_a.get("transition_duration")
        if explicit is not None:
            try:
                overlap = float(explicit)
            except (TypeError, ValueError):
                pass
        return self.make_response(
            True,
            {
                "overlap": float(overlap),
                "transition_out": t_out,
                "transition_in": t_in,
            },
            duration_ms=_ms(started),
        )

    def generate_chapter_markers(
        self, scenes: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Create chapter markers from mood groups / chapter flags."""
        content = [s for s in scenes if s.get("type", "scene") == "scene"]
        if not content:
            return [{"time": 0.0, "title": "Introduction", "scene_id": None}]

        chapters: List[Dict[str, Any]] = []
        # Always start with first scene
        first = content[0]
        chapters.append(
            {
                "time": round(float(first.get("start_time") or 0.0), 3),
                "title": self._chapter_title(first, "Introduction"),
                "scene_id": first.get("id") or first.get("scene_id"),
            }
        )

        current_mood = str(first.get("keyword_mood") or "neutral")
        last_chapter_time = float(chapters[0]["time"])

        for scene in content[1:]:
            start = float(scene.get("start_time") or 0.0)
            mood = str(scene.get("keyword_mood") or "neutral")
            is_chapter = bool(scene.get("is_chapter_start"))
            boundary = is_chapter or (mood != current_mood)
            if boundary and (start - last_chapter_time) >= MIN_CHAPTER_LENGTH:
                chapters.append(
                    {
                        "time": round(start, 3),
                        "title": self._chapter_title(
                            scene, mood.replace("_", " ").title()
                        ),
                        "scene_id": scene.get("id") or scene.get("scene_id"),
                    }
                )
                last_chapter_time = start
                current_mood = mood
            elif mood != current_mood:
                current_mood = mood

        # Cap chapters
        if len(chapters) > MAX_CHAPTERS:
            # Keep first, last, and evenly sample middle
            keep = [chapters[0]]
            middle = chapters[1:-1]
            step = max(1, len(middle) // (MAX_CHAPTERS - 2))
            keep.extend(middle[::step][: MAX_CHAPTERS - 2])
            if chapters[-1] not in keep:
                keep.append(chapters[-1])
            chapters = keep[:MAX_CHAPTERS]
        return chapters

    def export_youtube_chapters_text(self, chapters: Sequence[Dict[str, Any]]) -> str:
        """Format chapters for YouTube description paste."""
        lines: List[str] = []
        for chapter in chapters:
            time_s = float(chapter.get("time") or 0.0)
            title = str(chapter.get("title") or "Chapter")
            lines.append(f"{self._format_timestamp(time_s)} {title}")
        return "\n".join(lines)

    def insert_intro_scene(
        self,
        timeline_scenes: Sequence[Dict[str, Any]],
        intro_config: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Prepend intro and shift all subsequent timestamps."""
        duration = float(intro_config.get("duration") or DEFAULT_INTRO_DURATION)
        items = [dict(s) for s in timeline_scenes]
        for item in items:
            if item.get("start_time") is not None:
                item["start_time"] = round(float(item["start_time"]) + duration, 3)
            if item.get("end_time") is not None:
                item["end_time"] = round(float(item["end_time"]) + duration, 3)
        intro = {
            "type": "intro",
            "start_time": 0.0,
            "end_time": round(duration, 3),
            "duration": round(duration, 3),
            "transition_out": {
                "type": str(intro_config.get("transition") or "fade"),
                "duration": float(intro_config.get("transition_duration") or 1.0),
            },
        }
        return [intro] + items

    def insert_outro_scene(
        self,
        timeline_scenes: Sequence[Dict[str, Any]],
        outro_config: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Append outro after the last content item."""
        duration = float(outro_config.get("duration") or DEFAULT_OUTRO_DURATION)
        items = [dict(s) for s in timeline_scenes]
        last_end = 0.0
        for item in items:
            last_end = max(last_end, float(item.get("end_time") or 0.0))
        outro = {
            "type": "outro",
            "start_time": round(last_end, 3),
            "end_time": round(last_end + duration, 3),
            "duration": round(duration, 3),
            "transition_in": {
                "type": str(outro_config.get("transition") or "fade"),
                "duration": float(outro_config.get("transition_duration") or 1.0),
            },
        }
        return items + [outro]

    def validate_timeline(self, timeline: Dict[str, Any]) -> Dict[str, Any]:
        """Validate timeline for gaps, negatives, and duration mismatches."""
        started = time.perf_counter()
        issues: List[str] = []
        scenes = list(timeline.get("scenes") or [])
        items = list(timeline.get("all_items") or scenes)

        for item in items:
            dur = float(item.get("duration") or 0)
            if dur < 0:
                issues.append(
                    f"Negative duration on item {item.get('id') or item.get('type')}"
                )
            start = float(item.get("start_time") or 0)
            end = float(item.get("end_time") or 0)
            if end + 1e-6 < start:
                issues.append(
                    f"end_time < start_time for {item.get('id') or item.get('type')}"
                )

        content = [s for s in items if s.get("type", "scene") == "scene"]
        for index in range(len(content) - 1):
            a = content[index]
            b = content[index + 1]
            a_end = float(a.get("end_time") or 0)
            b_start = float(b.get("start_time") or 0)
            overlap = self.calculate_transition_overlap(a, b)["data"]["overlap"]
            # With overlap, b_start should be near a_end - overlap
            expected = a_end - float(overlap)
            gap = b_start - a_end
            if gap > 0.05:
                issues.append(
                    f"Gap {gap:.2f}s between scene {a.get('scene_number')} and {b.get('scene_number')}"
                )
            # Unexpected large reverse overlap beyond transition
            if b_start < expected - 0.25:
                issues.append(
                    f"Excessive overlap between scenes {a.get('scene_number')} and {b.get('scene_number')}"
                )

        narr = float(timeline.get("narration_duration") or 0)
        content_dur = float(timeline.get("content_duration") or 0)
        if narr > 0 and abs(content_dur - narr) > 0.5:
            issues.append(
                f"Content duration {content_dur:.2f}s differs from narration {narr:.2f}s"
            )

        return self.make_response(
            True,
            {"valid": len(issues) == 0, "issues": issues},
            duration_ms=_ms(started),
        )

    def get_scene_at_time(self, project_id: str, time_seconds: float) -> Dict[str, Any]:
        """Return the scene active at the given timeline time."""
        started = time.perf_counter()
        timeline = self._load_timeline(project_id)
        if not timeline:
            # Build lightly without intro/outro if missing
            built = self.build_timeline(project_id, save=False)
            timeline = built.get("data", {}).get("timeline")
        if not timeline:
            return self._err("Timeline not found", started)
        t = float(time_seconds)
        for scene in timeline.get("scenes") or []:
            start = float(scene.get("start_time") or 0)
            end = float(scene.get("end_time") or 0)
            if start <= t < end or (
                abs(t - end) < 1e-6 and scene is (timeline.get("scenes") or [])[-1]
            ):
                return self.make_response(
                    True, {"scene": scene, "time": t}, duration_ms=_ms(started)
                )
        # Intro/outro checks
        intro = timeline.get("intro") or {}
        if intro and float(intro.get("start", 0)) <= t < float(intro.get("end", 0)):
            return self.make_response(
                True, {"scene": intro, "time": t}, duration_ms=_ms(started)
            )
        outro = timeline.get("outro") or {}
        if outro and float(outro.get("start", 0)) <= t <= float(outro.get("end", 0)):
            return self.make_response(
                True, {"scene": outro, "time": t}, duration_ms=_ms(started)
            )
        return self.make_response(
            True,
            {"scene": None, "time": t},
            warnings=["No scene at requested time"],
            duration_ms=_ms(started),
        )

    def get_scene_boundaries(self, project_id: str) -> Dict[str, Any]:
        """Return (start, end) boundaries for content scenes."""
        started = time.perf_counter()
        timeline = self._load_timeline(project_id)
        if not timeline:
            built = self.build_timeline(project_id, save=True)
            timeline = built.get("data", {}).get("timeline")
        boundaries = []
        for scene in (timeline or {}).get("scenes") or []:
            boundaries.append(
                {
                    "scene_id": scene.get("id") or scene.get("scene_id"),
                    "scene_number": scene.get("scene_number"),
                    "start": float(scene.get("start_time") or 0),
                    "end": float(scene.get("end_time") or 0),
                }
            )
        return self.make_response(
            True,
            {"boundaries": boundaries, "count": len(boundaries)},
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_dialogue_by_scene(
        self, project_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group dialogue lines by scene_id."""
        rows = self.db.db.fetch_all(
            "SELECT * FROM dialogue_lines WHERE project_id = ? "
            "ORDER BY scene_id ASC, line_number ASC",
            (project_id,),
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            sid = str(row.get("scene_id"))
            grouped.setdefault(sid, []).append(row)
        return grouped

    def _narration_duration(
        self,
        project_id: str,
        narration_path: Optional[str],
        timed_scenes: Sequence[Dict[str, Any]],
    ) -> float:
        """Resolve narration duration from file, audio_tracks, or scene sum."""
        if narration_path and Path(narration_path).exists():
            return self._wav_duration(Path(narration_path))
        row = self.db.db.fetch_one(
            "SELECT duration_seconds FROM audio_tracks "
            "WHERE project_id = ? AND track_type = 'narration' "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        if row and float(row.get("duration_seconds") or 0) > 0:
            return float(row["duration_seconds"])
        return sum(float(s.get("duration") or 0) for s in timed_scenes)

    def _apply_transitions_and_offsets(
        self, scenes: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Assign start/end times with transition overlaps between scenes."""
        items: List[Dict[str, Any]] = []
        cursor = 0.0
        previous: Optional[Dict[str, Any]] = None
        for scene in scenes:
            item = dict(scene)
            overlap = 0.0
            if previous is not None:
                overlap = float(
                    self.calculate_transition_overlap(previous, item)["data"]["overlap"]
                )
                cursor = max(0.0, cursor - overlap)
            duration = float(item.get("duration") or MIN_SCENE_DURATION)
            item["start_time"] = round(cursor, 3)
            item["end_time"] = round(cursor + duration, 3)
            item["duration"] = round(duration, 3)
            item["transition_in"] = {
                "type": str(item.get("transition_in") or "crossfade"),
                "duration": (
                    overlap
                    if previous is not None
                    else float(item.get("transition_duration") or 0.0)
                ),
            }
            item["transition_out"] = {
                "type": str(item.get("transition_out") or "crossfade"),
                "duration": float(
                    item.get("transition_duration")
                    or TRANSITION_OVERLAP.get(
                        str(item.get("transition_out") or "crossfade"), 0.5
                    )
                ),
            }
            item["scene_id"] = item.get("id")
            item["image_path"] = item.get("image_file_path") or item.get(
                "image_filename"
            )
            items.append(item)
            cursor = float(item["end_time"])
            previous = item
        return items

    def _chapter_title(self, scene: Dict[str, Any], fallback: str) -> str:
        """Best-effort chapter title from scene fields."""
        for key in ("chapter_title", "scene_title", "title"):
            value = scene.get(key)
            if value:
                return str(value)
        image = str(scene.get("image_filename") or scene.get("image_path") or "")
        if image:
            stem = Path(image).stem.replace("_", " ").strip()
            if stem:
                return stem.title()
        mood = str(scene.get("keyword_mood") or "").replace("_", " ").strip()
        if mood:
            return mood.title()
        return fallback

    def _format_timestamp(self, seconds: float) -> str:
        """Format seconds as M:SS or H:MM:SS for YouTube."""
        total = max(0, int(seconds))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _pause_seconds(self, pause_name: str) -> float:
        """Map pause_after labels to seconds."""
        mapping = {
            "none": 0.0,
            "micro": 0.2,
            "short": 0.4,
            "medium": 0.85,
            "long": 1.75,
            "dramatic": 3.0,
        }
        return mapping.get(str(pause_name).lower(), DEFAULT_LINE_PAUSE)

    def _empty_timeline(self, project_id: str) -> Dict[str, Any]:
        """Empty timeline structure."""
        return {
            "project_id": project_id,
            "total_duration": 0.0,
            "intro": None,
            "scenes": [],
            "outro": None,
            "chapters": [],
            "youtube_chapters_text": "",
            "narration_duration": 0.0,
            "content_duration": 0.0,
            "all_items": [],
            "is_valid": True,
            "validation_issues": [],
        }

    def _intro_block(self, intro_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Intro summary block for timeline structure."""
        if not intro_cfg.get("enabled"):
            return None
        duration = float(intro_cfg.get("duration") or DEFAULT_INTRO_DURATION)
        return {"start": 0.0, "end": duration, "type": "intro", "duration": duration}

    def _outro_block(
        self, outro_cfg: Dict[str, Any], total: float
    ) -> Optional[Dict[str, Any]]:
        """Outro summary block for timeline structure."""
        if not outro_cfg.get("enabled"):
            return None
        duration = float(outro_cfg.get("duration") or DEFAULT_OUTRO_DURATION)
        start = max(0.0, total - duration)
        return {"start": start, "end": total, "type": "outro", "duration": duration}

    def _total_duration(
        self,
        items: Sequence[Dict[str, Any]],
        intro_cfg: Dict[str, Any],
        outro_cfg: Dict[str, Any],
    ) -> float:
        """Compute full timeline duration including intro/outro if present."""
        if not items:
            total = 0.0
        else:
            total = max(float(i.get("end_time") or 0) for i in items)
        return total

    def _save_timeline(self, project_id: str, timeline: Dict[str, Any]) -> None:
        """Upsert timeline_data row."""
        now = utc_now_str()
        existing = self.db.db.fetch_one(
            "SELECT id FROM timeline_data WHERE project_id = ?", (project_id,)
        )
        chapters = timeline.get("chapters") or []
        payload = (
            round(float(timeline.get("total_duration") or 0), 3),
            round(float((timeline.get("intro") or {}).get("duration") or 0), 3),
            round(float((timeline.get("outro") or {}).get("duration") or 0), 3),
            round(float(timeline.get("content_duration") or 0), 3),
            round(float(timeline.get("narration_duration") or 0), 3),
            json.dumps(chapters),
            str(timeline.get("youtube_chapters_text") or ""),
            json.dumps(timeline),
            1 if timeline.get("is_valid") else 0,
            json.dumps(timeline.get("validation_issues") or []),
            now,
        )
        if existing:
            self.db.db.execute(
                """
                UPDATE timeline_data SET
                    total_duration=?, intro_duration=?, outro_duration=?,
                    content_duration=?, narration_duration=?,
                    chapter_markers_json=?, youtube_chapters_text=?,
                    timeline_json=?, is_valid=?, validation_errors_json=?,
                    updated_at=?
                WHERE project_id=?
                """,
                payload + (project_id,),
            )
        else:
            self.db.db.execute(
                """
                INSERT INTO timeline_data (
                    id, project_id, total_duration, intro_duration, outro_duration,
                    content_duration, narration_duration, chapter_markers_json,
                    youtube_chapters_text, timeline_json, is_valid,
                    validation_errors_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (self.db.new_id(), project_id) + payload[:-1] + (now, now),
            )

    def _load_timeline(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Load timeline JSON from database."""
        row = self.db.db.fetch_one(
            "SELECT timeline_json FROM timeline_data WHERE project_id = ?",
            (project_id,),
        )
        if not row or not row.get("timeline_json"):
            return None
        try:
            data = json.loads(row["timeline_json"])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    def _update_scene_times(self, items: Sequence[Dict[str, Any]]) -> None:
        """Persist computed start/end/duration onto scenes table."""
        now = utc_now_str()
        for item in items:
            if item.get("type", "scene") != "scene":
                continue
            scene_id = item.get("id") or item.get("scene_id")
            if not scene_id:
                continue
            self.db.db.execute(
                "UPDATE scenes SET start_time = ?, end_time = ?, duration = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    float(item.get("start_time") or 0),
                    float(item.get("end_time") or 0),
                    float(item.get("duration") or 0),
                    now,
                    scene_id,
                ),
            )

    def _wav_duration(self, path: Path) -> float:
        """Read WAV duration; 0 if unreadable."""
        try:
            import wave

            with wave.open(str(path), "r") as handle:
                return handle.getnframes() / float(handle.getframerate())
        except Exception:  # noqa: BLE001
            return 0.0

    def _err(self, message: str, started: float) -> Dict[str, Any]:
        """Error response helper."""
        return self.make_response(
            False,
            data={
                "error_code": "TIMELINE_ERROR",
                "user_message": message,
                "is_recoverable": True,
            },
            error=message,
            duration_ms=_ms(started),
        )


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)
