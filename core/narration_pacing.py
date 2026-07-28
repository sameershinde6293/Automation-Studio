"""Natural pauses and human pacing for narration (PHASE 6).

PHASE 6 (natural pauses & human pacing): every boundary between two
narration lines used to receive exactly the same flat gap
(``core_engine._PAUSE_BETWEEN_LINES`` = 0.25s, mirrored in
``audio_processor.build_narration_track``'s ``pause_seconds`` default and
in ``timeline_engine.calculate_scene_durations``). A perfectly constant
gap is the single loudest "this is a machine reading" cue in an
otherwise good narration: real speakers pause differently after a comma
than after a full stop, hang slightly longer on a question, take a real
breath every so often, and never hit the same interval twice in a row.

This module is the ONE source of truth for that timing. It lives in
``core/`` (like ``core.time_helper`` / ``core.errors``) precisely so
every consumer can import it without breaking RULE A ("modules never
import each other"):

  * ``modules.tts_engine_manager`` — exposes it through
    ``plan_narration_pauses()`` for callers that only hold the TTS seam.
  * ``core.core_engine`` — plans the render's gaps once, uses them for
    word-timestamp offset accumulation, and hands the same plan to the
    mixer.
  * ``modules.audio_processor`` — inserts exactly those gaps when the
    narration track is assembled.
  * ``modules.timeline_engine`` — recomputes the identical plan from the
    same dialogue rows so scene boundaries stay aligned with the real
    audio.

DESIGN RULES (all four consumers depend on these):

1. PURE AND STDLIB-ONLY. No I/O, no numpy/pydub, no service container.
   Cheap enough to call once per render (O(number of lines), a list of
   floats) — never a measurable cost next to synthesis or encoding.

2. DETERMINISTIC. The humanizing variation is derived from a CRC32 of
   the line text plus its position — NOT ``random`` — because the same
   plan is recomputed independently in ``core_engine`` (audio offsets)
   and in ``timeline_engine`` (scene durations). Anything non-repeatable
   there would silently desynchronise images/subtitles from the voice.
   ``random.random()`` (used for in-line pause variation since B.4) is
   deliberately NOT used here for exactly that reason.

3. BACKWARD COMPATIBLE. ``natural_pauses_enabled = false`` in
   ``config/app_settings.json`` returns a flat plan of the legacy 0.25s
   gap, restoring the exact Phase 1-5 behaviour. Every consumer also
   falls back to the flat constant if planning raises for any reason.

4. NON-REGRESSIVE. This module only decides how much SILENCE goes
   BETWEEN two already-finished narration clips. It never touches the
   clips themselves, so Phase 1 stability, Phase 2 voice effects,
   Phase 3 click/transition work, Phase 4 hard-intro trimming and Phase
   5 paragraph batching all keep working unchanged.
"""

from __future__ import annotations

import re
import zlib
from typing import Any, Dict, List, Optional, Sequence

# ----------------------------------------------------------------------
# Configurable values (overridable from config/app_settings.json)
# ----------------------------------------------------------------------

# Legacy flat gap. Still the anchor for the whole scale and the exact
# value returned when natural pacing is disabled — matches
# core_engine._PAUSE_BETWEEN_LINES and build_narration_track's default.
LEGACY_PAUSE_SECONDS: float = 0.25

# app_settings keys -> internal config keys. Only these six are exposed
# as user settings; everything finer-grained lives in the tables below,
# where it is easy to find and tune in one place (same convention as
# modules/tts_presets.py for the voice/effect presets).
SETTING_KEYS: Dict[str, str] = {
    "natural_pauses_enabled": "enabled",
    "narration_pause_base_seconds": "base_seconds",
    "narration_pause_min_seconds": "min_seconds",
    "narration_pause_max_seconds": "max_seconds",
    "narration_pause_jitter": "jitter_ratio",
    "narration_breath_interval_seconds": "breath_interval_seconds",
}

