"""Emotion-aware prosody planning for narration (PHASE 7).

The planner is deliberately pure, deterministic, and stdlib-only.  It
turns text plus optional emotion context into a compact performance plan:

* one context-smoothed speaking-rate multiplier per narration line;
* phrase groups with subtle, duration-preserving pitch contours; and
* a small set of important words to stress with a gentle energy envelope.

The TTS manager consumes the plan after synthesis.  Phrase pitch shaping
is applied inside each phrase without changing the phrase or clip length,
and word timing is mapped through the same transform.  That keeps the
Phase 6 pause plan, narration duration, subtitles, and timeline aligned.
If planning or DSP is unavailable, callers simply use the existing TTS
path and the Phase 1-6 behavior remains intact.
"""

from __future__ import annotations

import math
import re
import zlib
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

SETTING_KEYS: Dict[str, str] = {
    "prosody_enhancement_enabled": "enabled",
    "prosody_emphasis_strength": "emphasis_strength",
    "prosody_intonation_strength": "intonation_strength",
    "prosody_transition_smoothing": "transition_smoothing",
    "prosody_phrase_max_words": "phrase_max_words",
    "prosody_max_emphasized_words": "max_emphasized_words",
}

PROSODY_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "emphasis_strength": 0.72,
    "intonation_strength": 0.68,
    "transition_smoothing": 0.22,
    "phrase_max_words": 11,
    "max_emphasized_words": 4,
    "max_emphasis_gain_db": 1.8,
    "max_pitch_semitones": 0.55,
    "rate_variation": 0.015,
    "min_rate_multiplier": 0.65,
    "max_rate_multiplier": 1.30,
}

# The speed values preserve the established Phase 2 emotion delivery.
# The additional range/energy values only control subtle within-utterance
# movement; they never replace a voice profile's base pitch or volume.
EMOTION_DELIVERY: Dict[str, Dict[str, float]] = {
    "neutral": {"rate": 1.00, "range": 0.55, "energy": 0.50},
    "calm": {"rate": 0.90, "range": 0.42, "energy": 0.36},
    "serious": {"rate": 0.95, "range": 0.45, "energy": 0.52},
    "dramatic": {"rate": 0.85, "range": 0.85, "energy": 0.78},
    "mysterious": {"rate": 0.80, "range": 0.62, "energy": 0.38},
    "excited": {"rate": 1.15, "range": 1.00, "energy": 0.95},
    "sad": {"rate": 0.85, "range": 0.38, "energy": 0.30},
    "angry": {"rate": 1.10, "range": 0.78, "energy": 1.00},
    "fearful": {"rate": 1.05, "range": 0.88, "energy": 0.58},
    "whisper": {"rate": 0.90, "range": 0.38, "energy": 0.18},
    "tense": {"rate": 1.10, "range": 0.72, "energy": 0.72},
    "reverent": {"rate": 0.85, "range": 0.40, "energy": 0.32},
    "investigative": {"rate": 1.00, "range": 0.62, "energy": 0.56},
    "authoritative": {"rate": 0.95, "range": 0.45, "energy": 0.72},
    "conspiratorial": {"rate": 0.85, "range": 0.58, "energy": 0.30},
    "ominous": {"rate": 0.75, "range": 0.58, "energy": 0.66},
    "shocked": {"rate": 1.05, "range": 1.00, "energy": 0.92},
    "melancholic": {"rate": 0.85, "range": 0.38, "energy": 0.28},
    "urgent": {"rate": 1.20, "range": 0.82, "energy": 0.98},
    "nostalgic": {"rate": 0.90, "range": 0.48, "energy": 0.38},
    "cold": {"rate": 1.00, "range": 0.28, "energy": 0.42},
    "haunted": {"rate": 0.85, "range": 0.58, "energy": 0.30},
    "solemn": {"rate": 0.80, "range": 0.32, "energy": 0.38},
    "contemplative": {"rate": 0.85, "range": 0.52, "energy": 0.34},
    "incredulous": {"rate": 1.00, "range": 0.90, "energy": 0.72},
    "compassionate": {"rate": 0.90, "range": 0.48, "energy": 0.38},
    "detached": {"rate": 1.00, "range": 0.25, "energy": 0.38},
    "accusatory": {"rate": 1.05, "range": 0.72, "energy": 0.88},
}

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

