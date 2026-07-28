"""Phase 3 QA: render 30-second and 5-minute narration with pauses and
breathing enabled (the exact splice code paths hardened in this phase)
and verify zero clicks/pops/dropouts survive narration assembly and the
final mix.

Run from anywhere with:
    python scripts/qa_render_phase3_clicks.py
"""

from __future__ import annotations

import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.service_container import ServiceContainer  # noqa: E402
from modules.audio_processor import AudioProcessor  # noqa: E402
from modules.tts_engine_manager import TTSEngineManager  # noqa: E402

# Deliberately dense with [PAUSE:*] tags so every render exercises
# insert_pauses_into_audio's splice points (and, with breathing_enabled,
# add_breathing_sounds' overlay splice points) many times over.
SENTENCES = [
    "Welcome to this documentary. [PAUSE:SHORT] Let us begin.",
    "Long before recorded history, [PAUSE:MICRO] societies began to form.",
    "[PAUSE:MEDIUM] Trade routes connected distant lands across seas.",
    "Great cities rose, [PAUSE:SHORT] shaped by rivers and mountains.",
    "[PAUSE:LONG] And yet, every empire eventually fades.",
    "We will explore how these worlds were built, [PAUSE:MICRO] and how they fell.",
    "Scholars debate the causes. [PAUSE:MEDIUM] Climate, conflict, internal strife.",
    "[PAUSE:DRAMATIC] But the full picture is more complicated than that.",
    "Join us [PAUSE:SHORT] as we examine the evidence.",
]


def _click_count(mono: np.ndarray) -> int:
    diffs = np.abs(np.diff(mono))
    med = float(np.median(diffs)) + 1e-9
    return int(np.count_nonzero((diffs > 0.25) & (diffs > med * 12.0)))


def render_and_check(
    tts: TTSEngineManager, audio: AudioProcessor, tmp_root: Path,
    target_seconds: float, label: str,
) -> None:
    tmp = tmp_root / label
    tmp.mkdir(parents=True, exist_ok=True)
    profile = {
        "engine": "piper",
        "voice_model": "default",
        "speed": 1.0,
        "pitch": 0,
        "volume": 1.0,
        "default_emotion": "neutral",
        "reverb_preset": "subtle_room",
        "eq_preset": "documentary_male",
        "breathing_enabled": True,
    }

    line_paths = []
    total = 0.0
    i = 0
    t0 = time.perf_counter()
    while total < target_seconds:
        text = SENTENCES[i % len(SENTENCES)]
        out = tmp / f"line_{i:04d}.wav"
        result = tts.generate_audio(text, profile, out)
        assert result["success"], result
        line_paths.append(result["data"]["audio_path"])
        total += result["data"]["duration"]
        i += 1
    print(
        f"[{label}] {len(line_paths)} lines, {total:.1f}s raw, "
        f"{time.perf_counter() - t0:.1f}s wall clock"
    )

    narration_out = tmp / "narration.wav"
    built = audio.build_narration_track(
        f"phase3-qa-{label}", line_paths, narration_out, pause_seconds=0.3
    )
    assert built["success"], built

    final_out = tmp / "final_mix.wav"
    mix_result = audio.generate_final_mix(
        f"phase3-qa-{label}",
        final_out,
        {"narration_path": str(narration_out)},
    )
    assert mix_result["success"], mix_result

    with wave.open(str(final_out), "rb") as handle:
        sr = handle.getframerate()
        ch = handle.getnchannels()
        n = handle.getnframes()
        raw = handle.readframes(n)
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch)
    mono = data.mean(axis=1) if data.ndim == 2 else data
    duration = n / float(sr)

    print(f"--- QA: {label} ({duration:.1f}s) ---")
    assert np.isfinite(data).all(), f"FAIL[{label}]: non-finite samples"
    peak = float(np.max(np.abs(data)))
    assert peak <= 1.0 + 1e-6, f"FAIL[{label}]: clipping ({peak})"
    clicks = _click_count(mono)
    print(f"Click-candidate discontinuities: {clicks}")
    assert clicks == 0, f"FAIL[{label}]: {clicks} clicks detected"
    silence_result = audio.detect_silence_regions(
        final_out, threshold_db=-45.0, min_duration=2.0
    )
    mid_silences = [
        r for r in silence_result["data"]["regions"]
        if r[0] > 0.5 and r[1] < duration - 0.5
    ]
    assert not mid_silences, f"FAIL[{label}]: dropout gap {mid_silences}"
    print(f"PASS[{label}]: no clicks, no pops, no dropouts, no clipping")


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="autopilot_phase3_qa_"))
    print(f"Working directory: {tmp_root}")
    container = ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_root / "autopilot.db"),
            "schema_path": str(ROOT / "database" / "schema.sql"),
            "config_folder": str(ROOT / "config"),
            "cache_folder": str(tmp_root / "cache"),
            "log_folder": str(tmp_root / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=ROOT,
    )
    tts = TTSEngineManager(container)
    audio = AudioProcessor(container)

    render_and_check(tts, audio, tmp_root, 30.0, "30s_pauses_breathing")
    render_and_check(tts, audio, tmp_root, 300.0, "5min_pauses_breathing")

    print("\n=== ALL PHASE 3 QA CHECKS PASSED (30s + 5min, pauses+breathing) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