PACING_DEFAULTS: Dict[str, Any] = {
    # Master switch. False -> flat LEGACY_PAUSE_SECONDS everywhere.
    "enabled": True,
    # Anchor gap; also the fallback for an unclassifiable boundary.
    "base_seconds": LEGACY_PAUSE_SECONDS,
    # Absolute clamps. No planned gap ever leaves this range, whatever
    # the punctuation/emotion/author combination asks for — this is the
    # "never excessive silence, never a swallowed beat" backstop.
    "min_seconds": 0.05,
    "max_seconds": 2.50,
    # Humanising variation, +/- this fraction of the computed gap.
    # Deterministic (CRC32-derived), see module docstring rule 2.
    "jitter_ratio": 0.12,
    # Breathing: after this much continuous speech without a gap of at
    # least breath_min_gap_seconds, the next boundary is widened to at
    # least breath_gap_seconds so the narrator has somewhere to breathe.
    "breath_interval_seconds": 14.0,
    "breath_gap_seconds": 0.34,
    "breath_min_gap_seconds": 0.26,
    # A new speaker is a real turn-taking beat; a new scene is a bigger
    # structural one. Both scale the punctuation-derived gap.
    "speaker_change_multiplier": 1.22,
    "scene_change_multiplier": 1.35,
    # PHASE 5 interaction: inside a paragraph batch the TTS engine has
    # already produced its own prosodic pause at the sentence boundary
    # (the punctuation is read by the engine), and split_paragraph_audio
    # keeps it in the clip tails. Adding a full gap on top would stack
    # two pauses into one unnaturally long hole, so paragraph-internal
    # boundaries only get this fraction of the planned gap.
    "paragraph_internal_ratio": 0.55,
    # Anti-stacking: the line's own text already ended with an authored
    # in-line pause tag ([PAUSE:LONG], [SILENCE=2s], ...) which
    # insert_pauses_into_audio has ALREADY baked into the clip. Only a
    # clean join is needed on top of it, never another full gap.
    "stacked_pause_seconds": 0.08,
    # Anti-robotic rhythm: if a gap lands within uniformity_epsilon of
    # the previous one, nudge it away by uniformity_nudge (direction is
    # deterministic) so consecutive boundaries are never metronomic.
    "uniformity_epsilon": 0.015,
    "uniformity_nudge": 0.035,
    # Excessive-silence guard: a derived (not author-specified) gap may
    # not exceed this fraction of the spoken line it follows — a long
    # hole after a three-word line reads as a dropout, not a beat.
    "max_gap_to_speech_ratio": 0.90,
    # Same ~0.35s/word estimate used by core_engine's per-line QA check
    # and tts_presets' paragraph batching — used only when a line has no
    # measured duration yet (pre-synthesis planning).
    "seconds_per_word_estimate": 0.35,
}

# Gap after a line, by how that line ENDS. These are inter-line silences
# in a continuous read, so they are deliberately tighter than the in-line
# dramatic beats in tts_presets.PAUSE_BASE_DURATIONS (which serve a
# different purpose: an authored [PAUSE:TYPE] beat inside one utterance).
PUNCTUATION_PAUSE_SECONDS: Dict[str, float] = {
    "paragraph": 0.62,    # explicit paragraph/section end
    "ellipsis": 0.52,     # "..." / "…" — trailing off, hangs longest
    "question": 0.40,     # "?" — a question is left sitting for a beat
    "exclamation": 0.36,  # "!"
    "sentence": 0.34,     # "."
    "colon": 0.30,        # ":" — leads into what follows
    "semicolon": 0.28,    # ";"
    "dash": 0.28,         # "-" / "—" — interruption, stays snappy
    "comma": 0.20,        # "," — the clause is not finished
    "none": 0.12,         # no terminal punctuation: sentence continues
}

# Author intent from the dialogue_lines.pause_after column.
#
# "short" is the SCHEMA DEFAULT (database/schema.sql, file_parser and
# core_engine all fall back to it), so it means "nothing was specified"
# and must NOT override the punctuation-derived length — otherwise every
# unannotated line in every existing script would collapse back to one
# flat value, which is exactly the robotic rhythm this phase removes.
# Every other label was typed deliberately by a script author and IS
# honoured: "none" runs straight on, "micro" clips the gap short, and
# medium/long/dramatic are real narrative beats.
AUTHORED_PAUSE_SECONDS: Dict[str, float] = {
    "none": 0.0,
    "micro": 0.16,
    "short": 0.30,
    "medium": 0.70,
    "long": 1.20,
    "dramatic": 1.90,
}
# Labels that override punctuation (i.e. every label except the
# ambiguous schema default). "none" is handled separately — it returns
# immediately, since no multiplier should ever reopen a gap the author
# explicitly closed.
EXPLICIT_AUTHORED_LABELS = frozenset({"micro", "medium", "long", "dramatic"})
# Deliberate beats that are additionally exempt from the
# excessive-silence guard: a one-word line CAN warrant a long pause when
# the script explicitly asks for one.
STRONG_AUTHORED_LABELS = frozenset({"medium", "long", "dramatic"})