# Compact lexical cues are used for stress, not for replacing an authored
# emotion.  Emotion metadata may be absent and neutral delivery remains a
# complete, safe plan.
EMOTIONAL_CUES = frozenset(
    {
        "alive", "alone", "amazing", "astonishing", "battle", "betrayed",
        "breakthrough", "catastrophic", "critical", "danger", "darkness",
        "dead", "death", "destroyed", "disappeared", "discovered", "doom",
        "evidence", "explosive", "fear", "final", "forever", "hidden",
        "horror", "immediately", "impossible", "killed", "legacy", "lost",
        "massacre", "murdered", "never", "nobody", "only", "revealed",
        "secret", "shocking", "silence", "suddenly", "terrifying", "truth",
        "unknown", "warning", "victory", "vanished",
    }
)
CONTRAST_WORDS = frozenset(
    {
        "actually", "although", "but", "despite", "except", "however",
        "instead", "never", "not", "only", "rather", "still", "unless",
        "yet",
    }
)
CONNECTORS = frozenset(
    {
        "although", "and", "because", "but", "however", "if", "instead",
        "meanwhile", "or", "since", "so", "therefore", "though", "while",
        "yet",
    }
)
STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "by",
        "for", "from", "had", "has", "have", "he", "her", "hers", "him",
        "his", "i", "in", "into", "is", "it", "its", "of", "on", "or",
        "our", "ours", "she", "that", "the", "their", "theirs", "them",
        "they", "this", "those", "to", "was", "we", "were", "which", "who",
        "will", "with", "you", "your",
    }
)

_SPELL_RE = re.compile(r"\[SPELL\](.*?)\[/SPELL\]", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"\[[^\[\]\n]{1,40}\]")
_WORD_CLEAN_RE = re.compile(r"[^\w'’-]+", re.UNICODE)
_NUMBER_RE = re.compile(r"^(?:[$£€]?\d[\d,.:/-]*%?)$")


