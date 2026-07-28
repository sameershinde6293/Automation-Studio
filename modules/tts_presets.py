"""TTS emotion, pause, reverb, and EQ presets for Autopilot.

Separated from TTSEngineManager to keep configuration data maintainable.
All 28 TTS emotions from File 07/08 plus documentary extensions.
"""

from __future__ import annotations

from typing import Dict, Tuple

# 28 TTS emotion presets: speed_mult, pitch_off (semitones), vol_mult
EMOTION_PRESETS: Dict[str, Dict[str, float]] = {
    "neutral": {"speed_mult": 1.00, "pitch_off": 0, "vol_mult": 1.00},
    "calm": {"speed_mult": 0.90, "pitch_off": 0, "vol_mult": 0.95},
    "serious": {"speed_mult": 0.95, "pitch_off": 0, "vol_mult": 1.00},
    "dramatic": {"speed_mult": 0.85, "pitch_off": -1, "vol_mult": 1.05},
    "mysterious": {"speed_mult": 0.80, "pitch_off": -2, "vol_mult": 0.95},
    "excited": {"speed_mult": 1.15, "pitch_off": 1, "vol_mult": 1.10},
    "sad": {"speed_mult": 0.85, "pitch_off": -1, "vol_mult": 0.90},
    "angry": {"speed_mult": 1.10, "pitch_off": 1, "vol_mult": 1.15},
    "fearful": {"speed_mult": 1.05, "pitch_off": 0, "vol_mult": 0.85},
    "whisper": {"speed_mult": 0.90, "pitch_off": 0, "vol_mult": 0.40},
    "tense": {"speed_mult": 1.10, "pitch_off": 0, "vol_mult": 1.05},
    "reverent": {"speed_mult": 0.85, "pitch_off": 0, "vol_mult": 0.95},
    "investigative": {"speed_mult": 1.00, "pitch_off": 0, "vol_mult": 1.00},
    "authoritative": {"speed_mult": 0.95, "pitch_off": 0, "vol_mult": 1.10},
    "conspiratorial": {"speed_mult": 0.85, "pitch_off": -1, "vol_mult": 0.80},
    "ominous": {"speed_mult": 0.75, "pitch_off": -3, "vol_mult": 1.00},
    "shocked": {"speed_mult": 1.05, "pitch_off": 1, "vol_mult": 1.10},
    "melancholic": {"speed_mult": 0.85, "pitch_off": -1, "vol_mult": 0.90},
    "urgent": {"speed_mult": 1.20, "pitch_off": 1, "vol_mult": 1.15},
    "nostalgic": {"speed_mult": 0.90, "pitch_off": 0, "vol_mult": 0.95},
    "cold": {"speed_mult": 1.00, "pitch_off": 0, "vol_mult": 1.00},
    "haunted": {"speed_mult": 0.85, "pitch_off": -1, "vol_mult": 0.85},
    "solemn": {"speed_mult": 0.80, "pitch_off": -1, "vol_mult": 0.95},
    "contemplative": {"speed_mult": 0.85, "pitch_off": 0, "vol_mult": 0.90},
    "incredulous": {"speed_mult": 1.00, "pitch_off": 1, "vol_mult": 1.05},
    "compassionate": {"speed_mult": 0.90, "pitch_off": 0, "vol_mult": 0.95},
    "detached": {"speed_mult": 1.00, "pitch_off": 0, "vol_mult": 1.00},
    "accusatory": {"speed_mult": 1.05, "pitch_off": 1, "vol_mult": 1.10},
}

# Extended documentary aliases mapping to base presets
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

# Pause base + variation (File 08 algorithm; ranges cover user B.4 targets)
PAUSE_BASE_DURATIONS: Dict[str, float] = {
    "MICRO": 0.20,
    "SHORT": 0.50,
    "MEDIUM": 1.00,
    "LONG": 2.00,
    "DRAMATIC": 3.25,
}
PAUSE_VARIATIONS: Dict[str, float] = {
    "MICRO": 0.05,
    "SHORT": 0.10,
    "MEDIUM": 0.15,
    "LONG": 0.25,
    "DRAMATIC": 0.30,
}

