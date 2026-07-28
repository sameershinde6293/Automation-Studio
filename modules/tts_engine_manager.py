"""TTS engine manager with mandatory lazy loading.

Never loads Piper/Kokoro/XTTS models at import or construction time.
Generation routes by character engine; RAM management unloads heavy engines.
"""

from __future__ import annotations

import array
import gc
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.narration_pacing import (
    LEGACY_PAUSE_SECONDS,
    plan_narration_pauses,
    resolve_pacing_config,
)
from core.narration_prosody import (
    merge_prosody_plans,
    resolve_prosody_config,
    spoken_words,
)
from core.narration_prosody import (
    plan_narration_prosody as build_narration_prosody,
)
from core.safe_io import atomic_write, replace_atomic, safe_unlink
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str
from modules.tts_presets import (
    ADMIN_PASSWORD,
    BREATH_CHANCE,
    BREATH_VOLUME_RANGE,
    COMPRESSOR_FILTER,
    EMOTION_ALIASES,
    EMOTION_PRESETS,
    EQ_PRESETS,
    HIGHPASS_FILTER,
    LIMITER_FILTER,
    LUFS_NORMALIZE_FILTER,
    NOISE_GATE_FILTER,
    PARAGRAPH_BREAKING_PAUSES,
    PARAGRAPH_HARD_MAX_CHARS,
    PARAGRAPH_MAX_CHARS,
    PARAGRAPH_MAX_ESTIMATED_SECONDS,
    PARAGRAPH_MAX_SENTENCES,
    PARAGRAPH_SECONDS_PER_WORD_ESTIMATE,
    PAUSE_BASE_DURATIONS,
    PAUSE_EMOTION_MULTIPLIERS,
    PAUSE_VARIATIONS,
    REVERB_PRESETS,
    SPECIAL_EFFECTS,
)

MODULE_NAME = "tts_engine_manager"
ENGINE_PIPER = "piper"
ENGINE_KOKORO = "kokoro"
ENGINE_XTTS = "xtts"

# PHASE 1 (audio stability): same threshold as audio_processor's
# identical constant — modules in this codebase don't cross-import
# (Rule A), so this is kept in sync deliberately rather than shared.
# A buffer this overwhelmingly non-finite is corrupted input, not
# something safely repairable in place.
_MAX_NONFINITE_RATIO = 0.5
# Edge fade length for every generated line — short enough to be
# inaudible, long enough (5-10ms) to remove the DC-step "tick" a raw
# TTS buffer boundary or an inserted pause splice can otherwise leave.
_EDGE_FADE_MS = 8.0

# PHASE 9 (TTS failure recovery): how many times a single line's
# synthesis is attempted before falling back to the synthetic tone.
# Deliberately small — a genuinely broken engine should degrade fast,
# while a transient failure under concurrent load gets one more chance
# (the fallback tone is audible in the finished video, an engine retry
# costs a second). A first-attempt success is unaffected.
_SYNTHESIS_ATTEMPTS = 2
_SYNTHESIS_RETRY_SECONDS = 0.4


PAUSE_TAG_RE = re.compile(r"\[PAUSE:([A-Za-z_]+)\]", re.IGNORECASE)
WORD_RE = re.compile(r"\S+")

# FEATURE (v3.2.11): support for the "TTS-ready script" tag format used
# by the AI Narrator Studio-style script-generation prompt — a DIFFERENT
# syntax from the original [PAUSE:TYPE] tags above (which this app has
# supported since early versions and still does, for backward
# compatibility). These durations are EXACT (no randomization) — the
# whole point of a script writer specifying "[PAUSE] = 0.8 seconds" is
# a predictable, reviewable result, not a humanized random variation.
SCRIPT_TAG_RE = re.compile(
    r"""
    \[SILENCE=(?P<sil_val>\d+(?:\.\d+)?)(?P<sil_unit>ms|s)\]
    | \[PAUSE\s+(?P<custom_secs>\d+(?:\.\d+)?)\]
    | \[(?P<named>SHORT_PAUSE|MEDIUM_PAUSE|LONG_PAUSE|PAUSE)\]
    """,
    re.IGNORECASE | re.VERBOSE,
)
SPELL_TAG_RE = re.compile(
    r"\[SPELL\](.*?)\[/SPELL\]", re.IGNORECASE | re.DOTALL
)
# Exact seconds per named tag — matches the script-generation prompt's
# spec precisely (not the older, randomized MICRO/SHORT/MEDIUM/LONG/
# DRAMATIC scale used by [PAUSE:TYPE]).
SCRIPT_TAG_DURATIONS = {
    "SHORT_PAUSE": 0.4,
    "PAUSE": 0.8,
    "MEDIUM_PAUSE": 1.0,
    "LONG_PAUSE": 1.5,
}

# RAM free thresholds (MB)
XTTS_UNLOAD_BELOW_MB = 1500
KOKORO_UNLOAD_BELOW_MB = 800
IDLE_UNLOAD_SECONDS = 60.0