# Emotion-aware pacing. Distinct from (and gentler than)
# tts_presets.PAUSE_EMOTION_MULTIPLIERS on purpose: that table scales
# authored in-line dramatic beats, this one scales the silence between
# consecutive narration lines, where the same +/-30% would read as
# dead air. Kept here so both domains stay independently tunable.
EMOTION_PACE_MULTIPLIERS: Dict[str, float] = {
    "urgent": 0.80,
    "excited": 0.82,
    "angry": 0.88,
    "tense": 0.88,
    "shocked": 0.92,
    "accusatory": 0.92,
    "fearful": 0.94,
    "cold": 0.96,
    "neutral": 1.00,
    "investigative": 1.00,
    "detached": 1.00,
    "incredulous": 1.02,
    "serious": 1.04,
    "authoritative": 1.04,
    "compassionate": 1.06,
    "calm": 1.08,
    "nostalgic": 1.08,
    "conspiratorial": 1.08,
    "sad": 1.10,
    "melancholic": 1.10,
    "whisper": 1.10,
    "contemplative": 1.12,
    "reverent": 1.12,
    "haunted": 1.12,
    "mysterious": 1.14,
    "dramatic": 1.16,
    "ominous": 1.18,
    "solemn": 1.22,
}

# Mirrors modules/tts_presets.EMOTION_ALIASES so a script that writes
# "dark" or "warm" gets the same pacing as its canonical emotion. Kept
# in sync deliberately (core/ must not import modules/).
EMOTION_ALIASES: Dict[str, str] = {
    "dark": "ominous",
    "historical": "authoritative",
    "warm": "compassionate",
    "resigned": "solemn",
    "curious": "investigative",
    "empathetic": "compassionate",
    "defiant": "angry",
    "sorrowful": "sad",
}

# Characters that may sit AFTER the real terminal punctuation and must
# be looked through when classifying a line ending: closing quotes and
# brackets ( he said." / (for now.) / «...» ).
_CLOSERS = "\"'\u2019\u201d\u00bb)]}\u203a"