PAUSE_EMOTION_MULTIPLIERS: Dict[str, float] = {
    "dramatic": 1.20,
    "mysterious": 1.15,
    "ominous": 1.25,
    "solemn": 1.30,
    "urgent": 0.75,
    "excited": 0.70,
    "calm": 1.10,
    "neutral": 1.00,
    "whisper": 1.15,
    "cold": 0.90,
    "haunted": 1.15,
    "conspiratorial": 1.10,
}

# FFmpeg aecho filter strings for reverb presets
REVERB_PRESETS: Dict[str, str] = {
    "none": "",
    "subtle_room": "aecho=0.9:0.85:20|35:0.1|0.05",
    "small_room": "aecho=0.85:0.80:30|60|90:0.15|0.08|0.04",
    "medium_room": "aecho=0.80:0.75:50|100|150:0.25|0.12|0.06",
    "large_hall": "aecho=0.75:0.70:60|120|200|300:0.35|0.20|0.10|0.05",
    "cathedral": "aecho=0.70:0.65:80|160|300|500|700:0.50|0.35|0.20|0.10|0.05",
    "cave": "aecho=0.80:0.75:40|90|150|220:0.40|0.30|0.18|0.08",
    "telephone": "aecho=0.9:0.85:5|10:0.05|0.02",
    "old_radio": "aecho=0.9:0.85:8|15:0.04|0.02",
    "underground_bunker": "aecho=0.75:0.70:45|100|200|350:0.42|0.28|0.15|0.07",
}

EQ_PRESETS: Dict[str, str] = {
    "flat": "",
    "documentary_male": (
        "equalizer=f=250:t=o:w=200:g=2,"
        "equalizer=f=3000:t=o:w=1000:g=3,"
        "equalizer=f=8000:t=o:w=3000:g=2"
    ),
    "documentary_female": (
        "highpass=f=120,"
        "equalizer=f=200:t=o:w=150:g=-1,"
        "equalizer=f=3500:t=o:w=1200:g=2,"
        "equalizer=f=10000:t=o:w=3000:g=2"
    ),
    "elderly_male": (
        "equalizer=f=300:t=o:w=250:g=3,"
        "equalizer=f=6000:t=h:w=4000:g=-3,"
        "equalizer=f=2500:t=o:w=800:g=2"
    ),
    "military": (
        "highpass=f=100,"
        "equalizer=f=200:t=o:w=100:g=-2,"
        "equalizer=f=2500:t=o:w=1000:g=4,"
        "equalizer=f=8000:t=o:w=3000:g=2"
    ),
    "villain": (
        "equalizer=f=150:t=o:w=100:g=4,"
        "equalizer=f=500:t=o:w=300:g=-2,"
        "equalizer=f=10000:t=h:w=3000:g=-3"
    ),
    "whisper_character": (
        "highpass=f=200,"
        "equalizer=f=4000:t=o:w=2000:g=3,"
        "equalizer=f=12000:t=o:w=4000:g=4"
    ),
}

SPECIAL_EFFECTS: Dict[str, str] = {
    "none": "",
    "old_radio": "highpass=f=300,lowpass=f=3400,aecho=0.9:0.85:8:0.04,volume=1.2",
    "telephone": "highpass=f=300,lowpass=f=3400,acompressor=threshold=-20dB:ratio=4,aecho=0.95:0.90:5:0.02",
    "megaphone": "highpass=f=500,lowpass=f=4000,acompressor=threshold=-15dB:ratio=8,volume=1.3",
    "underground": "lowpass=f=800,aecho=0.75:0.70:50|120|250:0.45|0.28|0.12",
    "ghost": "aecho=0.80:0.75:60|130|250:0.50|0.30|0.15,asetrate=44100*0.98,aresample=44100,volume=0.90",
}

# PHASE 2 (voice effects chain rebuild): the fixed, safe processing-order
# building blocks used by TTSEngineManager.apply_voice_effects. Kept here
# alongside the other filter-string presets rather than inlined in the
# engine manager so the actual DSP settings are easy to find/tune in one
# place, same as EQ_PRESETS/REVERB_PRESETS/SPECIAL_EFFECTS above.
#
# Required chain order (per-line, before mixing):
#   High-pass -> Gentle EQ -> (optional reverb/special coloring) ->
#   Light Compressor -> Limiter -> LUFS Normalization (single, optional)
#
# Every stage is independently bypassable — see apply_voice_effects's
# `*_enabled` profile/param flags — and the whole chain degrades
# gracefully (dropping the most fragile optional stages first) instead
# of crashing or silently shipping broken audio when ffmpeg fails.
HIGHPASS_FILTER: str = "highpass=f=80"