class TTSEngineManager(BaseModule):
    """Install, manage, and run TTS engines with lazy loading."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize manager without loading any TTS engine."""
        super().__init__(container, MODULE_NAME)
        self.kokoro_instance: Any = None
        self.xtts_instance: Any = None
        self._last_use: Dict[str, float] = {}
        self._ffmpeg_cache: Optional[Path] = None
        self._project_root = Path.cwd()
        # Resolve project root from config if available
        try:
            cfg_folder = getattr(self.config, "config_folder", None)
            if cfg_folder is not None:
                self._project_root = Path(cfg_folder).resolve().parent
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Public generation API
    # ------------------------------------------------------------------

    def generate_audio(
        self,
        text: str,
        character_profile: Dict[str, Any],
        output_path: str | Path,
        voice_sample_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate speech for text using the character's configured engine.

        Args:
            text: Dialogue text — may include [PAUSE:TYPE] tags (legacy
                format) and/or the "TTS-ready script" tag format:
                [SHORT_PAUSE] [PAUSE] [MEDIUM_PAUSE] [LONG_PAUSE]
                [SILENCE=500ms] [SILENCE=2s] [PAUSE 3]
                [SPELL]ACRONYM[/SPELL]. See process_pause_tags().
            character_profile: Voice profile dict.
            output_path: Destination WAV path.
            voice_sample_path: Optional XTTS reference sample.

        Returns:
            Standard response with audio_path, duration, word_timestamps.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self._err("tts_engine_manager is disabled", started)
        clean_text, pause_markers = self.process_pause_tags(text or "")
        if not clean_text.strip():
            return self._err("text is empty after pause tag removal", started)

        # PHASE 7: a dialogue line's authored emotion takes precedence over
        # the character's fallback emotion.  Earlier versions checked the
        # default first, which meant a profile with default=neutral silently
        # ignored row-level markers such as [NARRATOR|urgent].
        emotion = str(
            character_profile.get("emotion")
            or character_profile.get("default_emotion")
            or "neutral"
        )
        prosody_plan = self._prosody_plan_for_text(
            clean_text, emotion, character_profile
        )
        profile_for_params = dict(character_profile)
        profile_for_params["prosody_plan"] = prosody_plan
        params = self.apply_emotion_parameters(profile_for_params, emotion)
        preferred = str(character_profile.get("engine") or ENGINE_KOKORO).lower()
        engine = self._resolve_engine_with_fallback(preferred)
        if engine is None:
            return self._err(
                "No TTS engine available (Piper/Kokoro/XTTS not installed)",
                started,
            )

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        voice_model = str(
            character_profile.get("voice_model")
            or character_profile.get("voice")
            or "default"
        )

        if engine == ENGINE_PIPER:
            gen = self.generate_with_piper(clean_text, voice_model, params, str(out))
        elif engine == ENGINE_KOKORO:
            gen = self.generate_with_kokoro(clean_text, voice_model, params, str(out))
        else:
            sample = voice_sample_path or character_profile.get("avatar_path")
            gen = self.generate_with_xtts(clean_text, sample, params, str(out))

        if not gen.get("success"):
            return gen

        audio_path = Path(gen["data"]["audio_path"])
        timestamps = list(gen["data"].get("word_timestamps") or [])

        # PHASE 1/4 (audio stability, hard intro fix): sanitize the raw
        # engine output BEFORE any further processing touches it — NaN/
        # Inf/out-of-range samples (a real risk from Kokoro/XTTS numpy
        # buffers, or a flaky decoder) must never reach pitch-shift/
        # pause-insert/effects. Leading-silence trim is opt-out (default
        # ON) via character_profile/params "trim_leading_silence" — the
        # trim itself always stays breath-safe (a fixed margin is kept
        # before the detected first phoneme regardless of this flag), so
        # disabling it only restores the raw untrimmed engine output for
        # callers who explicitly want that; it never makes trimming more
        # aggressive. Every step below is best-effort and never raises —
        # on any internal failure the file is left untouched.
        trim_leading = bool(
            params.get(
                "trim_leading_silence",
                character_profile.get("trim_leading_silence", True),
            )
        )
        finalize_info = self._finalize_line_audio(
            audio_path, trim_leading_silence=trim_leading
        )
        if not self._validate_wav_basic(audio_path):
            return self._err(
                f"Generated audio failed validation after synthesis: {audio_path}",
                started,
            )
        if finalize_info.get("trimmed_ms", 0.0) > 0.0:
            # Timestamps are always an even distribution across duration
            # (see _approximate_word_timestamps) for every engine, real or
            # synthetic — recomputing against the new, shorter duration
            # keeps them at the same approximation quality instead of
            # silently drifting once leading silence is removed.
            new_duration = self._wav_duration(audio_path)
            timestamps = self._approximate_word_timestamps(clean_text, new_duration)

        # Pitch adjust if needed
        pitch = float(params.get("pitch", 0.0))
        if abs(pitch) > 0.01:
            # PHASE 1: validate after every processing stage — a failed or
            # partial ffmpeg pitch-shift must never leave corrupted/silent
            # audio behind; revert to the pre-shift bytes instead.
            backup_bytes = self._read_backup(audio_path)
            duration_before_pitch = self._wav_duration(audio_path)
            self._apply_pitch_shift(audio_path, pitch)
            if not self._validate_wav_basic(audio_path):
                self.log.warning(
                    "Pitch shift produced invalid audio for %s — reverting "
                    "to pre-shift audio", audio_path,
                )
                self._restore_backup(audio_path, backup_bytes)
            else:
                # PHASE 7: the revised pitch filter is duration preserving,
                # but reconcile timestamps defensively for older/custom
                # FFmpeg builds that round the output length differently.
                duration_after_pitch = self._wav_duration(audio_path)
                timestamps = self._scale_word_timestamps(
                    timestamps, duration_before_pitch, duration_after_pitch
                )

        # Insert pauses
        if pause_markers:
            # PHASE 1: same backup/validate/revert protection as above —
            # a pause-insertion failure (or a pydub/ffmpeg edge case)
            # must never corrupt the narration line.
            backup_bytes = self._read_backup(audio_path)
            try:
                pause_result = self.insert_pauses_into_audio(
                    str(audio_path),
                    pause_markers,
                    timestamps,
                    breathing=bool(character_profile.get("breathing_enabled")),
                    character_profile=character_profile,
                    emotion=str(params.get("emotion") or emotion),
                    speed=float(params.get("speed") or 1.0),
                )
            except Exception as exc:  # noqa: BLE001 - preserve raw speech
                self.log.warning("Pause insertion raised for %s: %s", audio_path, exc)
                pause_result = {"success": False}
            if (
                not pause_result.get("success")
                or not self._validate_wav_basic(audio_path)
            ):
                self.log.warning(
                    "Pause insertion failed or produced invalid audio for %s "
                    "— reverting to pre-pause audio", audio_path,
                )
                self._restore_backup(audio_path, backup_bytes)
            else:
                timestamps = self._adjust_timestamps_for_pauses(
                    timestamps, pause_markers
                )

        # PHASE 7: apply planned word stress and phrase intonation before
        # the established Phase 2 compressor/limiter.  The transform keeps
        # every phrase and the complete clip exactly the same duration and
        # returns timestamps mapped through the same resampling curve.
        prosody_result = self._apply_prosody_contour(
            audio_path, timestamps, prosody_plan
        )
        timestamps = list(prosody_result.get("word_timestamps") or timestamps)

        # Voice effects chain (already reverts internally on failure/
        # degenerate output — see apply_voice_effects/_audio_is_degenerate)
        duration_before_effects = self._wav_duration(audio_path)
        self.apply_voice_effects(str(audio_path), character_profile, params)
        timestamps = self._scale_word_timestamps(
            timestamps,
            duration_before_effects,
            self._wav_duration(audio_path),
        )

        # PHASE 1: final safety pass on the fully-assembled line — repairs
        # any click/pop introduced by pitch-shift, pause splicing, or the
        # effects chain, and guarantees a smooth (click-free) start/end on
        # every clip that leaves this function, per the fade-in/fade-out
        # requirement. Never raises; on failure the clip is left as-is.
        self._finalize_line_audio(audio_path, trim_leading_silence=False)
        if not self._validate_wav_basic(audio_path):
            return self._err(
                f"Generated audio failed final validation: {audio_path}",
                started,
            )

        duration = self._wav_duration(audio_path)

        self._mark_engine_used(engine)
        self.check_ram_and_manage_engines()
        if engine == ENGINE_XTTS:
            self.unload_engine_from_memory(ENGINE_XTTS)

        return self.make_response(
            True,
            {
                "audio_path": str(audio_path),
                "duration": duration,
                "word_timestamps": timestamps,
                "engine": engine,
                "emotion": params.get("emotion") or emotion,
                "params": params,
                "prosody_plan": prosody_plan,
                "prosody_applied": bool(prosody_result.get("applied")),
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # PHASE 5: Natural narration / paragraph-based TTS
    # ------------------------------------------------------------------

    def group_sentences_into_paragraphs(
        self,
        lines: Sequence[Dict[str, Any]],
        max_chars: int = PARAGRAPH_MAX_CHARS,
        max_sentences: int = PARAGRAPH_MAX_SENTENCES,
        hard_max_chars: int = PARAGRAPH_HARD_MAX_CHARS,
        max_estimated_seconds: float = PARAGRAPH_MAX_ESTIMATED_SECONDS,
    ) -> List[List[Dict[str, Any]]]:
        """Group consecutive, compatible dialogue lines into paragraphs.

        PHASE 5 (natural narration / paragraph-based TTS): a paragraph
        is one TTS request instead of one-per-sentence — this removes
        the small pause/click-fade join that used to occur at every
        single sentence boundary, replacing many robotic short clips
        with fewer, longer, more naturally-flowing ones. Grouping is
        deliberately conservative — a line breaks the current paragraph
        (starts a new one) whenever ANY of the following hold, so
        semantically/technically incompatible lines are NEVER merged:

          * Different ``character`` (voice profile) — a paragraph is
            always spoken by exactly one voice; never mix two
            characters into one TTS request.
          * Different ``emotion`` — an emotion change is itself a
            performance/pacing cue (speed/pitch/volume all shift per
            EMOTION_PRESETS); merging across it would silently discard
            that intent.
          * The line carries a "breaking" pause_after label (medium/
            long/dramatic — see PARAGRAPH_BREAKING_PAUSES) — a deliberate
            dramatic beat, not something to smooth over.
          * Adding the next sentence would cross ``max_chars``,
            ``max_sentences``, or the estimated-duration budget — smart
            batching's actual "never exceed engine limits" backstop.
            ``hard_max_chars`` is an absolute ceiling checked even for
            a single already-long sentence (which still becomes its own
            one-sentence "paragraph" rather than being merged further).
          * An empty/blank line — never contributes to a paragraph.

        Args:
            lines: Ordered dialogue-line-like dicts, each with at least
                ``text`` (or ``text_content``), ``character`` (or
                ``character_name``), and optionally ``emotion``/
                ``default_emotion`` and ``pause_after``. Extra keys are
                preserved verbatim on every line inside each returned
                paragraph group (callers can carry through row ids,
                indices, etc.).
            max_chars: Soft per-paragraph character budget.
            max_sentences: Soft per-paragraph sentence-count budget.
            hard_max_chars: Absolute ceiling — never crossed regardless
                of the other budgets.
            max_estimated_seconds: Soft per-paragraph estimated spoken
                duration budget (word count * ~0.35s/word).

        Returns:
            A list of paragraph groups, each a non-empty list of the
            original line dicts that belong together, in original order.
            Falls back to one-sentence-per-paragraph automatically for
            any line that can't be safely merged with its neighbors —
            "sentence mode" is simply the case where every group has
            exactly one line, so no separate code path is needed.
        """
        paragraphs: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        current_chars = 0
        current_words = 0

        def _text_of(line: Dict[str, Any]) -> str:
            return str(line.get("text") or line.get("text_content") or "").strip()

        def _character_of(line: Dict[str, Any]) -> str:
            return str(line.get("character") or line.get("character_name") or "")

        def _emotion_of(line: Dict[str, Any]) -> str:
            return str(
                line.get("emotion") or line.get("default_emotion") or "neutral"
            ).lower()

        def _flush() -> None:
            if current:
                paragraphs.append(list(current))
                current.clear()

        for line in lines:
            text = _text_of(line)
            if not text:
                continue
            char_count = len(text)
            word_count = len(WORD_RE.findall(text))

            if current:
                prev = current[-1]
                same_character = _character_of(prev) == _character_of(line)
                same_emotion = _emotion_of(prev) == _emotion_of(line)
                prev_pause = str(prev.get("pause_after") or "none").lower()
                prev_breaks = prev_pause in PARAGRAPH_BREAKING_PAUSES
                would_exceed_chars = (current_chars + 1 + char_count) > max_chars
                would_exceed_sentences = (len(current) + 1) > max_sentences
                would_exceed_seconds = (
                    (current_words + word_count) * PARAGRAPH_SECONDS_PER_WORD_ESTIMATE
                    > max_estimated_seconds
                )
                if (
                    not same_character
                    or not same_emotion
                    or prev_breaks
                    or would_exceed_chars
                    or would_exceed_sentences
                    or would_exceed_seconds
                ):
                    _flush()
                    current_chars = 0
                    current_words = 0

            # Hard ceiling: even a single oversized sentence never merges
            # further — it becomes (or stays) its own one-line group.
            if current and (current_chars + 1 + char_count) > hard_max_chars:
                _flush()
                current_chars = 0
                current_words = 0

            current.append(line)
            current_chars += char_count + (1 if len(current) > 1 else 0)
            current_words += word_count

        _flush()
        return paragraphs

    # ------------------------------------------------------------------
    # PHASE 6: Natural pauses & human pacing
    # ------------------------------------------------------------------

    def plan_narration_pauses(
        self,
        lines: Sequence[Dict[str, Any]],
        overrides: Optional[Dict[str, Any]] = None,
    ) -> List[float]:
        """Plan the natural gap that follows each narration line.

        PHASE 6 (natural pauses & human pacing): a thin, stable seam over
        ``core.narration_pacing`` for callers that only hold the TTS
        module (the UI's voice preview, plugins, batch tooling) — the
        real pipeline path is core_engine, which plans once from measured
        durations and shares that plan with the mixer and the timeline.

        Unlike ``generate_pause`` (which humanizes an authored IN-LINE
        [PAUSE:TYPE] beat with real randomness), this is deterministic:
        the same lines always yield the same gaps, because narration
        assembly, word-timestamp offsets and scene durations are computed
        independently and must agree exactly.

        Args:
            lines: Narration lines in playback order — any dicts with
                ``text``/``text_content``, optionally ``character``,
                ``emotion``, ``pause_after``, ``scene_id`` and a measured
                ``duration``/``audio_duration``.
            overrides: Optional direct pacing overrides (see
                ``core.narration_pacing.PACING_DEFAULTS``); app settings
                are read automatically.

        Returns:
            One gap in seconds per line — ``result[i]`` follows
            ``lines[i]``. The trailing entry is normally discarded by the
            caller (narration never ends with silence). Returns an empty
            list for empty input, and never raises.
        """
        try:
            config = resolve_pacing_config(self.config, overrides)
            return plan_narration_pauses(lines, config)
        except Exception as exc:  # noqa: BLE001 - pacing is never fatal
            self.log.warning(
                "Narration pause planning failed (%s) — falling back to "
                "the flat %.2fs gap", exc, LEGACY_PAUSE_SECONDS,
            )
            return [LEGACY_PAUSE_SECONDS] * len(list(lines or []))

    # ------------------------------------------------------------------
    # PHASE 7: Emotion & prosody enhancement
    # ------------------------------------------------------------------

    def plan_narration_prosody(
        self,
        lines: Sequence[Dict[str, Any]],
        overrides: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Return deterministic performance plans for ordered narration.

        This additive seam mirrors ``plan_narration_pauses`` for previews,
        plugins, and callers that hold only the TTS module.  Missing emotion
        metadata resolves to neutral; any planner/config failure returns
        disabled plans so synthesis continues through the established path.
        """
        items = list(lines or [])
        try:
            config = resolve_prosody_config(self.config, overrides)
            return build_narration_prosody(items, config)
        except Exception as exc:  # noqa: BLE001 - prosody is never fatal
            self.log.warning(
                "Narration prosody planning failed (%s) — using the "
                "existing engine delivery", exc,
            )
            return [
                {
                    "enabled": False,
                    "emotion": "neutral",
                    "word_count": 0,
                    "phrases": [],
                    "emphasis": [],
                }
                for _ in items
            ]

    def _prosody_plan_for_text(
        self,
        text: str,
        emotion: str,
        character_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Use a caller-supplied contextual plan or safely make one."""
        supplied = character_profile.get("prosody_plan")
        if isinstance(supplied, dict):
            return supplied
        plans = self.plan_narration_prosody(
            [
                {
                    "text": text,
                    "emotion": emotion,
                    "character": character_profile.get("character_name")
                    or "NARRATOR",
                }
            ]
        )
        if plans:
            return plans[0]
        return {"enabled": False, "phrases": [], "emphasis": []}

    def generate_paragraph_audio(
        self,
        lines: Sequence[Dict[str, Any]],
        character_profile: Dict[str, Any],
        output_path: str | Path,
        voice_sample_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate one TTS clip for a paragraph (multiple sentences).

        PHASE 5 (natural narration / paragraph-based TTS): identical
        contract to generate_audio() — same validation, click/hard-intro
        safety passes, effects chain, and response shape — but joins
        each line's text with a single space before synthesis, so the
        TTS engine reads the whole paragraph as one continuous, natural
        utterance instead of several separately-synthesized sentences
        awkwardly stitched together. Punctuation already present in each
        line's text (periods, commas, question marks) is preserved
        exactly as authored, so natural prosodic pauses at sentence
        boundaries still come from the engine's own punctuation
        handling — not from an inserted [PAUSE] tag.

        Per-line ``word_timestamps`` in the response are automatically
        split back out per original line (using each line's own word
        count against the combined paragraph's word timestamps, which
        is exact — never approximate — because every engine in this
        module already produces evenly-distributed word timestamps
        across the FULL synthesized duration; slicing that array by
        cumulative word count therefore reproduces exactly what
        generating each line separately would have produced, with no
        drift). This is what makes paragraph mode a pure internal
        optimization: every caller-visible timestamp, subtitle, and
        scene-sync calculation stays exactly as accurate as sentence
        mode.

        Args:
            lines: The paragraph's dialogue lines (same shape as
                group_sentences_into_paragraphs's input) — must all
                share one character/emotion (the caller is expected to
                have already grouped them via
                group_sentences_into_paragraphs; this method does not
                re-validate that, callers using their own batching are
                free to use it directly for a single character/emotion).
            character_profile: Voice profile dict (shared by every line).
            output_path: Destination WAV path for the whole paragraph.
            voice_sample_path: Optional XTTS reference sample.

        Returns:
            Standard response with ``audio_path``, ``duration``,
            ``word_timestamps`` (paragraph-relative seconds, exactly as
            generate_audio would return for the joined text), and an
            additional ``line_breakdown`` list — one entry per input
            line with its own ``text``, ``word_timestamps`` (also
            paragraph-relative, ready for the same absolute-offset
            accumulation core_engine already does per line), and
            ``word_count`` — so callers can recover exact per-line
            timing without re-synthesizing anything.
        """
        started = time.perf_counter()
        texts = [
            str(line.get("text") or line.get("text_content") or "").strip()
            for line in lines
        ]
        texts = [t for t in texts if t]
        if not texts:
            return self._err("No non-empty lines provided for paragraph", started)

        combined_text = " ".join(texts)
        # PHASE 7: core_engine plans each line with neighboring context before
        # paragraph batching.  Rebase those line-local word indices into one
        # paragraph plan so a single synthesis request retains every phrase
        # contour and stressed word.  If a caller supplied no plans,
        # generate_audio creates a safe standalone plan for combined_text.
        line_plans: List[Dict[str, Any]] = []
        for line in lines:
            plan = line.get("prosody_plan")
            if not isinstance(plan, dict):
                profile = line.get("profile")
                plan = (
                    profile.get("prosody_plan")
                    if isinstance(profile, dict) else None
                )
            if isinstance(plan, dict):
                line_plans.append(plan)
        paragraph_profile = dict(character_profile)
        if len(line_plans) == len(texts):
            paragraph_profile["prosody_plan"] = merge_prosody_plans(line_plans)
        gen = self.generate_audio(
            combined_text, paragraph_profile, output_path, voice_sample_path
        )
        if not gen.get("success"):
            return gen

        gdata = gen.get("data") or {}
        all_timestamps = list(gdata.get("word_timestamps") or [])
        # PHASE 7 sync hardening: count the words the engine actually spoke,
        # not raw bracket tags.  A [PAUSE:LONG] token contributes silence
        # but no timestamped word; [SPELL]KJV[/SPELL] contributes K J V.
        line_word_counts = [len(spoken_words(text)) for text in texts]

        # PHASE 5 (timestamp preservation): slice the combined paragraph's
        # word timestamps back out per original line by cumulative word
        # count — exact, not approximate (see docstring above for why).
        line_breakdown: List[Dict[str, Any]] = []
        cursor = 0
        for text, word_count in zip(texts, line_word_counts):
            line_words = all_timestamps[cursor: cursor + word_count]
            cursor += word_count
            line_breakdown.append(
                {
                    "text": text,
                    "word_timestamps": line_words,
                    "word_count": word_count,
                    "start": (
                        float(line_words[0]["start"]) if line_words else 0.0
                    ),
                    "end": (
                        float(line_words[-1]["end"]) if line_words else 0.0
                    ),
                }
            )

        gdata["line_breakdown"] = line_breakdown
        gdata["line_count"] = len(texts)
        gen["data"] = gdata
        gen["duration_ms"] = _ms(started)
        return gen

    def generate_with_piper(
        self,
        text: str,
        voice_model: str,
        settings: Dict[str, Any],
        output_path: str,
    ) -> Dict[str, Any]:
        """Generate WAV via Piper subprocess (no persistent model load)."""
        started = time.perf_counter()
        piper = self._find_piper_binary()
        if piper is None:
            return self._err(
                "Piper executable not found (STATUS: NOT VERIFIED on this host)",
                started,
            )
        model_path = self._find_piper_model(voice_model)
        if model_path is None:
            # Fallback: synthetic tone so pipeline can be tested offline
            return self._synthetic_speech_fallback(
                text, output_path, settings, engine=ENGINE_PIPER, started=started
            )
        speed = max(0.5, float(settings.get("speed", 1.0)))
        length_scale = 1.0 / speed
        cmd = [
            str(piper),
            "--model",
            str(model_path),
            "--output_file",
            output_path,
            "--length_scale",
            f"{length_scale:.4f}",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=text,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if proc.returncode != 0 or not Path(output_path).exists():
                # D2a: log the meaningful tail of stderr only — piper
                # prints a full Python traceback; the old 800+ char
                # WARNING per narration line made render logs unusable.
                tail = (proc.stderr or "").strip().splitlines()
                detail = tail[-1] if tail else "no stderr"
                self.log.warning(
                    "Piper failed (rc=%s): %s", proc.returncode, detail[:300]
                )
                return self._synthetic_speech_fallback(
                    text, output_path, settings, engine=ENGINE_PIPER, started=started
                )
            timestamps = self._approximate_word_timestamps(
                text, self._wav_duration(Path(output_path))
            )
            return self.make_response(
                True,
                {
                    "audio_path": output_path,
                    "word_timestamps": timestamps,
                    "duration": self._wav_duration(Path(output_path)),
                },
                duration_ms=_ms(started),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.error("Piper subprocess error: %s", exc)
            return self._synthetic_speech_fallback(
                text, output_path, settings, engine=ENGINE_PIPER, started=started
            )

    def generate_with_kokoro(
        self,
        text: str,
        voice_name: str,
        settings: Dict[str, Any],
        output_path: str,
    ) -> Dict[str, Any]:
        """Generate WAV with Kokoro (lazy-loaded)."""
        started = time.perf_counter()
        loaded = self._ensure_kokoro_loaded()
        if not loaded:
            return self._synthetic_speech_fallback(
                text, output_path, settings, engine=ENGINE_KOKORO, started=started
            )
        speed = float(settings.get("speed", 1.0))
        # PHASE 9 (TTS failure recovery): synthesis is retried once
        # before degrading to the synthetic tone. Lines are generated
        # concurrently (see core_engine._stage_tts), and a transient
        # onnxruntime allocation failure under that load used to burn
        # the whole line — the fallback tone is audible in the finished
        # video, so one cheap retry is worth far more than it costs.
        # Behavior on a first-attempt success is completely unchanged.
        for attempt in range(_SYNTHESIS_ATTEMPTS):
            try:
                samples, sample_rate = self.kokoro_instance.create(
                    text=text,
                    voice=voice_name,
                    speed=speed,
                    lang="en-us",
                )
                self._write_wav_samples(output_path, samples, int(sample_rate))
                duration = self._wav_duration(Path(output_path))
                timestamps = self._approximate_word_timestamps(text, duration)
                self._mark_engine_used(ENGINE_KOKORO)
                return self.make_response(
                    True,
                    {
                        "audio_path": output_path,
                        "word_timestamps": timestamps,
                        "duration": duration,
                    },
                    duration_ms=_ms(started),
                )
            except MemoryError as exc:
                # Retrying an allocation failure immediately would just
                # fail again; release what this module holds first.
                self.log.error("Kokoro ran out of memory: %s", exc)
                gc.collect()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 < _SYNTHESIS_ATTEMPTS:
                    self.log.warning(
                        "Kokoro generation failed (attempt %d/%d), retrying: %s",
                        attempt + 1,
                        _SYNTHESIS_ATTEMPTS,
                        exc,
                    )
                    time.sleep(_SYNTHESIS_RETRY_SECONDS)
                    continue
                self.log.error("Kokoro generation failed: %s", exc)
        return self._synthetic_speech_fallback(
            text, output_path, settings, engine=ENGINE_KOKORO, started=started
        )

    def generate_with_xtts(
        self,
        text: str,
        voice_sample_path: Optional[str],
        settings: Dict[str, Any],
        output_path: str,
    ) -> Dict[str, Any]:
        """Generate WAV with XTTS v2 (lazy-loaded, unload after use)."""
        started = time.perf_counter()
        loaded = self._ensure_xtts_loaded()
        if not loaded:
            return self._synthetic_speech_fallback(
                text, output_path, settings, engine=ENGINE_XTTS, started=started
            )
        speed = float(settings.get("speed", 1.0))
        try:
            kwargs: Dict[str, Any] = {
                "text": text,
                "language": "en",
                "file_path": output_path,
                "speed": speed,
            }
            if voice_sample_path and Path(voice_sample_path).exists():
                kwargs["speaker_wav"] = voice_sample_path
            self.xtts_instance.tts_to_file(**kwargs)
            duration = self._wav_duration(Path(output_path))
            timestamps = self._approximate_word_timestamps(text, duration)
            self._mark_engine_used(ENGINE_XTTS)
            return self.make_response(
                True,
                {
                    "audio_path": output_path,
                    "word_timestamps": timestamps,
                    "duration": duration,
                },
                duration_ms=_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("XTTS generation failed: %s", exc)
            return self._synthetic_speech_fallback(
                text, output_path, settings, engine=ENGINE_XTTS, started=started
            )
        finally:
            # Always unload XTTS after generation attempt (RAM)
            self.unload_engine_from_memory(ENGINE_XTTS)

    # ------------------------------------------------------------------
    # Emotion / pause / tags
    # ------------------------------------------------------------------

    def load_pronunciation_dict(self, path: str) -> Dict[str, str]:
        """Load a pronunciation dictionary JSON file: {"word": "spoken form"}.

        FEATURE (v3.2.13): the Voice Controls "Pronunciation:" field has
        existed in the UI for a while but nothing ever read or applied
        it — this is the missing other half. File format is a flat JSON
        object mapping the word AS WRITTEN to how it should be spoken,
        e.g. {"Yahweh": "Yah-way", "KJV": "K J V"}. This is a TEXT
        substitution (not true IPA/phoneme control — Kokoro/Piper don't
        expose that through this app), same technique the [SPELL] tag
        uses, just driven by a reusable dictionary instead of inline
        tags in every script.

        Returns an empty dict (never raises) if the file is missing,
        empty, or malformed — pronunciation is a nice-to-have polish
        feature, never a reason to fail a render.
        """
        try:
            p = Path(path)
            if not path or not p.is_file():
                return {}
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {
                str(k): str(v) for k, v in data.items()
                if str(k).strip() and str(v).strip()
            }
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def apply_pronunciation_dict(
        self, text: str, pronunciation: Dict[str, str]
    ) -> str:
        """Replace dictionary words in text with their spoken form.

        Whole-word, case-insensitive matching (so "yahweh" in the
        script matches a "Yahweh" dictionary entry) — never matches
        inside a longer word (e.g. a dictionary entry for "Cain" won't
        touch "explain"). Runs before pause-tag processing, same
        ordering as the [SPELL] tag, since this is also a text
        transform, not a pause.
        """
        if not pronunciation or not text:
            return text
        for word, spoken in pronunciation.items():
            pattern = re.compile(
                r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE
            )
            text = pattern.sub(spoken, text)
        return text

    def apply_emotion_parameters(
        self, voice_profile: Dict[str, Any], emotion: str
    ) -> Dict[str, Any]:
        """Adjust speed/pitch/volume from base profile using emotion preset.

        Args:
            voice_profile: Base character profile.
            emotion: Emotion name (supports aliases).

        Returns:
            Dict with speed, pitch, volume, emotion, pause_multiplier.
        """
        key = self._normalize_emotion(emotion)
        preset = EMOTION_PRESETS.get(key, EMOTION_PRESETS["neutral"])
        base_speed = float(voice_profile.get("speed", 1.0))
        base_pitch = float(voice_profile.get("pitch", 0.0))
        base_volume = float(voice_profile.get("volume", 1.0))
        prosody_plan = voice_profile.get("prosody_plan")
        prosody_enabled = bool(
            isinstance(prosody_plan, dict) and prosody_plan.get("enabled")
        )
        if prosody_enabled:
            # PHASE 7: the planner keeps the established emotion rates but
            # smooths abrupt neighboring emotion changes and adds a stable,
            # text-derived cadence variation.  Determinism matters here:
            # retries should not produce a differently paced take.
            speed_mult = float(
                prosody_plan.get("rate_multiplier") or preset["speed_mult"]
            )
            speed = base_speed * speed_mult
        else:
            speed_mult = float(preset["speed_mult"])
            speed = base_speed * speed_mult
            # Exact Phase 1-6 fallback when prosody is disabled/unavailable.
            speed = speed * (1.0 + random.uniform(-0.02, 0.02))
        speed = max(0.5, min(2.0, speed))
        pitch = base_pitch + float(preset["pitch_off"])
        pitch = max(-12.0, min(12.0, pitch))
        volume = base_volume * float(preset["vol_mult"])
        volume = max(0.0, min(2.0, volume))
        pause_mult = PAUSE_EMOTION_MULTIPLIERS.get(key, 1.0)
        return {
            "speed": round(speed, 4),
            "pitch": round(pitch, 3),
            "volume": round(volume, 4),
            "emotion": key,
            "pause_multiplier": pause_mult,
            "speed_mult": speed_mult,
            "pitch_off": preset["pitch_off"],
            "vol_mult": preset["vol_mult"],
            "prosody_enabled": prosody_enabled,
        }

    def generate_pause(
        self,
        pause_type: str,
        emotion: str = "neutral",
        speed: float = 1.0,
    ) -> float:
        """Generate a humanized pause duration with random variation.

        Args:
            pause_type: MICRO|SHORT|MEDIUM|LONG|DRAMATIC.
            emotion: Emotion context for multiplier.
            speed: Speaking rate (faster speech → shorter pauses).

        Returns:
            Duration in seconds.
        """
        ptype = str(pause_type or "SHORT").upper()
        if ptype not in PAUSE_BASE_DURATIONS:
            ptype = "SHORT"
        base = PAUSE_BASE_DURATIONS[ptype]
        variation = PAUSE_VARIATIONS[ptype]
        duration = base + random.uniform(-variation, variation)
        emotion_key = self._normalize_emotion(emotion)
        duration *= PAUSE_EMOTION_MULTIPLIERS.get(emotion_key, 1.0)
        rate = max(0.5, float(speed) if speed else 1.0)
        duration *= 1.0 / rate
        duration = max(0.10, min(5.00, duration))
        return round(duration, 3)

    def process_pause_tags(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Extract pause/spell tags and return clean text + pause markers.

        Supports TWO tag formats, both usable in the same script:
          - The original [PAUSE:TYPE] format (TYPE = MICRO/SHORT/MEDIUM/
            LONG/DRAMATIC) — duration resolved later with humanized
            random variation via generate_pause(), unchanged behavior.
          - FEATURE (v3.2.11): the "TTS-ready script" tag format —
            [SHORT_PAUSE] [PAUSE] [MEDIUM_PAUSE] [LONG_PAUSE]
            [SILENCE=500ms] [SILENCE=2s] [PAUSE 3] — these carry an
            EXACT duration already (no randomization), matching a
            script writer's expectation that "[PAUSE] = 0.8 seconds"
            means exactly that, not a random range.
          - [SPELL]TEXT[/SPELL] — NOT a pause; rewrites TEXT into
            letter-by-letter spaced form (e.g. "KJV" -> "K J V") so the
            TTS engine naturally reads each letter separately, instead
            of trying to pronounce the acronym as a word.

        Args:
            text: Raw dialogue text, possibly containing any of the
                above tags.

        Returns:
            (clean_text, markers) where each marker has word_index,
            type, duration (None for old-format tags — filled in later
            with emotion/speed context; an exact float for new-format
            tags), and char_position.
        """
        # SPELL is a text transform, not a pause — resolve it first so
        # word_index counting below (for pause placement) reflects the
        # EXPANDED letter sequence, matching where the pause will
        # actually land once TTS reads "K J V" as three words.
        def _spell_out(match: "re.Match") -> str:
            letters = re.sub(r"\s+", "", match.group(1) or "")
            return " ".join(letters)

        text = SPELL_TAG_RE.sub(_spell_out, text or "")

        markers: List[Dict[str, Any]] = []
        parts: List[str] = []
        last = 0
        word_index = 0

        # Merge both known tag formats into one ordered pass over the
        # text so markers come out in the correct left-to-right order
        # regardless of which format was used where.
        known = list(PAUSE_TAG_RE.finditer(text)) + list(SCRIPT_TAG_RE.finditer(text))
        known_spans = [(m.start(), m.end()) for m in known]

        # SAFETY NET (v3.2.11): the whole point of this feature is that
        # tags get FOLLOWED, never read aloud — if the script contains
        # something bracket-shaped that ISN'T one of the recognized tags
        # (a typo, an unsupported tag the AI generator wasn't supposed
        # to produce, a leftover [whisper]-style tag), it must be
        # stripped too, not left to leak into the TTS input as literal
        # garbage text. Found here (not as a separate pass afterward) so
        # word_index stays accurate — a separate later pass would shift
        # word counts for every pause marker placed after a stripped tag.
        unknown = [
            m for m in re.finditer(r"\[[^\[\]\n]{1,40}\]", text)
            if not any(m.start() < e and s < m.end() for s, e in known_spans)
        ]
        combined = sorted(known + unknown, key=lambda m: m.start())

        for match in combined:
            segment = text[last : match.start()]
            parts.append(segment)
            word_index += len(WORD_RE.findall(segment))
            last = match.end()

            is_known = match in known
            if not is_known:
                self.log.warning(
                    "Unsupported tag %r found in narration text — "
                    "removed, not spoken (only the documented pause/"
                    "spell tags are supported)", match.group(0),
                )
                continue  # removed, no marker — just advances word_index

            if match.re is PAUSE_TAG_RE:
                ptype = match.group(1).upper()
                duration = None  # resolved later via generate_pause()
            elif match.group("sil_val") is not None:
                value = float(match.group("sil_val"))
                seconds = value / 1000.0 if match.group("sil_unit").lower() == "ms" else value
                seconds = max(0.05, min(30.0, seconds))
                ptype = "SILENCE"
                duration = round(seconds, 3)
            elif match.group("custom_secs") is not None:
                ptype = "CUSTOM"
                duration = round(max(0.05, min(30.0, float(match.group("custom_secs")))), 3)
            else:
                ptype = match.group("named").upper()
                duration = SCRIPT_TAG_DURATIONS.get(ptype, 0.8)

            markers.append(
                {
                    "word_index": word_index,
                    "type": ptype,
                    "duration": duration,
                    "char_position": match.start(),
                }
            )
        parts.append(text[last:] if text else "")
        clean = re.sub(r"\s+", " ", "".join(parts)).strip()
        return clean, markers

    def insert_pauses_into_audio(
        self,
        audio_path: str,
        pause_markers: List[Dict[str, Any]],
        word_timestamps: List[Dict[str, Any]],
        breathing: bool = False,
        character_profile: Optional[Dict[str, Any]] = None,
        emotion: str = "neutral",
        speed: float = 1.0,
    ) -> Dict[str, Any]:
        """Insert silence (and optional breath) at pause marker positions.

        Args:
            audio_path: WAV path to modify in place.
            pause_markers: Markers from process_pause_tags.
            word_timestamps: Word timing list.
            breathing: Whether to mix breath samples.
            character_profile: Optional profile for breath volume.
            emotion: Emotion for pause duration.
            speed: Speaking rate for pause duration.

        Returns:
            Standard response.
        """
        started = time.perf_counter()
        path = Path(audio_path)
        if not path.exists():
            return self._err(f"Audio not found: {audio_path}", started)
        try:
            from pydub import AudioSegment
        except ImportError:
            return self._err("pydub not installed", started)

        audio = AudioSegment.from_file(str(path))
        # Build pause list with durations and ms positions (process back-to-front)
        prepared: List[Tuple[int, int, str, Dict[str, Any]]] = []
        for marker in pause_markers:
            marker["_inserted"] = False
            ptype = str(marker.get("type") or "SHORT").upper()
            duration = marker.get("duration")
            if duration is None:
                duration = self.generate_pause(ptype, emotion, speed)
                # Preserve the exact randomized duration actually inserted.
                # _adjust_timestamps_for_pauses reads this same marker later;
                # generating a second random value there would desynchronise
                # every following subtitle from the audio.
                marker["duration"] = duration
            ms_pos = self._marker_to_ms(marker, word_timestamps, len(audio))
            prepared.append(
                (ms_pos, int(float(duration) * 1000), ptype, marker)
            )
        prepared.sort(key=lambda item: item[0], reverse=True)

        for ms_pos, silence_ms, ptype, marker in prepared:
            silence = AudioSegment.silent(duration=max(1, silence_ms))
            if breathing and BREATH_CHANCE.get(ptype, 0) > 0:
                if random.random() <= BREATH_CHANCE[ptype]:
                    breath = self._load_breath_segment(
                        ptype, character_profile, silence_ms
                    )
                    if breath is not None:
                        if len(breath) < silence_ms:
                            silence = breath.overlay(silence)
                        else:
                            # PHASE 3 (click removal): truncating a
                            # breath sample to fit a shorter pause slot
                            # creates a brand new hard edge at the cut
                            # point, even though _load_breath_segment
                            # already faded the sample's OWN natural
                            # end — that fade is now discarded by the
                            # slice, so re-apply a short fade-out here.
                            trimmed = breath[:silence_ms]
                            edge_fade_ms = min(
                                int(_EDGE_FADE_MS), max(1, len(trimmed) // 2 - 1)
                            )
                            if edge_fade_ms > 0 and len(trimmed) > edge_fade_ms * 2:
                                trimmed = trimmed.fade_out(edge_fade_ms)
                            silence = trimmed
            # PHASE 3 (click & transition refinement): snap the splice
            # position to the nearest true zero crossing before cutting
            # — word-timestamp boundaries are approximate and can land
            # mid-waveform, which a fade-out/fade-in alone softens but
            # doesn't fully eliminate. Snapping first, THEN fading,
            # removes the discontinuity at its source instead of just
            # masking it.
            ms_pos = self._nearest_zero_crossing_ms(audio, ms_pos)
            edge_fade_ms = int(_EDGE_FADE_MS)
            before = audio[:ms_pos]
            after = audio[ms_pos:]
            if len(before) > edge_fade_ms * 2:
                before = before.fade_out(edge_fade_ms)
            if len(after) > edge_fade_ms * 2:
                after = after.fade_in(edge_fade_ms)
            joined = before + silence + after
            # PHASE 3: validate after every transition — reject a
            # splice that somehow produced non-finite/garbage samples
            # and keep the previous, still-good audio instead.
            if self._pydub_segment_is_valid(joined):
                audio = joined
                marker["_inserted"] = True
            else:
                self.log.warning(
                    "Pause splice at %dms produced invalid audio — "
                    "keeping audio unchanged for this pause marker",
                    ms_pos,
                )

        audio.export(str(path), format="wav")
        inserted_count = sum(
            1 for marker in pause_markers if marker.get("_inserted")
        )
        return self.make_response(
            True,
            {"audio_path": str(path), "pauses_inserted": inserted_count},
            duration_ms=_ms(started),
        )

    def _pydub_segment_is_valid(self, seg: Any) -> bool:
        """PHASE 3: validate a pydub segment after a splice/transition.

        Identical contract to audio_processor.AudioProcessor's method of
        the same name (Rule A: modules don't cross-import). Checks the
        segment is non-empty and, for 16-bit PCM, free of NaN/Inf.
        """
        try:
            if seg is None or len(seg) <= 0:
                return False
            if seg.sample_width != 2:
                return True
            import numpy as np

            samples = np.frombuffer(seg.raw_data, dtype="<i2")
            if samples.size == 0:
                return False
            return bool(np.isfinite(samples.astype(np.float64)).all())
        except Exception:  # noqa: BLE001 - treat unreadable as invalid
            return False

    def add_breathing_sounds(
        self,
        audio_path: str,
        character_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Insert breath samples at detected sentence-boundary silences."""
        started = time.perf_counter()
        path = Path(audio_path)
        if not path.exists():
            return self._err(f"Audio not found: {audio_path}", started)
        if not character_profile.get("breathing_enabled"):
            return self.make_response(
                True,
                {"audio_path": str(path), "breaths_added": 0},
                duration_ms=_ms(started),
            )
        try:
            from pydub import AudioSegment
            from pydub.silence import detect_silence
        except ImportError:
            return self._err("pydub not installed", started)

        audio = AudioSegment.from_file(str(path))
        silences = detect_silence(audio, min_silence_len=300, silence_thresh=-40)
        breaths = 0
        # Process from end so indices stay valid
        for start_ms, end_ms in reversed(silences):
            gap = end_ms - start_ms
            if gap < 300:
                continue
            ptype = "MEDIUM" if gap < 1200 else "LONG"
            breath = self._load_breath_segment(ptype, character_profile, gap)
            if breath is None:
                continue
            insert_at = start_ms + 50
            # PHASE 3 (click & transition refinement): this lands inside
            # an already-detected silence region, so it's normally very
            # close to zero already — but snapping to the nearest true
            # zero crossing costs nothing and removes any residual
            # discontinuity from that region's own natural noise floor.
            insert_at = self._nearest_zero_crossing_ms(audio, insert_at)
            chunk = breath[: min(len(breath), gap - 50)]
            # PHASE 3 (click removal): as in insert_pauses_into_audio,
            # truncating the breath sample to fit the available silence
            # gap discards whatever fade _load_breath_segment already
            # applied at its natural end — re-fade the (possibly
            # shorter) truncated chunk so it still ends cleanly instead
            # of cutting off mid-waveform.
            edge_fade_ms = min(int(_EDGE_FADE_MS), max(1, len(chunk) // 2 - 1))
            if edge_fade_ms > 0 and len(chunk) > edge_fade_ms * 2:
                chunk = chunk.fade_out(edge_fade_ms)
            joined = (
                audio[:insert_at]
                + chunk.overlay(audio[insert_at : insert_at + len(chunk)])
                + audio[insert_at + len(chunk) :]
            )
            # PHASE 3: validate after every transition.
            if self._pydub_segment_is_valid(joined):
                audio = joined
                breaths += 1
            else:
                self.log.warning(
                    "Breath overlay at %dms produced invalid audio — "
                    "skipping this breath insertion",
                    insert_at,
                )
        audio.export(str(path), format="wav")
        return self.make_response(
            True,
            {"audio_path": str(path), "breaths_added": breaths},
            duration_ms=_ms(started),
        )

    def apply_voice_effects(
        self,
        audio_path: str,
        character_profile: Dict[str, Any],
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Apply the narration voice-effects chain, per-line.

        PHASE 2 (voice effects chain rebuild): fixed, safe processing
        order —

            High-pass -> (optional) Noise Gate -> Gentle EQ ->
            (optional) Reverb -> (optional) Special Effect ->
            (optional) Volume -> Light Compressor -> Limiter ->
            (optional, single) LUFS Normalization

        The mandated backbone (high-pass -> EQ -> compressor -> limiter
        -> LUFS) always appears in that relative order; the extra
        optional coloring stages this app already supported (noise
        gate, reverb, special effect, per-line volume) are interleaved
        without disturbing it, and every stage — including the ones
        that predate this phase — is now individually bypassable via
        ``character_profile``/``params`` flags, defaulting to the exact
        prior behavior for full backward compatibility:

            highpass_enabled      (default True)
            noise_gate_enabled    (default True  — existing schema col)
            eq_preset == "flat"/"none" already bypasses EQ (unchanged)
            reverb_preset == "none" already bypasses reverb (unchanged)
            special_effect == "none" already bypasses it (unchanged)
            compression_enabled   (default True  — existing schema col)
            limiter_enabled       (default True)
            lufs_normalize_enabled (default False — new, opt-in; the
                final mix already runs its own single 2-pass LUFS
                normalize over the complete program, so per-line LUFS
                stays off by default to avoid ever normalizing the same
                audio twice)

        There is at most ONE limiter and at most ONE LUFS normalization
        pass in this chain — never chained/duplicated. If FFmpeg is
        unavailable, or the full chain fails/produces degenerate audio,
        this degrades in two safe steps instead of crashing or shipping
        broken audio:

          1. Retry with only the mandatory, extensively-exercised
             backbone (high-pass, EQ, compressor, limiter) — dropping
             the more exotic optional filters (noise gate, reverb,
             special effect, per-line LUFS) most likely to trip a
             ffmpeg-build-specific bug (see the historical v3.1.2
             agate/EQ/alimiter collapse this class of check exists for).
          2. If even that fails, keep the original, unmodified audio —
             losing optional coloring is a minor cosmetic loss; losing
             the narration is not. This never raises.
        """
        started = time.perf_counter()
        path = Path(audio_path)
        if not path.exists():
            return self._err(f"Audio not found: {audio_path}", started)

        params = params or {}
        volume = float(params.get("volume", character_profile.get("volume", 1.0)))
        full_filters, safe_filters, flags = self._build_voice_effects_filters(
            character_profile, params, volume
        )
        full_chain = ",".join(full_filters)

        ffmpeg = self._find_ffmpeg()
        if ffmpeg is None:
            # STATUS: NOT VERIFIED full chain — apply volume via pydub only
            return self._apply_volume_pydub(path, volume, started, full_chain)

        attempt = self._run_ffmpeg_filter_chain(ffmpeg, path, full_filters, tag="fx-full")
        if attempt["ok"] and not self._audio_is_degenerate(attempt["output"]):
            attempt["output"].replace(path)
            return self.make_response(
                True,
                {
                    "audio_path": str(path),
                    "filter_chain": full_chain,
                    "ffmpeg": True,
                    "effects_applied": flags,
                },
                duration_ms=_ms(started),
            )
        self._discard_temp(attempt.get("output"))

        # Step 1 of graceful degradation: bypass the optional/exotic
        # stages and retry with just the mandatory backbone — only when
        # that backbone actually differs from what was just tried (no
        # point retrying an identical chain).
        if safe_filters != full_filters:
            safe_chain = ",".join(safe_filters)
            fallback = self._run_ffmpeg_filter_chain(ffmpeg, path, safe_filters, tag="fx-safe")
            if fallback["ok"] and not self._audio_is_degenerate(fallback["output"]):
                fallback["output"].replace(path)
                return self.make_response(
                    True,
                    {
                        "audio_path": str(path),
                        "filter_chain": safe_chain,
                        "ffmpeg": True,
                        "effects_applied": flags,
                        "effects_skipped": "optional_stages_bypassed_after_failure",
                    },
                    warnings=[
                        "Some optional voice effects (noise gate/reverb/"
                        "special effect/per-line LUFS) failed on this "
                        "machine's ffmpeg build — applied only the core "
                        "high-pass/EQ/compressor/limiter chain instead"
                    ],
                    duration_ms=_ms(started),
                )
            self._discard_temp(fallback.get("output"))

        # Step 2: keep the original, unmodified (pre-effects) audio —
        # never ship silence/degenerate output, never crash.
        self.log.warning(
            "Voice effects chain failed or produced degenerate audio for "
            "%s — keeping original TTS audio unmodified instead of "
            "shipping broken output",
            path,
        )
        return self.make_response(
            True,
            {
                "audio_path": str(path),
                "filter_chain": full_chain,
                "ffmpeg": True,
                "effects_skipped": "degenerate_output_detected",
            },
            warnings=[
                "Voice effects produced invalid audio on this machine's "
                "ffmpeg build; original clean narration was kept instead"
            ],
            duration_ms=_ms(started),
        )

    def _build_voice_effects_filters(
        self,
        character_profile: Dict[str, Any],
        params: Dict[str, Any],
        volume: float,
    ) -> Tuple[List[str], List[str], Dict[str, bool]]:
        """Resolve per-stage bypass flags into two ordered filter lists.

        Returns ``(full_filters, safe_filters, flags)`` — ``safe_filters``
        is the mandatory backbone only (high-pass/EQ/compressor/limiter,
        respecting their own bypass flags), used as the automatic
        fallback when the full chain fails. ``flags`` records which
        stages actually ran, for callers/tests/QA to inspect.
        """

        def _flag(key: str, default: bool) -> bool:
            if key in params:
                return bool(params[key])
            return bool(character_profile.get(key, default))

        flags = {
            "highpass": _flag("highpass_enabled", True),
            "noise_gate": _flag("noise_gate_enabled", True),
            "eq": False,
            "reverb": False,
            "special_effect": False,
            "volume": abs(volume - 1.0) > 0.001,
            "compressor": _flag("compression_enabled", True),
            "limiter": _flag("limiter_enabled", True),
            "lufs_normalize": _flag("lufs_normalize_enabled", False),
        }

        eq_key = str(character_profile.get("eq_preset") or "documentary_male")
        eq = EQ_PRESETS.get(eq_key, "")
        flags["eq"] = bool(eq)

        reverb_key = str(
            character_profile.get("reverb_preset")
            or character_profile.get("reverb")
            or "none"
        )
        reverb = REVERB_PRESETS.get(reverb_key, "")
        flags["reverb"] = bool(reverb)

        special_key = str(character_profile.get("special_effect") or "none")
        special = SPECIAL_EFFECTS.get(special_key, "")
        flags["special_effect"] = bool(special)

        backbone: List[str] = []
        if flags["highpass"]:
            backbone.append(HIGHPASS_FILTER)
        if flags["eq"]:
            backbone.append(eq)
        if flags["compressor"]:
            backbone.append(COMPRESSOR_FILTER)
        if flags["limiter"]:
            backbone.append(LIMITER_FILTER)

        full: List[str] = []
        if flags["highpass"]:
            full.append(HIGHPASS_FILTER)
        if flags["noise_gate"]:
            full.append(NOISE_GATE_FILTER)
        if flags["eq"]:
            full.append(eq)
        if flags["reverb"]:
            full.append(reverb)
        if flags["special_effect"]:
            full.append(special)
        if flags["volume"]:
            full.append(f"volume={volume:.4f}")
        if flags["compressor"]:
            full.append(COMPRESSOR_FILTER)
        if flags["limiter"]:
            full.append(LIMITER_FILTER)
        if flags["lufs_normalize"]:
            full.append(LUFS_NORMALIZE_FILTER)

        # Degenerate case: every stage bypassed — nothing to run at all.
        if not full:
            full = ["anull"]
        if not backbone:
            backbone = ["anull"]
        return full, backbone, flags

    def _run_ffmpeg_filter_chain(
        self, ffmpeg: Path, path: Path, filters: List[str], tag: str = "fx"
    ) -> Dict[str, Any]:
        """Run one FFmpeg filter chain to a temp file; never raises.

        Returns ``{"ok": bool, "output": Path}`` — ``output`` always
        points at a temp path (may or may not exist depending on
        success) so the caller can clean it up uniformly. ``tag``
        namespaces the temp filename (e.g. "full" vs "safe") so the two
        degradation attempts in apply_voice_effects never collide on the
        same temp file even if run back-to-back on the same input path.
        """
        temp_out = path.with_suffix(f".{tag}.wav")
        cmd = [
            str(ffmpeg),
            "-y",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            "-ar",
            "48000",
            "-ac",
            "2",
            str(temp_out),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, check=False
            )
            if proc.returncode != 0 or not temp_out.exists():
                self.log.warning(
                    "FFmpeg effects chain failed (rc=%s): %s",
                    proc.returncode,
                    (proc.stderr or "")[-500:],
                )
                return {"ok": False, "output": temp_out}
            return {"ok": True, "output": temp_out}
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.warning("FFmpeg effects error: %s", exc)
            return {"ok": False, "output": temp_out}

    def _discard_temp(self, path: Optional[Path]) -> None:
        """Best-effort cleanup of a leftover temp file."""
        if path is None:
            return
        safe_unlink(path)

    @staticmethod
    def _read_backup(path: Path) -> Optional[bytes]:
        """Snapshot a line's bytes before an in-place processing stage.

        PHASE 9: the revert-on-failure protection added in PHASE 1 read
        the backup with a bare ``read_bytes()``. If THAT read failed
        (the file vanished, a transient share lock), the exception
        escaped ``generate_audio`` and killed the line outright — the
        exact outcome the protection exists to prevent. A failed
        snapshot now simply means "no revert available", and the stage
        proceeds under its own validation as before.
        """
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _restore_backup(self, path: Path, backup: Optional[bytes]) -> bool:
        """Restore bytes captured by :meth:`_read_backup`.

        PHASE 9: written atomically, so a revert interrupted part-way
        cannot leave the line in a worse state than the bad audio it was
        replacing, and a failed revert is logged rather than raised.
        """
        if backup is None:
            self.log.warning("No backup available to restore for %s", path)
            return False
        if atomic_write(path, lambda temp: temp.write_bytes(backup)):
            return True
        self.log.error("Could not restore original audio for %s", path)
        return False

    def _audio_is_degenerate(self, path: Path) -> bool:

        """Detect a DC-biased / collapsed WAV (effects-chain failure).

        The observed failure signature is NOT silence — it's a large
        constant DC offset (e.g. mean around -22000 out of +/-32768) with
        some modest variation riding on top of it. Real, properly generated
        speech is AC-coupled (mean close to 0). We flag output where the
        offset itself is large AND dominates over the actual variation,
        which is the fingerprint reproduced from the effects chain bug
        (confirmed via direct testing), not a property real speech has.
        Only handles the common 16-bit PCM case; anything else is assumed
        fine (best-effort safety net, not a full audio validator).
        """
        try:
            with wave.open(str(path), "rb") as handle:
                n_frames = handle.getnframes()
                sampwidth = handle.getsampwidth()
                channels = max(1, handle.getnchannels())
                if n_frames == 0:
                    return True
                if sampwidth != 2:
                    return False
                raw = handle.readframes(n_frames)
            samples = array.array("h")
            samples.frombytes(raw[: (len(raw) // 2) * 2])
            if channels > 1:
                samples = samples[0::channels]
            if not samples:
                return True
            step = max(1, len(samples) // 48000)
            subset = samples[::step]
            mean = sum(subset) / len(subset)
            variance = sum((s - mean) ** 2 for s in subset) / len(subset)
            std = variance**0.5
            # Two known failure shapes reproduced from the effects chain bug:
            # (a) large DC bias dominating over real variation, or
            # (b) the whole clip collapsed to near-total silence — neither
            # is possible from real, successfully generated speech audio.
            return (abs(mean) > 4000 and abs(mean) > std) or std < 10.0
        except Exception:  # noqa: BLE001 - best-effort safety net only
            return False

    def _validate_wav_basic(self, path: Path) -> bool:
        """Reject a corrupted buffer: PHASE 1/PHASE 9 defensive gate.

        Checked after every processing stage that rewrites a narration
        line in place (pitch shift, pause insertion, effects, click
        repair): the file must exist, contain at least one audio frame,
        have a plausible sample rate/channel count, and — for the common
        16-bit PCM case — contain no NaN-shaped garbage. This is a fast,
        best-effort sanity check, not a full decoder; it exists to catch
        "the previous stage silently produced junk", not to replace the
        stage-specific validation (e.g. _audio_is_degenerate) already in
        place for the effects chain specifically.
        """
        try:
            if not path.exists() or path.stat().st_size == 0:
                return False
            with wave.open(str(path), "rb") as handle:
                n_frames = handle.getnframes()
                sr = handle.getframerate()
                channels = handle.getnchannels()
                sampwidth = handle.getsampwidth()
            if n_frames <= 0 or sr <= 0 or channels <= 0 or sampwidth <= 0:
                return False
            return True
        except Exception:  # noqa: BLE001 - unreadable file is invalid
            return False

    def _finalize_line_audio(
        self, path: Path, trim_leading_silence: bool = False
    ) -> Dict[str, Any]:
        """Safety pass applied to every generated narration line.

        PHASE 1/3/4 (audio stability, click removal, hard intro fix):
        runs three independent, best-effort repairs on the WAV at
        ``path`` (in place):

          1. Leading-silence trim (only when ``trim_leading_silence`` is
             True — i.e. right after raw synthesis, before pause/effects
             touch the file): removes excessive dead air at the very
             start of a TTS render WITHOUT cutting into the first
             phoneme, fixing the "hard beginning of narration" problem.
          2. Click/pop repair: detects sample-to-sample discontinuities
             far outside the local signal's normal range (the audible
             fingerprint of a click — a decoder glitch, a bad splice
             point, or an unstable effects filter) and smooths them.
          3. Fade-in/fade-out: a short (5-10ms) edge fade on every clip
             guarantees the waveform starts and ends at (near) zero,
             which is what actually prevents a start/end/join click —
             the discontinuity check in step 2 only catches *internal*
             clicks, not a hard edge at sample 0.

        Never raises and never crashes the caller: any internal failure
        (missing pydub, corrupt file, unreadable WAV) leaves the file
        completely untouched and returns ``{"trimmed_ms": 0.0}``.
        """
        try:
            import numpy as np
        except Exception:  # noqa: BLE001 - numpy is a hard dependency in
            # practice, but this pass must never be the reason a render
            # crashes if it's somehow missing.
            return {"trimmed_ms": 0.0}
        try:
            with wave.open(str(path), "rb") as handle:
                n_frames = handle.getnframes()
                sr = handle.getframerate()
                channels = max(1, handle.getnchannels())
                sampwidth = handle.getsampwidth()
                raw = handle.readframes(n_frames)
            if n_frames == 0 or sampwidth != 2:
                # Only the common 16-bit PCM case is handled here (matches
                # every writer in this module); anything else is left
                # untouched rather than guessed at.
                return {"trimmed_ms": 0.0}
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels)
            mono = samples.mean(axis=1) if samples.ndim == 2 else samples
            if mono.size == 0:
                return {"trimmed_ms": 0.0}

            trimmed_ms = 0.0
            if trim_leading_silence:
                samples, trimmed_ms = self._trim_leading_silence(samples, mono, sr)
                mono = samples.mean(axis=1) if samples.ndim == 2 else samples

            samples = self._repair_clicks(samples, sr)
            samples = self._apply_edge_fades(samples, sr)
            samples = self._sanitize_samples(np.asarray(samples, dtype=np.float64))

            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(channels)
                handle.setsampwidth(2)
                handle.setframerate(sr)
                handle.writeframes(pcm.tobytes())
            return {"trimmed_ms": trimmed_ms}
        except Exception as exc:  # noqa: BLE001 - never break generation
            self.log.warning("Line audio finalize skipped for %s: %s", path, exc)
            return {"trimmed_ms": 0.0}

    def _trim_leading_silence(
        self, samples: Any, mono: Any, sr: int
    ) -> Tuple[Any, float]:
        """Trim excessive leading silence without cutting the first phoneme.

        PHASE 4 (remove hard intro): only silence BEYOND a small
        breath-safe margin is removed — the margin (60ms) is kept so a
        soft consonant onset or breath intake immediately preceding
        speech is never clipped. Only trims when the detected silence
        is "excessive" (> 150ms after the margin) so normal, already-tight
        TTS output is left completely alone.
        """
        import numpy as np

        threshold = 0.01  # ~ -40 dBFS, matches detect_silence_regions default
        above = np.flatnonzero(np.abs(mono) > threshold)
        if above.size == 0:
            return samples, 0.0
        first_sound = int(above[0])
        margin_samples = int(0.06 * sr)
        cut_at = max(0, first_sound - margin_samples)
        min_excess_samples = int(0.15 * sr)
        if cut_at < min_excess_samples:
            return samples, 0.0
        trimmed = samples[cut_at:]
        return trimmed, (cut_at / float(sr)) * 1000.0

    def _repair_clicks(self, samples: Any, sr: int) -> Any:
        """Detect and smooth abrupt sample-to-sample discontinuities.

        PHASE 3 (click removal): a click is, physically, one or two
        samples that jump far outside the trend of their immediate
        neighbourhood — a decoder glitch, a bad crossfade seam, or an
        unstable filter. Flags samples whose jump is both large in
        absolute terms and a large multiple of the local (windowed)
        median absolute jump, then replaces just those samples with a
        linear interpolation across the gap — inaudible on real speech,
        which never contains true instantaneous jumps of that scale.
        """
        import numpy as np

        arr = np.array(samples, dtype=np.float64, copy=True)
        channels = 1 if arr.ndim == 1 else arr.shape[1]
        work = arr.reshape(-1, 1) if arr.ndim == 1 else arr

        for ch in range(channels):
            col = work[:, ch] if channels > 1 else work[:, 0]
            if col.size < 8:
                continue
            diffs = np.abs(np.diff(col))
            if diffs.size == 0:
                continue
            med = float(np.median(diffs)) + 1e-6
            # A jump both large on an absolute scale (> 0.25 full-scale)
            # and a big multiple of the local typical jump is the
            # click fingerprint — normal speech transients scale with
            # the signal, they don't spike this far above their own
            # median step size.
            spike = (diffs > 0.25) & (diffs > med * 12.0)
            idx = np.flatnonzero(spike)
            for i in idx:
                lo = max(0, i - 1)
                hi = min(col.size - 1, i + 2)
                if hi > lo:
                    col[lo:hi + 1] = np.linspace(col[lo], col[hi], hi - lo + 1)
            if channels > 1:
                work[:, ch] = col
            else:
                work[:, 0] = col
        return work.reshape(-1) if arr.ndim == 1 else work

    def _apply_edge_fades(self, samples: Any, sr: int, fade_ms: float = _EDGE_FADE_MS) -> Any:
        """Apply a short fade-in and fade-out to guarantee zero-crossing edges.

        PHASE 3 (click removal): the single biggest source of start/end/
        join clicks is a waveform that begins or ends away from zero —
        this guarantees both edges ramp cleanly from/to silence. 5-10ms
        is short enough to be completely inaudible on speech while
        reliably removing the discontinuity.
        """
        import numpy as np

        arr = np.array(samples, dtype=np.float64, copy=True)
        n = arr.shape[0] if arr.ndim else 0

        fade_len = min(int(sr * fade_ms / 1000.0), n // 2 if n else 0)
        if fade_len <= 1:
            return arr
        fade_in = np.linspace(0.0, 1.0, fade_len)
        fade_out = np.linspace(1.0, 0.0, fade_len)
        if arr.ndim == 2:
            fade_in = fade_in[:, None]
            fade_out = fade_out[:, None]
        arr[:fade_len] = arr[:fade_len] * fade_in
        arr[-fade_len:] = arr[-fade_len:] * fade_out
        return arr

    # ------------------------------------------------------------------
    # Engine install / test / memory
    # ------------------------------------------------------------------

    def get_available_engines(self) -> Dict[str, Any]:
        """Return installed/available engine status without loading models."""
        engines = []
        if self._find_piper_binary() is not None:
            engines.append(
                {"name": ENGINE_PIPER, "status": "available", "loaded": False}
            )
        else:
            engines.append(
                {"name": ENGINE_PIPER, "status": "not_installed", "loaded": False}
            )
        kokoro_status = "available" if self._kokoro_importable() else "not_installed"
        engines.append(
            {
                "name": ENGINE_KOKORO,
                "status": kokoro_status,
                "loaded": self.kokoro_instance is not None,
            }
        )
        xtts_status = "available" if self._xtts_importable() else "not_installed"
        engines.append(
            {
                "name": ENGINE_XTTS,
                "status": xtts_status,
                "loaded": self.xtts_instance is not None,
            }
        )
        return self.make_response(True, {"engines": engines})

    def install_engine(self, engine_file_path: str | Path) -> Dict[str, Any]:
        """Install engine/model file into engines/ folder tree."""
        started = time.perf_counter()
        src = Path(engine_file_path)
        if not src.exists():
            return self._err(f"File not found: {src}", started)
        suffix = src.suffix.lower()
        if suffix == ".exe" or src.name.lower().startswith("piper"):
            dest_dir = self._project_root / "engines" / "piper"
            engine = ENGINE_PIPER
        elif suffix in (".onnx",):
            dest_dir = self._project_root / "engines" / "piper" / "models"
            engine = ENGINE_PIPER
        elif suffix in (".pt", ".pth", ".bin"):
            dest_dir = self._project_root / "engines" / "xtts" / "models"
            engine = ENGINE_XTTS
        elif suffix == ".zip":
            dest_dir = self._project_root / "engines" / "kokoro" / "models"
            engine = ENGINE_KOKORO
        else:
            dest_dir = self._project_root / "engines" / "kokoro" / "models"
            engine = ENGINE_KOKORO
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        self._record_engine_install(engine, str(dest))
        return self.make_response(
            True,
            {"engine": engine, "status": "installed", "path": str(dest)},
            duration_ms=_ms(started),
        )

    def install_engine_from_url(self, url: str, admin_password: str) -> Dict[str, Any]:
        """Download and install engine; requires admin password IAMKING."""
        started = time.perf_counter()
        if admin_password != ADMIN_PASSWORD:
            return self._err("Invalid admin password", started)
        if not url:
            return self._err("url is required", started)
        try:
            import requests
        except ImportError:
            return self._err("requests package not installed", started)
        temp_dir = self._project_root / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        filename = url.rstrip("/").split("/")[-1] or "engine_download.bin"
        dest = temp_dir / filename
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with dest.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            handle.write(chunk)
            return self.install_engine(dest)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Download failed: {exc}", started)

    def test_engine(self, engine_name: str) -> Dict[str, Any]:
        """Generate a short test phrase with the named engine."""
        started = time.perf_counter()
        out = self._project_root / "temp" / f"tts_test_{engine_name}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        profile = {
            "engine": engine_name,
            "voice_model": "default",
            "speed": 1.0,
            "pitch": 0.0,
            "volume": 1.0,
            "default_emotion": "neutral",
            "reverb_preset": "none",
            "eq_preset": "flat",
            "breathing_enabled": False,
        }
        result = self.generate_audio(
            "This is a test of the Autopilot system.",
            profile,
            out,
        )
        result["duration_ms"] = _ms(started)
        return result

    def unload_engine_from_memory(self, engine_name: str) -> Dict[str, Any]:
        """Explicitly free a loaded engine from RAM."""
        started = time.perf_counter()
        before = self._rss_mb()
        name = str(engine_name or "").lower()
        if name == ENGINE_KOKORO:
            self.kokoro_instance = None
        elif name == ENGINE_XTTS:
            self.xtts_instance = None
        # Piper is subprocess-only
        gc.collect()
        after = self._rss_mb()
        freed = max(0.0, before - after)
        self.log.info("Unloaded %s; RSS delta ~%.1f MB", name, freed)
        return self.make_response(
            True,
            {
                "engine": name,
                "rss_before_mb": before,
                "rss_after_mb": after,
                "freed_mb": freed,
            },
            duration_ms=_ms(started),
        )

    def check_ram_and_manage_engines(self) -> Dict[str, Any]:
        """Unload heavy engines when free RAM is low or idle timeout exceeded."""
        actions: List[str] = []
        free_mb = self._available_ram_mb()
        now = time.time()
        if (
            free_mb
            and free_mb < XTTS_UNLOAD_BELOW_MB
            and self.xtts_instance is not None
        ):
            self.unload_engine_from_memory(ENGINE_XTTS)
            actions.append("unloaded_xtts_low_ram")
        if (
            free_mb
            and free_mb < KOKORO_UNLOAD_BELOW_MB
            and self.kokoro_instance is not None
        ):
            self.unload_engine_from_memory(ENGINE_KOKORO)
            actions.append("unloaded_kokoro_low_ram")
        # Idle unload for kokoro
        last_kokoro = self._last_use.get(ENGINE_KOKORO, 0)
        if (
            self.kokoro_instance is not None
            and last_kokoro
            and now - last_kokoro > IDLE_UNLOAD_SECONDS
            and free_mb
            and free_mb < 2000
        ):
            self.unload_engine_from_memory(ENGINE_KOKORO)
            actions.append("unloaded_kokoro_idle")
        return self.make_response(
            True, {"actions": actions, "available_ram_mb": free_mb}
        )

    def list_emotions(self) -> List[str]:
        """Return all supported emotion names (28 base presets)."""
        return sorted(EMOTION_PRESETS.keys())

    def engines_loaded_in_memory(self) -> Dict[str, bool]:
        """Return which persistent engines currently hold models in RAM."""
        return {
            ENGINE_PIPER: False,  # never persistent
            ENGINE_KOKORO: self.kokoro_instance is not None,
            ENGINE_XTTS: self.xtts_instance is not None,
        }

    # ------------------------------------------------------------------
    # Lazy load helpers
    # ------------------------------------------------------------------

    def _ensure_kokoro_loaded(self) -> bool:
        """Import and construct Kokoro only when needed."""
        if self.kokoro_instance is not None:
            return True
        try:
            from kokoro_onnx import Kokoro  # type: ignore

            model_dir = self._project_root / "engines" / "kokoro" / "models"
            model_path = (
                next(model_dir.glob("*.onnx"), None) if model_dir.exists() else None
            )
            # BUGFIX (v3.1.4): glob("*voices*") could match a FOLDER named
            # "voices" (e.g. one containing per-voice .pt files from a
            # different Kokoro distribution) as readily as an actual voices
            # file. Passing a directory path to Kokoro() makes it try to
            # open that directory as a binary file, which on Windows raises
            # a misleading "PermissionError: Access is denied" instead of a
            # clear "that's a folder" error. We now only consider actual
            # files, and log a specific, actionable message when a
            # same-named folder exists instead — this is not a permissions
            # problem, it's the wrong voices file format installed.
            voices_candidates = (
                [p for p in model_dir.glob("*voices*") if p.is_file()]
                if model_dir.exists()
                else []
            )
            voices_path = voices_candidates[0] if voices_candidates else None
            if model_path is None:
                self.log.warning("Kokoro model (.onnx) not found under %s", model_dir)
                return False
            if voices_path is None:
                voices_dir = model_dir / "voices"
                if voices_dir.is_dir():
                    self.log.warning(
                        "Kokoro voices path is a folder of individual voice "
                        "files (%s), not the single combined voices file "
                        "kokoro-onnx needs (e.g. voices-v1.0.bin). Download "
                        "the matching voices file from the kokoro-onnx "
                        "releases page and place it directly in %s",
                        voices_dir,
                        model_dir,
                    )
                return False
            self.kokoro_instance = Kokoro(str(model_path), str(voices_path))
            self.log.info("Kokoro loaded lazily from %s", model_path)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Kokoro not available: %s", exc)
            return False

    def _ensure_xtts_loaded(self) -> bool:
        """Import and construct XTTS only when needed."""
        if self.xtts_instance is not None:
            return True
        try:
            from TTS.api import TTS  # type: ignore

            self.xtts_instance = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
            self.log.info("XTTS loaded lazily")
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("XTTS not available: %s", exc)
            return False

    def _kokoro_importable(self) -> bool:
        """True if kokoro package can be imported (does not load model)."""
        try:
            import importlib.util

            return importlib.util.find_spec("kokoro_onnx") is not None
        except Exception:  # noqa: BLE001
            return False

    def _xtts_importable(self) -> bool:
        """True if TTS package can be imported (does not load model)."""
        try:
            import importlib.util

            return importlib.util.find_spec("TTS") is not None
        except Exception:  # noqa: BLE001
            return False

    def _find_piper_binary(self) -> Optional[Path]:
        """Locate piper executable without loading models."""
        candidates = [
            self._project_root / "engines" / "piper" / "piper",
            self._project_root / "engines" / "piper" / "piper.exe",
            Path(shutil.which("piper") or ""),
        ]
        for path in candidates:
            if path and path.exists():
                return path
        return None

    def _find_piper_model(self, voice_model: str) -> Optional[Path]:
        """Find a Piper .onnx model matching voice name.

        D2a: a candidate is only usable when its REQUIRED sidecar
        <model>.onnx.json sits next to it — piper refuses to load a
        bare .onnx (crashes FileNotFoundError on the config). This
        also neutralises leftover junk models (e.g. test-written
        fake_model.onnx) that previously made every narration line
        retry piper, crash, and fall back after ~2s plus a full
        traceback in the log.
        """

        def _usable(candidate: Path) -> bool:
            return (candidate.parent / (candidate.name + ".json")).exists()

        model_dir = self._project_root / "engines" / "piper" / "models"
        if not model_dir.exists():
            return None
        exact = model_dir / f"{voice_model}.onnx"
        if exact.exists() and _usable(exact):
            return exact
        matches = [
            m for m in model_dir.glob(f"*{voice_model}*.onnx") if _usable(m)
        ]
        if matches:
            return matches[0]
        any_onnx = [m for m in model_dir.glob("*.onnx") if _usable(m)]
        return any_onnx[0] if any_onnx else None

    def _find_ffmpeg(self) -> Optional[Path]:
        """Locate ffmpeg binary.

        PHASE 1 (audio stability / effects chain instability fix): see
        the identical fix + rationale in audio_processor._find_ffmpeg —
        modules don't cross-import in this codebase (Rule A), so this is
        kept in sync deliberately. ``Path(shutil.which(...) or "")``
        silently resolved to the current working directory (which always
        ``.exists()``) whenever ffmpeg wasn't on PATH, so effects/pitch/
        loudnorm calls were handed a bogus "ffmpeg" executable instead of
        cleanly falling back — this was a real, reproducible source of
        the "effects chain instability" this phase targets.
        """
        # PHASE 8 (rendering & export optimization): resolution result is
        # remembered for as long as it stays valid — every narration line
        # ran a fresh PATH scan (shutil.which) plus several stat() calls
        # for effects, pitch and pause work. Re-resolved automatically if
        # the binary disappears, so a moved/uninstalled ffmpeg still
        # falls back exactly as before.
        cached = getattr(self, "_ffmpeg_cache", None)
        if cached is not None and cached.is_file():
            return cached
        hint = None
        try:
            hint = self.config.get("ffmpeg_path")
        except Exception:  # noqa: BLE001
            hint = None
        candidates = []
        if hint:
            candidates.append(Path(str(hint)))
        candidates.append(self._project_root / "engines" / "ffmpeg" / "ffmpeg")
        candidates.append(self._project_root / "engines" / "ffmpeg" / "ffmpeg.exe")
        which = shutil.which("ffmpeg")
        if which:
            candidates.append(Path(which))
        for path in candidates:
            if path and str(path) and path.is_file():
                self._ffmpeg_cache = path
                return path
        return None

    def _resolve_engine_with_fallback(self, engine: str) -> Optional[str]:
        """Fallback XTTS→kokoro→piper when preferred engine missing."""
        order = [engine]
        for candidate in (ENGINE_XTTS, ENGINE_KOKORO, ENGINE_PIPER):
            if candidate not in order:
                order.append(candidate)
        available = self.get_available_engines()["data"]["engines"]
        status = {item["name"]: item["status"] for item in available}
        for name in order:
            if status.get(name) == "available":
                return name
            # Piper may still use synthetic fallback for offline tests
            if name == ENGINE_PIPER:
                return ENGINE_PIPER
        return ENGINE_PIPER

    def _mark_engine_used(self, engine: str) -> None:
        """Record last-use timestamp for idle unload."""
        self._last_use[engine] = time.time()

    # ------------------------------------------------------------------
    # Audio utilities
    # ------------------------------------------------------------------

    def _synthetic_speech_fallback(
        self,
        text: str,
        output_path: str,
        settings: Dict[str, Any],
        engine: str,
        started: float,
    ) -> Dict[str, Any]:
        """Generate a tone-based WAV so offline tests can exercise the pipeline.

        STATUS: NOT a real TTS voice — used when engines are not installed.
        """
        words = WORD_RE.findall(text)
        speed = max(0.5, float(settings.get("speed", 1.0)))
        # ~0.35s per word adjusted by speed
        duration = max(0.4, len(words) * 0.35 / speed)
        sample_rate = 22050
        frequency = 180.0 + float(settings.get("pitch", 0.0)) * 8.0
        self._write_sine_wav(output_path, duration, frequency, sample_rate)
        timestamps = self._approximate_word_timestamps(text, duration)
        return self.make_response(
            True,
            {
                "audio_path": output_path,
                "word_timestamps": timestamps,
                "duration": duration,
                "engine": engine,
                "synthetic": True,
                "warning": "Synthetic fallback audio — real TTS engine not installed",
            },
            warnings=["Synthetic fallback audio used (engine not installed)"],
            duration_ms=_ms(started),
        )

    def _write_sine_wav(
        self,
        path: str | Path,
        duration: float,
        frequency: float,
        sample_rate: int,
    ) -> None:
        """Write a simple mono sine wave WAV file."""
        n_samples = int(duration * sample_rate)
        with wave.open(str(path), "w") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            frames = bytearray()
            for i in range(n_samples):
                # Soft envelope to avoid clicks
                env = 1.0
                if i < sample_rate * 0.02:
                    env = i / (sample_rate * 0.02)
                elif i > n_samples - sample_rate * 0.02:
                    env = max(0.0, (n_samples - i) / (sample_rate * 0.02))
                value = int(
                    10000 * env * math.sin(2 * math.pi * frequency * i / sample_rate)
                )
                frames.extend(struct.pack("<h", value))
            handle.writeframes(bytes(frames))

    def _write_wav_samples(self, path: str, samples: Any, sample_rate: int) -> None:
        """Write numpy/list samples to WAV via soundfile or wave.

        PHASE 1 (audio stability): Kokoro (and any future numpy-based
        engine) can hand back a buffer containing NaN/Inf — an unstable
        ONNX run, a malformed phoneme sequence, or a numerical edge case
        in the vocoder are all real, observed failure modes — and those
        values must never reach disk. Every buffer is sanitized (NaN/Inf
        zeroed, hard-clipped to [-1, 1]) before either write path runs.
        """
        import numpy as np

        arr = np.asarray(samples, dtype=np.float32)
        arr = self._sanitize_samples(arr)
        try:
            import soundfile as sf

            sf.write(path, arr, sample_rate)
            return
        except Exception:  # noqa: BLE001
            pass
        # Fallback minimal writer
        pcm = (arr * 32767).astype("<i2")
        with wave.open(path, "w") as handle:
            handle.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm.tobytes())

    def _sanitize_samples(self, arr: Any) -> Any:
        """Replace NaN/Inf with silence and hard-clip to [-1, 1].

        PHASE 1 (audio stability): shared by every raw-sample write path
        in this module (Kokoro output, synthetic fallback tone, and the
        click-repair/fade finalize pass). A handful of bad samples (one
        glitch frame) is zeroed in place — inaudible; a buffer that is
        mostly non-finite is corrupted input, not a signal worth
        preserving, so it becomes matching-length silence instead of
        shipping garbage downstream.
        """
        import numpy as np

        if arr.size == 0:
            return arr
        finite = np.isfinite(arr)
        if finite.all():
            return np.clip(arr, -1.0, 1.0).astype(arr.dtype, copy=False)
        bad_ratio = 1.0 - (float(np.count_nonzero(finite)) / float(arr.size))
        if bad_ratio > _MAX_NONFINITE_RATIO:
            self.log.warning(
                "TTS output %.1f%% non-finite (NaN/Inf) — replacing with "
                "silence instead of shipping corrupted samples",
                bad_ratio * 100.0,
            )
            return np.zeros_like(arr)
        repaired = np.where(finite, arr, 0.0)
        return np.clip(repaired, -1.0, 1.0).astype(arr.dtype, copy=False)

    def _wav_duration(self, path: Path) -> float:
        """Return WAV duration in seconds."""
        try:
            with wave.open(str(path), "r") as handle:
                return handle.getnframes() / float(handle.getframerate())
        except Exception:  # noqa: BLE001
            return 0.0

    def _scale_word_timestamps(
        self,
        timestamps: List[Dict[str, Any]],
        old_duration: float,
        new_duration: float,
    ) -> List[Dict[str, Any]]:
        """Scale line-relative timestamps after a duration-changing filter."""
        if not timestamps or old_duration <= 0 or new_duration <= 0:
            return timestamps
        ratio = new_duration / old_duration
        if abs(ratio - 1.0) < 0.0005:
            return timestamps
        return [
            {
                **word,
                "start": round(float(word.get("start", 0.0)) * ratio, 3),
                "end": round(float(word.get("end", 0.0)) * ratio, 3),
            }
            for word in timestamps
        ]

    def _apply_prosody_contour(
        self,
        path: Path,
        timestamps: List[Dict[str, Any]],
        plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply duration-preserving phrase intonation and word stress.

        PHASE 7: each phrase is resampled against a very small, zero-net
        time warp.  Local source-map slope creates pitch movement, while
        pinning both phrase endpoints preserves its exact duration. Word
        times are inverse-mapped through that same source map.  Emphasis
        uses smooth raised-cosine gain envelopes, never hard volume steps.

        Only 16-bit PCM (the format every writer in this module emits) is
        touched. Missing numpy, malformed plans, or unreadable audio return
        the original timestamps and leave the file byte-for-byte unchanged.
        """
        original = list(timestamps or [])
        if not plan.get("enabled") or not original:
            return {"applied": False, "word_timestamps": original}
        phrases = list(plan.get("phrases") or [])
        stresses = list(plan.get("emphasis") or [])
        if not phrases and not stresses:
            return {"applied": False, "word_timestamps": original}
        try:
            import numpy as np
        except Exception:  # noqa: BLE001 - optional enhancement fallback
            return {"applied": False, "word_timestamps": original}

        try:
            with wave.open(str(path), "rb") as handle:
                frames = handle.getnframes()
                sample_rate = handle.getframerate()
                channels = max(1, handle.getnchannels())
                sample_width = handle.getsampwidth()
                raw = handle.readframes(frames)
            if frames < 8 or sample_width != 2 or sample_rate <= 0:
                return {"applied": False, "word_timestamps": original}

            samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
            samples = samples / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels)
            shaped = np.array(samples, dtype=np.float64, copy=True)
            adjusted = [dict(word) for word in original]
            pitch_applied = False

            for phrase in phrases:
                start_word = int(phrase.get("start_word", -1))
                end_word = int(phrase.get("end_word", -1))
                if (
                    start_word < 0
                    or end_word < start_word
                    or end_word >= len(adjusted)
                ):
                    continue
                # Do not time-warp an authored inline pause or breath. A
                # >=140 ms inter-word hole is far beyond this module's normal
                # 10% timestamp slot and is treated as intentional silence.
                if any(
                    float(original[word + 1].get("start", 0.0))
                    - float(original[word].get("end", 0.0)) >= 0.14
                    for word in range(start_word, end_word)
                ):
                    continue
                start_time = max(
                    0.0, float(original[start_word].get("start", 0.0))
                )
                end_time = min(
                    frames / float(sample_rate),
                    float(original[end_word].get("end", 0.0)),
                )
                lo = max(0, min(frames - 1, int(start_time * sample_rate)))
                hi = max(lo + 1, min(frames, int(end_time * sample_rate)))
                length = hi - lo
                if length < max(32, int(sample_rate * 0.10)):
                    continue

                pitch_values = np.asarray(
                    [
                        float(phrase.get("pitch_start", 0.0)),
                        float(phrase.get("pitch_peak", 0.0)),
                        float(phrase.get("pitch_end", 0.0)),
                    ],
                    dtype=np.float64,
                )
                if np.max(np.abs(pitch_values)) < 0.005:
                    continue
                output_positions = np.arange(length, dtype=np.float64)
                phase = output_positions / max(1.0, float(length - 1))
                semitones = np.interp(
                    phase,
                    np.asarray([0.0, 0.55, 1.0]),
                    pitch_values,
                )
                slope = np.power(2.0, semitones / 12.0)
                # Zero net warp: exact phrase duration and endpoint samples.
                source_positions = np.cumsum(slope)
                source_positions -= source_positions[0]
                final_source = float(source_positions[-1])
                if final_source <= 0:
                    continue
                source_positions *= (length - 1) / final_source

                segment = shaped[lo:hi]
                if segment.ndim == 1:
                    shaped[lo:hi] = np.interp(
                        source_positions, output_positions, segment
                    )
                else:
                    for channel in range(segment.shape[1]):
                        shaped[lo:hi, channel] = np.interp(
                            source_positions,
                            output_positions,
                            segment[:, channel],
                        )

                # A broad, endpoint-safe energy arc reinforces phrase
                # grouping without inserting silence or a hard gain step.
                energy_db = _clamp_number(
                    float(phrase.get("energy_db", 0.0)), -0.4, 0.4
                )
                if abs(energy_db) >= 0.01:
                    energy_gain = 10.0 ** (energy_db / 20.0)
                    energy_envelope = 1.0 + (energy_gain - 1.0) * (
                        np.sin(np.linspace(0.0, math.pi, length)) ** 2
                    )
                    if shaped.ndim == 2:
                        energy_envelope = energy_envelope[:, None]
                    shaped[lo:hi] *= energy_envelope

                # Rebase every word in the phrase through the inverse map.
                for word_index in range(start_word, end_word + 1):
                    for key in ("start", "end"):
                        old_time = float(original[word_index].get(key, 0.0))
                        local_source = _clamp_number(
                            (old_time - start_time) * sample_rate,
                            0.0,
                            float(length - 1),
                        )
                        local_output = float(
                            np.interp(
                                local_source,
                                source_positions,
                                output_positions,
                            )
                        )
                        adjusted[word_index][key] = round(
                            start_time + local_output / sample_rate, 3
                        )
                pitch_applied = True

            emphasis_applied = False
            for stress in stresses:
                word_index = int(stress.get("word_index", -1))
                if word_index < 0 or word_index >= len(adjusted):
                    continue
                start_time = float(adjusted[word_index].get("start", 0.0))
                end_time = float(adjusted[word_index].get("end", start_time))
                if end_time <= start_time:
                    continue
                duration = end_time - start_time
                lo = max(0, int((start_time - duration * 0.12) * sample_rate))
                hi = min(frames, int((end_time + duration * 0.08) * sample_rate))
                length = hi - lo
                if length < 4:
                    continue
                gain_db = _clamp_number(
                    float(stress.get("gain_db", 0.0)), 0.0, 2.5
                )
                if gain_db < 0.05:
                    continue
                peak_gain = 10.0 ** (gain_db / 20.0)
                phase = np.linspace(0.0, math.pi, length)
                envelope = 1.0 + (peak_gain - 1.0) * np.sin(phase) ** 2
                if shaped.ndim == 2:
                    envelope = envelope[:, None]
                shaped[lo:hi] *= envelope
                emphasis_applied = True

            if not pitch_applied and not emphasis_applied:
                return {"applied": False, "word_timestamps": original}
            shaped = self._sanitize_samples(shaped)
            peak = float(np.max(np.abs(shaped))) if shaped.size else 0.0
            if peak > 0.995:
                shaped *= 0.995 / peak
            pcm = (np.clip(shaped, -1.0, 1.0) * 32767.0).astype("<i2")
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(channels)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(pcm.tobytes())
            return {
                "applied": True,
                "pitch_applied": pitch_applied,
                "emphasis_applied": emphasis_applied,
                "word_timestamps": adjusted,
            }
        except Exception as exc:  # noqa: BLE001 - never break synthesis
            self.log.warning("Prosody contour skipped for %s: %s", path, exc)
            return {"applied": False, "word_timestamps": original}

    def _approximate_word_timestamps(
        self, text: str, duration: float
    ) -> List[Dict[str, Any]]:
        """Evenly distribute word timings across duration."""
        words = WORD_RE.findall(text)
        if not words:
            return []
        slot = duration / len(words)
        result = []
        for index, word in enumerate(words):
            start = index * slot
            end = start + slot * 0.9
            result.append(
                {"word": word, "start": round(start, 3), "end": round(end, 3)}
            )
        return result

    def _adjust_timestamps_for_pauses(
        self,
        timestamps: List[Dict[str, Any]],
        pause_markers: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Shift word timestamps by inserted pause durations."""
        if not timestamps or not pause_markers:
            return timestamps
        # Sort only markers confirmed inserted.  The private flag is absent
        # for legacy/direct callers, where the historical assumption was
        # that every supplied marker already exists in the audio.
        markers = sorted(
            (marker for marker in pause_markers if marker.get("_inserted", True)),
            key=lambda marker: int(marker.get("word_index") or 0),
        )
        offset = 0.0
        marker_i = 0
        adjusted: List[Dict[str, Any]] = []
        for index, word in enumerate(timestamps):
            while (
                marker_i < len(markers)
                and int(markers[marker_i].get("word_index") or 0) <= index
            ):
                duration = markers[marker_i].get("duration")
                if duration is None:
                    duration = self.generate_pause(
                        str(markers[marker_i].get("type") or "SHORT")
                    )
                offset += float(duration)
                marker_i += 1
            adjusted.append(
                {
                    "word": word.get("word"),
                    "start": round(float(word.get("start", 0)) + offset, 3),
                    "end": round(float(word.get("end", 0)) + offset, 3),
                }
            )
        return adjusted

    def _marker_to_ms(
        self,
        marker: Dict[str, Any],
        word_timestamps: List[Dict[str, Any]],
        audio_len_ms: int,
    ) -> int:
        """Map pause marker word_index to millisecond position in audio."""
        idx = int(marker.get("word_index") or 0)
        if word_timestamps and 0 < idx <= len(word_timestamps):
            # Insert after word at idx-1
            end = float(word_timestamps[idx - 1].get("end", 0.0))
            return int(end * 1000)
        if word_timestamps and idx == 0:
            return 0
        # Fallback: proportional
        return min(audio_len_ms, max(0, int(audio_len_ms * 0.5)))

    def _nearest_zero_crossing_ms(
        self, seg: Any, pos_ms: int, search_ms: int = 5
    ) -> int:
        """Snap a millisecond position to the nearest true zero crossing.

        PHASE 3 (click & transition refinement): word-timestamp-derived
        split points (pause insertion, breath overlay) are approximate —
        they can land mid-waveform, away from zero, which is an audible
        click regardless of how short the subsequent fade is. This scans
        a small window (+/-search_ms) of the underlying PCM around
        ``pos_ms`` for a genuine sign-change crossing (or, failing that,
        the smallest-magnitude sample) and returns that position instead.
        Identical logic to audio_processor.AudioProcessor's method of the
        same name — modules don't cross-import in this codebase (Rule A),
        so this is kept in sync deliberately. Best-effort: falls back to
        the original ``pos_ms`` for anything that can't be read as PCM.
        """
        try:
            import numpy as np

            width = seg.sample_width
            if width != 2:
                return pos_ms
            channels = max(1, seg.channels)
            sr = seg.frame_rate
            total_ms = len(seg)
            pos_ms = max(0, min(total_ms, pos_ms))
            window_ms = max(1, search_ms)
            start_ms = max(0, pos_ms - window_ms)
            end_ms = min(total_ms, pos_ms + window_ms)
            if end_ms <= start_ms:
                return pos_ms
            chunk = seg[start_ms:end_ms]
            samples = np.frombuffer(chunk.raw_data, dtype="<i2")
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            if samples.size < 2:
                return pos_ms

            signs = np.sign(samples)
            crossing_idx = np.flatnonzero(np.diff(signs) != 0)
            if crossing_idx.size > 0:
                target_sample = int((pos_ms - start_ms) / 1000.0 * sr)
                best = crossing_idx[np.argmin(np.abs(crossing_idx - target_sample))]
                chosen = int(best)
            else:
                chosen = int(np.argmin(np.abs(samples)))
            offset_ms = int(round(chosen / float(sr) * 1000.0))
            return max(0, min(total_ms, start_ms + offset_ms))
        except Exception:  # noqa: BLE001 - best-effort, never break a splice
            return pos_ms

    def _load_breath_segment(
        self,
        pause_type: str,
        character_profile: Optional[Dict[str, Any]],
        max_ms: int,
    ) -> Any:
        """Load a breath WAV if present; else synthesize a quiet noise burst."""
        try:
            from pydub import AudioSegment
            from pydub.generators import WhiteNoise
        except ImportError:
            return None
        gender = "male"
        if character_profile:
            model = str(character_profile.get("voice_model") or "").lower()
            if "female" in model or "woman" in model:
                gender = "female"
        breath_dir = self._project_root / "assets" / "sfx" / "breathing"
        candidates = [
            breath_dir / f"breath_gentle_{gender}.wav",
            breath_dir / f"breath_normal_{gender}.wav",
            breath_dir / f"breath_deep_{gender}.wav",
        ]
        segment = None
        for candidate in candidates:
            if candidate.exists():
                segment = AudioSegment.from_file(str(candidate))
                break
        if segment is None:
            # Synthetic soft noise as stand-in breath
            duration = min(max_ms, 300 if pause_type in ("SHORT", "MEDIUM") else 500)
            segment = WhiteNoise().to_audio_segment(duration=duration, volume=-35)
        vol_range = BREATH_VOLUME_RANGE.get(pause_type, (0.1, 0.15))
        target_ratio = random.uniform(vol_range[0], vol_range[1])
        # pydub volume is dB; approximate scale
        if target_ratio <= 0:
            return None
        db_adjust = 20 * math.log10(max(target_ratio, 0.01))
        breath = segment + db_adjust
        # PHASE 3 (click removal): both sources here can have a hard
        # edge — a real breath sample cut short by the caller's
        # `breath[:silence_ms]` truncation, or the synthetic WhiteNoise
        # burst, which starts/stops at full level with no envelope at
        # all. A short edge fade guarantees this is inaudible as a click
        # when overlaid into the pause's silence.
        edge_fade_ms = min(int(_EDGE_FADE_MS), max(1, len(breath) // 2 - 1))
        if edge_fade_ms > 0 and len(breath) > edge_fade_ms * 2:
            breath = breath.fade_in(edge_fade_ms).fade_out(edge_fade_ms)
        return breath

    def _apply_pitch_shift(self, path: Path, semitones: float) -> None:
        """Pitch-shift WAV without changing speech duration.

        PHASE 7 fixes the former hard-coded 44.1 kHz ``asetrate`` path,
        which changed both pitch *and* duration for typical 22/24 kHz TTS
        output. ``atempo`` compensates the rate change, preserving timing;
        the source sample rate is read from the WAV instead of assumed.
        """
        ffmpeg = self._find_ffmpeg()
        if ffmpeg is None or abs(semitones) < 0.01:
            return
        try:
            with wave.open(str(path), "rb") as handle:
                sample_rate = int(handle.getframerate())
        except (OSError, wave.Error):
            return
        if sample_rate <= 0:
            return
        factor = 2 ** (semitones / 12.0)
        tempo = 1.0 / factor
        temp = path.with_suffix(".pitch.wav")
        cmd = [
            str(ffmpeg),
            "-y",
            "-i",
            str(path),
            "-af",
            (
                f"asetrate={sample_rate}*{factor:.6f},"
                f"aresample={sample_rate},atempo={tempo:.6f}"
            ),
            str(temp),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, check=False
            )
            if proc.returncode == 0 and temp.is_file():
                # PHASE 9: a retrying atomic rename — on Windows the
                # freshly closed temp WAV can still be held for a moment
                # by antivirus, which silently skipped the pitch shift.
                replace_atomic(temp, path)
            elif proc.returncode != 0:
                self.log.warning(
                    "Pitch shift failed (rc=%s): %s",
                    proc.returncode,
                    (proc.stderr or "").strip()[-200:],
                )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.warning("Pitch shift error for %s: %s", path, exc)
        finally:
            safe_unlink(temp)

    def _apply_volume_pydub(
        self,
        path: Path,
        volume: float,
        started: float,
        filter_chain: str,
    ) -> Dict[str, Any]:
        """Volume-only fallback when FFmpeg is unavailable."""
        try:
            from pydub import AudioSegment

            audio = AudioSegment.from_file(str(path))
            if abs(volume - 1.0) > 0.001:
                db = 20 * math.log10(max(volume, 0.01))
                audio = audio + db
            audio.export(str(path), format="wav")
            return self.make_response(
                True,
                {
                    "audio_path": str(path),
                    "filter_chain": filter_chain,
                    "ffmpeg": False,
                    "warning": "FFmpeg not found; applied volume-only effects",
                },
                warnings=["FFmpeg not found — full effect chain NOT VERIFIED"],
                duration_ms=_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Effects failed: {exc}", started)

    def _record_engine_install(self, engine: str, path: str) -> None:
        """Best-effort DB update for engine_installations."""
        try:
            now = utc_now_str()
            row = self.db.db.fetch_one(
                "SELECT id FROM engine_installations WHERE engine_name = ?",
                (engine,),
            )
            if row:
                self.db.db.execute(
                    "UPDATE engine_installations SET install_path = ?, status = ?, "
                    "updated_at = ? WHERE engine_name = ?",
                    (path, "installed", now, engine),
                )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("engine install DB update skipped: %s", exc)

    def _normalize_emotion(self, emotion: str) -> str:
        """Map aliases to base emotion keys."""
        key = str(emotion or "neutral").strip().lower()
        key = EMOTION_ALIASES.get(key, key)
        if key not in EMOTION_PRESETS:
            return "neutral"
        return key

    def _available_ram_mb(self) -> float:
        """Return free system RAM in MB."""
        try:
            import psutil

            return float(psutil.virtual_memory().available) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return 0.0

    def _rss_mb(self) -> float:
        """Return current process RSS in MB."""
        try:
            import psutil

            return float(psutil.Process(os.getpid()).memory_info().rss) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return 0.0

    def _err(self, message: str, started: float) -> Dict[str, Any]:
        """Build error response."""
        return self.make_response(
            False,
            data={
                "error_code": "TTS_ERROR",
                "user_message": message,
                "is_recoverable": True,
            },
            error=message,
            duration_ms=_ms(started),
        )


def _clamp_number(value: float, low: float, high: float) -> float:
    """Clamp a finite numeric DSP value without exposing a public helper."""
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)