def resolve_prosody_config(
    source: Any = None, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Resolve app settings into a bounded, complete prosody config."""
    config: Dict[str, Any] = dict(PROSODY_DEFAULTS)
    getter = getattr(source, "get", None) if source is not None else None
    for setting_key, internal_key in SETTING_KEYS.items():
        try:
            value = getter(setting_key, None) if callable(getter) else None
        except Exception:  # noqa: BLE001 - configuration is never fatal
            value = None
        if value is None:
            continue
        if internal_key == "enabled":
            config[internal_key] = bool(value)
            continue
        try:
            config[internal_key] = float(value)
        except (TypeError, ValueError):
            continue

    if overrides:
        for key, value in overrides.items():
            if key in config:
                config[key] = value

    config["emphasis_strength"] = _clamp(
        config["emphasis_strength"], 0.0, 1.0
    )
    config["intonation_strength"] = _clamp(
        config["intonation_strength"], 0.0, 1.0
    )
    config["transition_smoothing"] = _clamp(
        config["transition_smoothing"], 0.0, 0.45
    )
    config["phrase_max_words"] = int(
        _clamp(config["phrase_max_words"], 5.0, 20.0)
    )
    config["max_emphasized_words"] = int(
        _clamp(config["max_emphasized_words"], 1.0, 8.0)
    )
    return config


def spoken_words(text: str) -> List[str]:
    """Return the whitespace tokens the TTS manager will timestamp.

    This mirrors its inexpensive text-only handling of SPELL and bracket
    tags, allowing plans made before synthesis to retain exact word
    indices after pause tags are removed.
    """

    def _spell(match: "re.Match[str]") -> str:
        letters = re.sub(r"\s+", "", match.group(1) or "")
        return " ".join(letters)

    expanded = _SPELL_RE.sub(_spell, str(text or ""))
    expanded = _TAG_RE.sub(" ", expanded)
    return re.sub(r"\s+", " ", expanded).strip().split()


def plan_narration_prosody(
    lines: Sequence[Any], config: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Plan rate, phrases, intonation, and emphasis for ordered lines."""
    cfg = dict(config or PROSODY_DEFAULTS)
    items = list(lines or [])
    if not items:
        return []

    views = [_line_view(line) for line in items]
    context_words = [_content_vocabulary(view["words"]) for view in views]
    plans: List[Dict[str, Any]] = []

    for index, view in enumerate(views):
        previous = views[index - 1] if index else None
        following = views[index + 1] if index + 1 < len(views) else None
        nearby: Set[str] = set()
        if index:
            nearby.update(context_words[index - 1])
        if index + 1 < len(views):
            nearby.update(context_words[index + 1])
        plans.append(
            _plan_line(view, previous, following, nearby, index, cfg)
        )
    return plans


def merge_prosody_plans(plans: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge line-local plans into one paragraph-relative plan."""
    valid = [plan for plan in plans if isinstance(plan, dict)]
    enabled = [plan for plan in valid if plan.get("enabled")]
    if not enabled:
        return {"enabled": False, "word_count": 0, "phrases": [], "emphasis": []}

    phrases: List[Dict[str, Any]] = []
    emphasis: List[Dict[str, Any]] = []
    offset = 0
    rate_total = 0.0
    weight_total = 0
    for plan in valid:
        count = max(0, int(plan.get("word_count") or 0))
        if plan.get("enabled"):
            weight = max(1, count)
            rate_total += float(plan.get("rate_multiplier") or 1.0) * weight
            weight_total += weight
            for phrase in plan.get("phrases") or []:
                item = dict(phrase)
                item["start_word"] = int(item.get("start_word") or 0) + offset
                item["end_word"] = int(item.get("end_word") or 0) + offset
                phrases.append(item)
            for stress in plan.get("emphasis") or []:
                item = dict(stress)
                item["word_index"] = int(item.get("word_index") or 0) + offset
                emphasis.append(item)
        offset += count

    first = enabled[0]
    return {
        "enabled": True,
        "emotion": first.get("emotion") or "neutral",
        "emotion_available": all(
            bool(plan.get("emotion_available")) for plan in enabled
        ),
        "rate_multiplier": round(rate_total / max(1, weight_total), 4),
        "word_count": offset,
        "phrases": phrases,
        "emphasis": emphasis,
        "transition": first.get("transition") or {},
    }


def _plan_line(
    view: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    following: Optional[Dict[str, Any]],
    nearby_words: Set[str],
    index: int,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    words = view["words"]
    emotion = view["emotion"]
    profile = EMOTION_DELIVERY[emotion]
    if not config.get("enabled", True) or not words:
        return {
            "enabled": False,
            "emotion": emotion,
            "emotion_available": view["emotion_available"],
            "rate_multiplier": profile["rate"],
            "word_count": len(words),
            "phrases": [],
            "emphasis": [],
            "transition": {},
        }

    previous_emotion = previous["emotion"] if previous else emotion
    next_emotion = following["emotion"] if following else emotion
    previous_profile = EMOTION_DELIVERY[previous_emotion]
    next_profile = EMOTION_DELIVERY[next_emotion]
    smoothing = float(config["transition_smoothing"])
    same_speaker_before = bool(
        previous and previous.get("character") == view.get("character")
    )
    same_speaker_after = bool(
        following and following.get("character") == view.get("character")
    )

    rate = float(profile["rate"])
    transition_from = previous_emotion != emotion and same_speaker_before
    transition_to = next_emotion != emotion and same_speaker_after
    if transition_from:
        rate = rate * (1.0 - smoothing) + previous_profile["rate"] * smoothing
    if transition_to:
        anticipation = smoothing * 0.28
        rate = rate * (1.0 - anticipation) + next_profile["rate"] * anticipation

    # Long phrases need slightly more articulation; short high-impact
    # statements need room to land. Deterministic micro-cadence replaces
    # the old run-to-run random rate wobble when enhancement is enabled.
    if len(words) >= 28:
        rate *= 0.97
    elif len(words) <= 6 and profile["energy"] >= 0.70:
        rate *= 0.975
    if str(view["text"]).rstrip().endswith("?"):
        rate *= 0.985
    unit = _deterministic_unit(f"{index}:{view['text']}")
    rate *= 1.0 + unit * float(config["rate_variation"])
    rate = _clamp(
        rate,
        float(config["min_rate_multiplier"]),
        float(config["max_rate_multiplier"]),
    )

    phrase_ranges = _group_phrases(words, int(config["phrase_max_words"]))
    phrases = [
        _phrase_plan(
            words,
            start,
            end,
            phrase_index,
            len(phrase_ranges),
            emotion,
            previous_profile if transition_from else None,
            config,
        )
        for phrase_index, (start, end) in enumerate(phrase_ranges)
    ]
    emphasis = _plan_emphasis(
        words,
        nearby_words,
        phrase_ranges,
        float(profile["energy"]),
        config,
    )
    distance_from = _emotion_distance(profile, previous_profile)
    distance_to = _emotion_distance(profile, next_profile)

    return {
        "enabled": True,
        "emotion": emotion,
        "emotion_available": view["emotion_available"],
        "rate_multiplier": round(rate, 4),
        "word_count": len(words),
        "phrases": phrases,
        "emphasis": emphasis,
        "transition": {
            "from": previous_emotion if transition_from else emotion,
            "to": next_emotion if transition_to else emotion,
            "from_distance": round(distance_from if transition_from else 0.0, 3),
            "to_distance": round(distance_to if transition_to else 0.0, 3),
            "smoothed": bool(transition_from or transition_to),
        },
    }


def _phrase_plan(
    words: Sequence[str],
    start: int,
    end: int,
    phrase_index: int,
    phrase_count: int,
    emotion: str,
    previous_profile: Optional[Dict[str, float]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    token = words[end].rstrip("\"'”’)]}")
    if token.endswith("?"):
        shape = (0.00, 0.18, 0.65)
        ending = "question"
    elif token.endswith("!"):
        shape = (0.08, 0.72, -0.06)
        ending = "exclamation"
    elif token.endswith(("...", "…")):
        shape = (0.05, 0.02, -0.58)
        ending = "ellipsis"
    elif token.endswith((",", ";", ":", "—", "–")):
        shape = (0.00, 0.30, 0.08)
        ending = "continuation"
    else:
        shape = (0.05, 0.34, -0.26)
        ending = "statement"

    delivery = EMOTION_DELIVERY[emotion]
    scale = (
        float(delivery["range"])
        * float(config["intonation_strength"])
        * float(config["max_pitch_semitones"])
    )
    # Alternating peaks keep adjacent phrases from repeating one melodic
    # shape while retaining a gentle overall paragraph declination.
    cadence = 1.0 + (0.08 if phrase_index % 2 == 0 else -0.06)
    paragraph_fall = 0.06 * (phrase_index / max(1, phrase_count - 1))
    start_pitch = shape[0] * scale
    peak_pitch = shape[1] * scale * cadence
    end_pitch = shape[2] * scale - paragraph_fall * scale

    if emotion in {"sad", "melancholic", "solemn", "haunted", "ominous"}:
        end_pitch -= 0.10 * scale
    elif emotion in {"excited", "shocked", "fearful", "incredulous"}:
        peak_pitch += 0.12 * scale
    elif emotion in {"cold", "detached", "authoritative"}:
        peak_pitch *= 0.70

    if phrase_index == 0 and previous_profile is not None:
        # Begin between the previous and current ranges rather than
        # snapping to an unrelated contour at an emotion boundary.
        previous_range = float(previous_profile["range"])
        current_range = max(0.01, float(delivery["range"]))
        ratio = _clamp(previous_range / current_range, 0.55, 1.45)
        start_pitch *= 0.75 + 0.25 * ratio

    limit = float(config["max_pitch_semitones"])
    phrase_energy = (float(delivery["energy"]) - 0.5) * 0.35
    return {
        "start_word": start,
        "end_word": end,
        "ending": ending,
        "pitch_start": round(_clamp(start_pitch, -limit, limit), 4),
        "pitch_peak": round(_clamp(peak_pitch, -limit, limit), 4),
        "pitch_end": round(_clamp(end_pitch, -limit, limit), 4),
        "energy_db": round(phrase_energy, 3),
    }


def _group_phrases(words: Sequence[str], max_words: int) -> List[Tuple[int, int]]:
    """Group tokens by punctuation, discourse boundaries, and length."""
    if not words:
        return []
    groups: List[Tuple[int, int]] = []
    start = 0
    for index, token in enumerate(words):
        length = index - start + 1
        cleaned = _normal_word(token)
        terminal = token.rstrip("\"'”’)]}").endswith(
            (".", "?", "!", "…", ";", ":", ",", "—", "–")
        )
        next_connector = (
            index + 1 < len(words)
            and _normal_word(words[index + 1]) in CONNECTORS
            and length >= 4
        )
        connector_break = cleaned in {"however", "meanwhile", "therefore"}
        hard_limit = length >= max_words
        if terminal or next_connector or connector_break or hard_limit:
            groups.append((start, index))
            start = index + 1
    if start < len(words):
        groups.append((start, len(words) - 1))

    # Avoid one-token fragments caused by adjacent commas/connectors.
    merged: List[Tuple[int, int]] = []
    for group in groups:
        if (
            merged
            and group[1] == group[0]
            and merged[-1][1] - merged[-1][0] < max_words
        ):
            previous = merged.pop()
            merged.append((previous[0], group[1]))
        else:
            merged.append(group)
    if len(merged) > 1 and merged[0][0] == merged[0][1]:
        first, second = merged[0], merged[1]
        merged[:2] = [(first[0], second[1])]
    return merged


def _plan_emphasis(
    words: Sequence[str],
    nearby_words: Set[str],
    phrase_ranges: Sequence[Tuple[int, int]],
    emotion_energy: float,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    phrase_starts = {start for start, _ in phrase_ranges}
    scored: List[Tuple[float, int, str, str]] = []
    last_content = -1
    for index, raw in enumerate(words):
        word = _normal_word(raw)
        if word and word not in STOP_WORDS:
            last_content = index
        if not word or (word in STOP_WORDS and word not in CONTRAST_WORDS):
            continue
        if len(word) <= 2 and word not in {"no", "not"}:
            continue

        score = 0.7
        reasons: List[str] = []
        if word in CONTRAST_WORDS:
            score += 3.3
            reasons.append("contrast")
        if word in EMOTIONAL_CUES:
            score += 2.6
            reasons.append("emotion")
        if _NUMBER_RE.match(raw.strip("\"'()[]{}")):
            score += 2.8
            reasons.append("number")
        letters = re.sub(r"[^A-Za-z]", "", raw)
        if len(letters) >= 2 and letters.isupper():
            score += 2.5
            reasons.append("acronym")
        elif index > 0 and raw[:1].isupper() and letters:
            score += 1.5
            reasons.append("name")
        if raw.startswith(("\"", "“", "'")) or raw.endswith(("\"", "”", "'")):
            score += 1.2
            reasons.append("quoted")
        if word.endswith(("est", "most")) or word in {"very", "truly", "utterly"}:
            score += 0.8
            reasons.append("degree")
        if index in phrase_starts and word not in CONNECTORS:
            score += 0.35
        if word not in nearby_words:
            score += 0.45
            reasons.append("new")
        else:
            score -= 0.55
        scored.append((score, index, raw, "+".join(reasons) or "content"))

    if last_content >= 0:
        scored = [
            (score + (0.65 if index == last_content else 0.0), index, raw, reason)
            for score, index, raw, reason in scored
        ]

    word_budget = max(1, math.ceil(len(words) / 7))
    budget = min(int(config["max_emphasized_words"]), word_budget)
    selected: List[Tuple[float, int, str, str]] = []
    for candidate in sorted(scored, key=lambda item: (-item[0], item[1])):
        score, index, _, _ = candidate
        if score < 1.15:
            continue
        if any(abs(index - existing[1]) <= 1 for existing in selected) and score < 3.5:
            continue
        selected.append(candidate)
        if len(selected) >= budget:
            break

    strength_scale = float(config["emphasis_strength"])
    # Whispered/somber delivery stresses more gently; urgent/angry/excited
    # delivery can carry a firmer accent without changing which words matter.
    emotion_scale = _clamp(0.72 + emotion_energy * 0.42, 0.75, 1.14)
    max_gain = float(config["max_emphasis_gain_db"])
    result: List[Dict[str, Any]] = []
    for score, index, raw, reason in sorted(selected, key=lambda item: item[1]):
        strength = (
            _clamp(0.35 + score / 8.0, 0.35, 1.0)
            * strength_scale
            * emotion_scale
        )
        strength = _clamp(strength, 0.0, 1.0)
        result.append(
            {
                "word_index": index,
                "word": raw,
                "strength": round(strength, 3),
                "gain_db": round(max_gain * strength, 3),
                "reason": reason,
            }
        )
    return result


def _line_view(line: Any) -> Dict[str, Any]:
    if isinstance(line, dict):
        text = str(line.get("text") or line.get("text_content") or "")
        raw_emotion = line.get("emotion") or line.get("default_emotion")
        profile = line.get("profile")
        if raw_emotion is None and isinstance(profile, dict):
            raw_emotion = profile.get("emotion") or profile.get("default_emotion")
        character = str(
            line.get("character") or line.get("character_name") or "NARRATOR"
        )
    else:
        text = str(line or "")
        raw_emotion = None
        character = "NARRATOR"
    emotion, available = _normalize_emotion(raw_emotion)
    return {
        "text": text,
        "words": spoken_words(text),
        "emotion": emotion,
        "emotion_available": available,
        "character": character,
    }


def _normalize_emotion(emotion: Any) -> Tuple[str, bool]:
    if emotion is None or not str(emotion).strip():
        return "neutral", False
    key = str(emotion).strip().lower()
    key = EMOTION_ALIASES.get(key, key)
    if key not in EMOTION_DELIVERY:
        return "neutral", False
    return key, True


def _normal_word(word: str) -> str:
    return _WORD_CLEAN_RE.sub("", str(word or "")).strip("_’'-").lower()


def _content_vocabulary(words: Sequence[str]) -> Set[str]:
    return {
        normal
        for normal in (_normal_word(word) for word in words)
        if normal and normal not in STOP_WORDS and len(normal) > 2
    }


def _emotion_distance(
    left: Dict[str, float], right: Dict[str, float]
) -> float:
    return min(
        1.0,
        abs(left["rate"] - right["rate"]) / 0.45 * 0.45
        + abs(left["range"] - right["range"]) * 0.30
        + abs(left["energy"] - right["energy"]) * 0.35,
    )


def _deterministic_unit(text: str) -> float:
    digest = zlib.crc32(str(text).encode("utf-8")) & 0xFFFFFFFF
    return (digest / 0xFFFFFFFF) * 2.0 - 1.0


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    if not math.isfinite(number):
        number = low
    return max(low, min(high, number))