# Noise gate: unchanged parameters from the original always-on chain —
# now individually bypassable via voice_profiles.noise_gate_enabled
# (already a real schema column; previously ignored by the code).
NOISE_GATE_FILTER: str = "agate=threshold=0.008:ratio=10:attack=1:release=200"

# Light compressor: gentle ratio/knee, makeup=1 (no automatic gain) so
# it evens out dynamics without adding an "unsafe" extra gain stage —
# final level is still governed by the single limiter + optional LUFS
# stage that follow it, never by the compressor itself.
COMPRESSOR_FILTER: str = (
    "acompressor=threshold=-20dB:ratio=2.5:attack=15:release=250:"
    "makeup=1:knee=6"
)

# Single limiter for this chain (unchanged from the previous
# implementation — already well exercised in production).
LIMITER_FILTER: str = "alimiter=limit=0.95:level=false"

# Single-pass loudnorm for per-line consistency. Deliberately NOT the
# 2-pass measure+apply loudnorm audio_processor.normalize_to_lufs uses
# for the final mix — running a 2-pass measurement per narration line
# would roughly double ffmpeg invocations for every line in a render
# (HARD RULE: never increase render time unnecessarily). Off by default
# (see apply_voice_effects's lufs_normalize_enabled, default False) —
# the final mix's own 2-pass LUFS pass already normalizes the complete,
# mixed program once; this is an opt-in per-line consistency aid, not a
# second mandatory normalization of the same signal.
LUFS_NORMALIZE_FILTER: str = "loudnorm=I=-16:TP=-1.5:LRA=11"

# Breath insertion probability by pause type
BREATH_CHANCE: Dict[str, float] = {
    "MICRO": 0.0,
    "SHORT": 0.20,
    "MEDIUM": 0.70,
    "LONG": 0.95,
    "DRAMATIC": 1.0,
}

BREATH_VOLUME_RANGE: Dict[str, Tuple[float, float]] = {
    "MICRO": (0.0, 0.0),
    "SHORT": (0.05, 0.08),
    "MEDIUM": (0.10, 0.15),
    "LONG": (0.15, 0.20),
    "DRAMATIC": (0.18, 0.25),
}

# PHASE 5 (natural narration / paragraph-based TTS): batching limits for
# grouping consecutive same-character sentences into a single TTS
# request. Conservative defaults chosen to stay well inside every
# supported engine's practical comfort zone (Piper/Kokoro/XTTS all
# handle a few hundred characters without quality loss; much beyond
# this, per-engine failure modes — truncation, drift, OOM — become more
# likely) while still meaningfully reducing the number of separate
# clips (and therefore joins/crossfades) versus one-request-per-sentence.
# Never exceeded — see TTSEngineManager.group_sentences_into_paragraphs.
PARAGRAPH_MAX_CHARS: int = 350
PARAGRAPH_MAX_SENTENCES: int = 5
# A hard ceiling no single paragraph request may cross regardless of the
# above — the actual "never exceed engine limits" backstop.
PARAGRAPH_HARD_MAX_CHARS: int = 480
# Estimated-duration cap (seconds, using the same ~0.35s/word heuristic
# already used elsewhere in this codebase for QA/fallback duration
# estimates) — batching stops adding sentences to a paragraph once the
# estimated speech length would cross this, independent of char/sentence
# counts, so an unusually word-dense paragraph still can't produce an
# overlong single TTS request.
PARAGRAPH_MAX_ESTIMATED_SECONDS: float = 22.0
# Same ~0.35s/word heuristic already used by core_engine's per-line QA
# duration check (kept in sync deliberately — modules don't cross-import
# in this codebase, Rule A) — used here purely to estimate a paragraph's
# likely spoken duration BEFORE synthesis, for batching decisions only.
PARAGRAPH_SECONDS_PER_WORD_ESTIMATE: float = 0.35
# Authored pause labels strong enough to always end a paragraph after
# the line that carries them — a deliberate dramatic/long pause is a
# real narrative beat, not something paragraph batching should smooth
# over by continuing straight into the next sentence.
PARAGRAPH_BREAKING_PAUSES = frozenset({"medium", "long", "dramatic"})

ADMIN_PASSWORD = "IAMKING"
