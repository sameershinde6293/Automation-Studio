"""Phase 2 QA: render 30-second and 5-minute narration samples through
the rebuilt voice-effects chain and verify the acceptance criteria:

  - No clicks / No pops / No dropouts / No clipping
  - Consistent loudness
  - Natural voice quality (no NaN/Inf, no degenerate/silent stretches)
  - Effects can be safely enabled/disabled (both configurations render
    valid, clean audio)

Run from anywhere with:
    python scripts/qa_render_phase2.py
"""

from __future__ import annotations

import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Dict

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.service_container import ServiceContainer  # noqa: E402
from modules.audio_processor import AudioProcessor  # noqa: E402
from modules.tts_engine_manager import TTSEngineManager  # noqa: E402

SENTENCES = [
    "Welcome to this documentary about the history of ancient civilizations.",
    "Long before recorded history, human societies began to form complex structures.",
    "[PAUSE:MEDIUM] Trade routes connected distant lands across deserts and seas.",
    "Great cities rose from humble beginnings, shaped by rivers and mountains.",
    "[PAUSE:LONG] And yet, for all their power, every empire eventually fades.",
    "In the chapters that follow, we will explore how these worlds were built, "
    "and how they fell.",
    "Scholars have long debated the causes of these dramatic collapses.",
    "Climate, conflict, and internal strife are often cited as contributing factors.",
    "[PAUSE:SHORT] But the full picture is almost always more complicated than that.",
    "Join us as we examine the evidence, one civilization at a time.",
]


def _build_lines(tts: TTSEngineManager, tmp: Path, profile: Dict, target_seconds: float):
    line_paths = []
    total = 0.0
    i = 0
    while total < target_seconds:
        text = SENTENCES[i % len(SENTENCES)]
        out = tmp / f"line_{i:04d}.wav"
        result = tts.generate_audio(text, profile, out)
        assert result["success"], result
        line_paths.append(result["data"]["audio_path"])
        total += result["data"]["duration"]
        i += 1
    return line_paths, total


def _qa_checks(final_out: Path, audio: AudioProcessor, label: str) -> None:
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
    print(f"\n--- QA: {label} ({duration:.1f}s) ---")

    assert np.isfinite(data).all(), f"FAIL[{label}]: non-finite samples present"
    print(f"PASS[{label}]: no NaN/Inf samples")

    peak = float(np.max(np.abs(data)))
    print(f"Peak amplitude: {peak:.4f} ({20 * np.log10(max(peak, 1e-9)):.2f} dBFS)")
    assert peak <= 1.0 + 1e-6, f"FAIL[{label}]: clipping detected"
    print(f"PASS[{label}]: no clipping")

    first_sample = float(abs(mono[0]))
    last_sample = float(abs(mono[-1]))
    assert first_sample < 0.05, f"FAIL[{label}]: hard/abrupt narration start"
    assert last_sample < 0.05, f"FAIL[{label}]: hard/abrupt narration end"
    print(f"PASS[{label}]: smooth start/end")

    diffs = np.abs(np.diff(mono))
    med = float(np.median(diffs)) + 1e-9
    spikes = np.flatnonzero((diffs > 0.25) & (diffs > med * 12.0))
    print(f"Click-candidate discontinuities: {len(spikes)}")
    assert len(spikes) == 0, f"FAIL[{label}]: {len(spikes)} click-like discontinuities"
    print(f"PASS[{label}]: no clicks/pops detected")

    silence_result = audio.detect_silence_regions(
        final_out, threshold_db=-45.0, min_duration=1.5
    )
    mid_silences = [
        r for r in silence_result["data"]["regions"]
        if r[0] > 0.5 and r[1] < duration - 0.5
    ]
    print(f"Unexpected mid-track silences (>1.5s): {mid_silences}")
    assert not mid_silences, f"FAIL[{label}]: unexpected dropout/silence gap"
    print(f"PASS[{label}]: no dropouts")

    # Consistent loudness: windowed RMS shouldn't swing wildly once
    # normalized to LUFS by generate_final_mix.
    window = int(2.0 * sr)
    if len(mono) >= window * 2:
        rms_windows = [
            float(np.sqrt(np.mean(mono[i:i + window] ** 2)))
            for i in range(0, len(mono) - window, window)
            if np.abs(mono[i:i + window]).max() > 0.01  # skip pure silence windows
        ]
        if len(rms_windows) >= 2:
            rms_db = [20 * np.log10(max(r, 1e-9)) for r in rms_windows]
            spread = max(rms_db) - min(rms_db)
            print(f"Windowed loudness spread across track: {spread:.2f} dB")
            assert spread < 12.0, f"FAIL[{label}]: inconsistent loudness ({spread:.1f} dB)"
            print(f"PASS[{label}]: consistent loudness")


def render_sample(
    tts: TTSEngineManager,
    audio: AudioProcessor,
    tmp_root: Path,
    target_seconds: float,
    effects_enabled: bool,
    label: str,
) -> Path:
    tmp = tmp_root / label
    tmp.mkdir(parents=True, exist_ok=True)

    profile = {
        "engine": "piper",
        "voice_model": "default",
        "speed": 1.0,
        "pitch": 0,
        "volume": 1.0,
        "default_emotion": "neutral",
        "reverb_preset": "subtle_room" if effects_enabled else "none",
        "eq_preset": "documentary_male" if effects_enabled else "flat",
        "breathing_enabled": True,
        "highpass_enabled": effects_enabled,
        "noise_gate_enabled": effects_enabled,
        "compression_enabled": effects_enabled,
        "limiter_enabled": effects_enabled,
    }

    t0 = time.perf_counter()
    line_paths, total_line_duration = _build_lines(tts, tmp, profile, target_seconds)
    print(
        f"[{label}] generated {len(line_paths)} lines, "
        f"{total_line_duration:.1f}s raw narration in "
        f"{time.perf_counter() - t0:.1f}s wall clock"
    )

    narration_out = tmp / "narration.wav"
    built = audio.build_narration_track(
        f"phase2-qa-{label}", line_paths, narration_out, pause_seconds=0.3
    )
    assert built["success"], built

    music_path = tmp / "music.wav"
    sr = 48000
    music_len = int(max(30.0, built["data"]["duration"]) * sr)
    t = np.arange(music_len) / sr
    music = 0.2 * np.sin(2 * np.pi * 220 * t)
    audio._write_audio(music_path, np.stack([music, music], axis=1), sr)

    final_out = tmp / "final_mix.wav"
    mix_result = audio.generate_final_mix(
        f"phase2-qa-{label}",
        final_out,
        {"narration_path": str(narration_out), "music_path": str(music_path)},
    )
    assert mix_result["success"], mix_result
    print(f"[{label}] final mix: {mix_result['data']}")
    return final_out


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="autopilot_phase2_qa_"))
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

    # 30-second sample, effects ON.
    out_30_on = render_sample(tts, audio, tmp_root, 30.0, True, "30s_effects_on")
    _qa_checks(out_30_on, audio, "30s effects ON")

    # 30-second sample, effects OFF (bypass) -- must also render cleanly.
    out_30_off = render_sample(tts, audio, tmp_root, 30.0, False, "30s_effects_off")
    _qa_checks(out_30_off, audio, "30s effects OFF")

    # 5-minute sample, effects ON.
    out_5m_on = render_sample(tts, audio, tmp_root, 300.0, True, "5min_effects_on")
    _qa_checks(out_5m_on, audio, "5min effects ON")

    print("\n=== ALL PHASE 2 QA CHECKS PASSED (30s + 5min, effects on/off) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
