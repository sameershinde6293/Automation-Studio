"""Phase 1 QA: render a ~30s narration through the real production
pipeline (TTSEngineManager + AudioProcessor) and verify:
  - no clicks / dropouts
  - no hard intro (smooth start)
  - no clipping
  - finite (no NaN/Inf) samples throughout

Run from anywhere with:
    python scripts/qa_render_30s_phase1.py
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.service_container import ServiceContainer  # noqa: E402
from modules.audio_processor import AudioProcessor  # noqa: E402
from modules.tts_engine_manager import TTSEngineManager  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="autopilot_phase1_qa_"))
    print(f"Working directory: {tmp}")

    container = ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp / "autopilot.db"),
            "schema_path": str(ROOT / "database" / "schema.sql"),
            "config_folder": str(ROOT / "config"),
            "cache_folder": str(tmp / "cache"),
            "log_folder": str(tmp / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=ROOT,
    )

    tts = TTSEngineManager(container)
    audio = AudioProcessor(container)

    profile = {
        "engine": "piper",  # falls back to a synthetic tone if not installed
        "voice_model": "default",
        "speed": 1.0,
        "pitch": 0,
        "volume": 1.0,
        "default_emotion": "neutral",
        "reverb_preset": "none",
        "eq_preset": "flat",
        "breathing_enabled": True,
    }

    # ~30 seconds of narration split into several lines (sentences),
    # matching how core_engine._stage_tts generates one WAV per line.
    sentences = [
        "Welcome to this documentary about the history of ancient civilizations.",
        "Long before recorded history, human societies began to form complex structures.",
        "[PAUSE:MEDIUM] Trade routes connected distant lands across deserts and seas.",
        "Great cities rose from humble beginnings, shaped by rivers and mountains.",
        "[PAUSE:LONG] And yet, for all their power, every empire eventually fades.",
        "In the chapters that follow, we will explore how these worlds were built, "
        "and how they fell.",
    ]

    line_paths = []
    total_duration = 0.0
    for i, text in enumerate(sentences):
        out = tmp / f"line_{i:02d}.wav"
        result = tts.generate_audio(text, profile, out)
        assert result["success"], result
        line_paths.append(result["data"]["audio_path"])
        total_duration += result["data"]["duration"]
        print(
            f"line {i}: duration={result['data']['duration']:.2f}s "
            f"engine={result['data'].get('engine')}"
        )
    print(f"Total raw line duration: {total_duration:.2f}s")

    narration_out = tmp / "narration.wav"
    built = audio.build_narration_track(
        "phase1-qa", line_paths, narration_out, pause_seconds=0.3
    )
    assert built["success"], built
    print(f"Narration track duration: {built['data']['duration']:.2f}s")

    # Background music bed so the full mix -> limiter -> LUFS chain (the
    # actual production final-mix path) is exercised end to end.
    music_path = tmp / "music.wav"
    sr = 48000
    music_len = int(max(30.0, built["data"]["duration"]) * sr)
    t = np.arange(music_len) / sr
    music = 0.25 * np.sin(2 * np.pi * 220 * t)
    audio._write_audio(music_path, np.stack([music, music], axis=1), sr)

    final_out = tmp / "final_mix.wav"
    mix_result = audio.generate_final_mix(
        "phase1-qa",
        final_out,
        {"narration_path": str(narration_out), "music_path": str(music_path)},
    )
    assert mix_result["success"], mix_result
    print("Final mix:", mix_result["data"])

    # -----------------------------------------------------------------
    # QA checks
    # -----------------------------------------------------------------
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
    print(f"\nFinal mix: sr={sr} ch={ch} duration={duration:.2f}s")

    # 1. NaN/Inf check
    assert np.isfinite(data).all(), "FAIL: non-finite samples present"
    print("PASS: no NaN/Inf samples")

    # 2. Clipping check
    peak = float(np.max(np.abs(data)))
    print(f"Peak amplitude: {peak:.4f} ({20 * np.log10(peak):.2f} dBFS)")
    assert peak <= 1.0 + 1e-6, "FAIL: clipping detected"
    print("PASS: no clipping")

    # 3. Hard intro check: the waveform must begin AT (or very near) zero
    # and ramp up smoothly -- not jump straight to full amplitude on the
    # very first sample (that jump is what a listener perceives as an
    # abrupt/hard start, not the eventual loudness of the clip).
    first_sample = float(abs(mono[0]))
    print(f"First sample amplitude: {first_sample:.4f}")
    assert first_sample < 0.05, "FAIL: hard/abrupt narration start"
    first_ms_diffs = np.abs(np.diff(mono[: int(0.01 * sr)]))
    max_early_step = float(np.max(first_ms_diffs)) if first_ms_diffs.size else 0.0
    print(f"Largest single-sample jump in first 10ms: {max_early_step:.4f}")
    assert max_early_step < 0.05, "FAIL: abrupt jump at narration start"
    print("PASS: smooth start (no hard intro)")

    last_sample = float(abs(mono[-1]))
    print(f"Last sample amplitude: {last_sample:.4f}")
    assert last_sample < 0.05, "FAIL: hard/abrupt narration end"
    print("PASS: smooth end")

    # 4. Click/dropout check: large sample-to-sample discontinuities far
    # beyond the local median step (the same detector used in production).
    diffs = np.abs(np.diff(mono))
    med = float(np.median(diffs)) + 1e-9
    spikes = np.flatnonzero((diffs > 0.25) & (diffs > med * 12.0))
    print(f"Click-candidate discontinuities: {len(spikes)}")
    assert len(spikes) == 0, f"FAIL: {len(spikes)} click-like discontinuities found"
    print("PASS: no clicks/pops detected")

    # 5. Dropout check: no unexpected long silence gaps mid-narration.
    silence_result = audio.detect_silence_regions(
        final_out, threshold_db=-45.0, min_duration=1.5
    )
    long_silences = silence_result["data"]["regions"]
    mid_silences = [r for r in long_silences if r[0] > 0.5 and r[1] < duration - 0.5]
    print(f"Unexpected mid-track silences (>1.5s): {mid_silences}")
    assert not mid_silences, "FAIL: unexpected dropout/silence gap"
    print("PASS: no dropouts")

    print("\n=== ALL PHASE 1 QA CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