# An authored in-line pause tag at the very end of a line (both tag
# families TTSEngineManager.process_pause_tags understands). When one is
# present the silence is already inside the clip — see the
# stacked_pause_seconds note above.
TRAILING_PAUSE_TAG_RE = re.compile(
    r"""(?:
        \[PAUSE:[A-Za-z_]+\]
      | \[SILENCE=\d+(?:\.\d+)?(?:ms|s)\]
      | \[PAUSE\s+\d+(?:\.\d+)?\]
      | \[(?:SHORT_PAUSE|MEDIUM_PAUSE|LONG_PAUSE|PAUSE)\]
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Any bracket tag, stripped before punctuation classification so that
# "He vanished. [PAUSE:LONG]" is still classified as a sentence ending.
_ANY_TAG_RE = re.compile(r"\[[^\[\]\n]{1,40}\]")

_WORD_RE = re.compile(r"\S+")


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def resolve_pacing_config(
    source: Any = None, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build a complete pacing config from defaults + settings + overrides.

    Args:
        source: Optional settings provider. Either an object exposing
            ``.get(key, default)`` (``ConfigService`` does) or a plain
            dict of app settings. Only the six documented
            ``SETTING_KEYS`` are read; anything missing or unparseable
            silently keeps its default, so a partial or hand-edited
            ``app_settings.json`` can never break a render.
        overrides: Optional direct overrides, applied last. Keys are the
            internal names (``PACING_DEFAULTS`` keys), letting a caller
            or test set any value without touching config on disk.

    Returns:
        A new config dict — never the shared defaults object.
    """
    config: Dict[str, Any] = dict(PACING_DEFAULTS)

    if source is not None:
        getter = getattr(source, "get", None)
        for setting_key, internal_key in SETTING_KEYS.items():
            try:
                if callable(getter):
                    value = getter(setting_key, None)
                else:  # pragma: no cover - defensive, non-mapping source
                    value = None
            except Exception:  # noqa: BLE001 - config must never raise
                value = None
            if value is None:
                continue
            if internal_key == "enabled":
                config["enabled"] = bool(value)
                continue
            try:
                config[internal_key] = float(value)
            except (TypeError, ValueError):
                continue

    if overrides:
        for key, value in overrides.items():
            if key in config:
                config[key] = value

    # Keep the clamps sane even if a user inverted them by hand.
    try:
        low = float(config["min_seconds"])
        high = float(config["max_seconds"])
        if high < low:
            config["min_seconds"], config["max_seconds"] = high, low
    except (TypeError, ValueError):  # pragma: no cover - defensive
        config["min_seconds"] = PACING_DEFAULTS["min_seconds"]
        config["max_seconds"] = PACING_DEFAULTS["max_seconds"]
    return config


# ----------------------------------------------------------------------
# Punctuation
# ----------------------------------------------------------------------


def classify_line_ending(text: str) -> str:
    """Classify how a narration line ends, for pause-length purposes.

    Better punctuation handling is most of what makes the difference
    between "read by a person" and "read by a machine": a comma is not a
    full stop, a question hangs, an ellipsis trails off, and a line with
    no terminal punctuation at all is a sentence still in progress that
    must NOT get a full sentence-sized gap.

    Closing quotes/brackets after the punctuation are looked through
    (``he whispered."`` is a sentence ending), and any authored bracket
    tag is stripped first (``He vanished. [PAUSE:LONG]`` is still a
    sentence ending).

    Args:
        text: The line's text, exactly as authored.

    Returns:
        One of the keys of ``PUNCTUATION_PAUSE_SECONDS``.
    """
    cleaned = _ANY_TAG_RE.sub(" ", str(text or "")).rstrip()
    if not cleaned:
        return "none"
    # Look through trailing closers to find the real final punctuation.
    stripped = cleaned.rstrip(_CLOSERS).rstrip()
    if not stripped:
        return "none"

    if stripped.endswith("\u2026") or stripped.endswith("..."):
        return "ellipsis"
    last = stripped[-1]
    if last == "?":
        return "question"
    if last == "!":
        return "exclamation"
    if last == ".":
        return "sentence"
    if last == ":":
        return "colon"
    if last == ";":
        return "semicolon"
    if last in "\u2014\u2013-":
        return "dash"
    if last == ",":
        return "comma"
    return "none"


# ----------------------------------------------------------------------
# Line normalisation
# ----------------------------------------------------------------------


def _line_view(line: Any) -> Dict[str, Any]:
    """Normalise one line dict into the fields the planner needs.

    Accepts core_engine's TTS job dicts (``text``/``character``), raw
    ``dialogue_lines`` rows (``text_content``/``character_name``), and
    the paragraph-group dicts used by the TTS seam — so every consumer
    can pass whatever it already has in hand.
    """
    if not isinstance(line, dict):  # pragma: no cover - defensive
        return {
            "text": str(line or ""),
            "character": "",
            "emotion": "neutral",
            "pause_after": "",
            "scene": "",
            "duration": None,
            "paragraph_internal": False,
        }
    text = str(line.get("text") or line.get("text_content") or "")
    character = str(line.get("character") or line.get("character_name") or "")
    emotion = str(
        line.get("emotion") or line.get("default_emotion") or "neutral"
    )
    scene: Any = line.get("scene_id")
    if scene is None:
        scene = line.get("scene_number")
    if scene is None:
        row = line.get("row")
        if isinstance(row, dict):
            scene = row.get("scene_id") or row.get("scene_number")
    duration = line.get("duration")
    if duration is None:
        duration = line.get("audio_duration")
    try:
        duration_value = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None
    return {
        "text": text,
        "character": character,
        "emotion": emotion,
        "pause_after": str(line.get("pause_after") or ""),
        "scene": "" if scene is None else str(scene),
        "duration": duration_value,
        "paragraph_internal": bool(line.get("paragraph_internal")),
    }


def _normalize_emotion(emotion: str) -> str:
    """Lower-case an emotion and resolve documentary aliases."""
    key = str(emotion or "neutral").strip().lower()
    return EMOTION_ALIASES.get(key, key)


def _estimated_speech_seconds(view: Dict[str, Any], config: Dict[str, Any]) -> float:
    """Measured duration when known, else the ~0.35s/word estimate."""
    duration = view.get("duration")
    if duration is not None and duration > 0:
        return float(duration)
    words = len(_WORD_RE.findall(view["text"]))
    per_word = float(config.get("seconds_per_word_estimate", 0.35))
    return max(0.0, words * per_word)


def _deterministic_unit(text: str, index: int) -> float:
    """Stable pseudo-random value in [-1, 1] for text at a position.

    CRC32 rather than ``hash()``: Python's string hashing is salted per
    process, which would make two runs — or two modules in the same run
    — disagree, and this value must be reproducible everywhere (see the
    module docstring, rule 2).
    """
    digest = zlib.crc32(f"{index}\x00{text}".encode("utf-8")) & 0xFFFFFFFF
    return (digest / 0xFFFFFFFF) * 2.0 - 1.0


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------


def pause_after_line(
    line: Any,
    next_line: Any = None,
    index: int = 0,
    config: Optional[Dict[str, Any]] = None,
    previous_pause: Optional[float] = None,
    speech_since_breath: float = 0.0,
) -> float:
    """Plan the silence that should follow ONE narration line.

    Applied in this exact order (each step is numbered in the body):

      1. Authored-tag de-stacking — the clip already contains its pause.
      2. Author intent from ``pause_after``.
      3. Punctuation context, when the author specified nothing.
      4. Emotion-aware pacing.
      5. Speaker / scene-change beats.
      6. Paragraph-batch de-stacking (PHASE 5 interaction).
      7. Excessive-silence guard, for derived gaps only.
      8. Breathing need.
      9. Deterministic humanising jitter.
     10. Anti-robotic-rhythm nudge, then the breath floor is re-asserted
         and the result is clamped to the configured range.

    Args:
        line: The line that just finished (any shape ``_line_view``
            accepts).
        next_line: The line that follows, or None at the end of the
            narration (used for speaker/scene change detection).
        index: Position of ``line`` in the narration; feeds the
            deterministic jitter so identical repeated text still varies.
        config: Resolved config from ``resolve_pacing_config``; defaults
            are used when omitted.
        previous_pause: The gap planned for the previous boundary, used
            only by the anti-robotic-rhythm nudge.
        speech_since_breath: Seconds of speech since the last gap wide
            enough to breathe in.

    Returns:
        Gap in seconds, already clamped to the configured range.
    """
    cfg = config or PACING_DEFAULTS
    if not cfg.get("enabled", True):
        return float(cfg.get("base_seconds", LEGACY_PAUSE_SECONDS))

    view = _line_view(line)
    min_seconds = float(cfg["min_seconds"])
    max_seconds = float(cfg["max_seconds"])

    # 1. Anti-stacking (authored tag): the line's text ends with a pause
    #    tag, so insert_pauses_into_audio has ALREADY baked that silence
    #    into the clip — adding a planned gap on top would stack two
    #    pauses into one unnaturally long hole. Only a clean join here.
    if TRAILING_PAUSE_TAG_RE.search(view["text"] or ""):
        return _clamp(float(cfg["stacked_pause_seconds"]), min_seconds, max_seconds)

    # 2. Author intent next. "none" closes the gap outright; every other
    #    explicit label sets the length. The schema default "short"
    #    deliberately falls through to punctuation — see the table above.
    label = view["pause_after"].strip().lower()
    if label == "none":
        return 0.0
    if label in EXPLICIT_AUTHORED_LABELS:
        seconds = AUTHORED_PAUSE_SECONDS[label]
        author_specified = label in STRONG_AUTHORED_LABELS
    else:
        # 3. Otherwise the punctuation decides the natural length.
        seconds = PUNCTUATION_PAUSE_SECONDS.get(
            classify_line_ending(view["text"]),
            float(cfg.get("base_seconds", LEGACY_PAUSE_SECONDS)),
        )
        author_specified = False

    # 4. Emotion-aware pacing.
    seconds *= EMOTION_PACE_MULTIPLIERS.get(
        _normalize_emotion(view["emotion"]), 1.0
    )

    # 5. Structure: turn-taking and scene changes are real beats.
    next_view = _line_view(next_line) if next_line is not None else None
    if next_view is not None:
        if view["character"] and next_view["character"] != view["character"]:
            seconds *= float(cfg["speaker_change_multiplier"])
        if view["scene"] and next_view["scene"] != view["scene"]:
            seconds *= float(cfg["scene_change_multiplier"])

    # 6. Anti-stacking (paragraph batch): the engine already voiced a
    #    prosodic pause at this sentence boundary — see PHASE 5 note.
    if view["paragraph_internal"]:
        seconds *= float(cfg["paragraph_internal_ratio"])

    # 7. Excessive-silence guard for derived gaps only: never leave a
    #    hole longer than the line it follows really justifies. Authored
    #    beats are exempt — a one-word line CAN warrant a long pause if
    #    the script explicitly asked for one.
    if not author_specified:
        speech = _estimated_speech_seconds(view, cfg)
        if speech > 0:
            ceiling = max(
                min_seconds, speech * float(cfg["max_gap_to_speech_ratio"])
            )
            seconds = min(seconds, ceiling)

    # 8. Breathing-aware: after a long unbroken stretch, make room for a
    #    real breath at this boundary.
    needs_breath = speech_since_breath >= float(cfg["breath_interval_seconds"])
    breath_floor = float(cfg["breath_gap_seconds"]) if needs_breath else 0.0
    if needs_breath:
        seconds = max(seconds, breath_floor)

    # 9. Deterministic humanising jitter — the same script always plans
    #    the same gaps, but no two neighbours share an exact length.
    jitter = float(cfg["jitter_ratio"])
    if jitter > 0:
        seconds *= 1.0 + _deterministic_unit(view["text"], index) * jitter

    # 10. Anti-robotic rhythm: never repeat the previous interval.
    if previous_pause is not None:
        epsilon = float(cfg["uniformity_epsilon"])
        if abs(seconds - previous_pause) < epsilon:
            nudge = float(cfg["uniformity_nudge"])
            if _deterministic_unit(view["text"], index + 977) >= 0:
                seconds += nudge
            else:
                seconds = max(min_seconds, seconds - nudge)

    # Steps 9-10 can only shave a few percent off, but a breath that is
    # needed must still BE a breath — re-assert the floor so the planner
    # and plan_narration_pauses's breath-counter reset never disagree
    # (a gap trimmed just below breath_min_gap_seconds would otherwise
    # leave the counter running and stack the need onto the next line).
    if needs_breath:
        seconds = max(seconds, breath_floor)

    return _clamp(seconds, min_seconds, max_seconds)


def plan_narration_pauses(
    lines: Sequence[Any],
    config: Optional[Dict[str, Any]] = None,
) -> List[float]:
    """Plan the gap that follows every narration line, in order.

    This is the entry point every consumer uses. Given the same lines
    and the same config it always returns the same list, which is what
    lets ``core_engine`` (word-timestamp offsets), ``audio_processor``
    (the silence actually inserted into the WAV) and ``timeline_engine``
    (scene durations) agree to the millisecond without sharing state.

    Args:
        lines: Narration lines in playback order. Each may be a
            core_engine TTS job, a ``dialogue_lines`` row, or any dict
            with ``text``/``text_content``; optional keys used when
            present are ``character``/``character_name``, ``emotion``,
            ``pause_after``, ``scene_id``/``scene_number``,
            ``duration``/``audio_duration`` and ``paragraph_internal``.
        config: Resolved config from ``resolve_pacing_config``.

    Returns:
        One gap per line, in seconds — ``result[i]`` is the silence
        AFTER ``lines[i]``. The final entry is the trailing gap, which
        callers normally drop (no silence is appended after the last
        line of a narration track).
    """
    cfg = config or PACING_DEFAULTS
    items = list(lines or [])
    if not items:
        return []

    if not cfg.get("enabled", True):
        flat = float(cfg.get("base_seconds", LEGACY_PAUSE_SECONDS))
        return [flat] * len(items)

    plan: List[float] = []
    previous: Optional[float] = None
    speech_since_breath = 0.0
    breath_min_gap = float(cfg["breath_min_gap_seconds"])

    for index, line in enumerate(items):
        view = _line_view(line)
        speech_since_breath += _estimated_speech_seconds(view, cfg)
        next_line = items[index + 1] if index + 1 < len(items) else None
        seconds = pause_after_line(
            line,
            next_line=next_line,
            index=index,
            config=cfg,
            previous_pause=previous,
            speech_since_breath=speech_since_breath,
        )
        seconds = round(seconds, 3)
        if seconds >= breath_min_gap:
            # Wide enough to breathe in — restart the breath counter.
            speech_since_breath = 0.0
        plan.append(seconds)
        previous = seconds
    return plan


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp a pause length into the configured range."""
    return max(low, min(high, float(value)))
