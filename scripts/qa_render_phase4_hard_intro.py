"""Phase 4 (Remove Hard Intro) QA.

Renders 30-second and 5-minute narration samples — including a
deliberately "hard intro" first line (synthetic silence prepended) to
prove the fix actually engages — through the real production pipeline
and verifies:

  - The final program starts smoothly (no hard/abrupt intro)
  - The first phoneme is never clipped (some real signal remains near
    the start once trimmed)
  - No clicks / no dropouts / no clipping (no regression vs. earlier
    phases)

Run from anywhere with:
    python scripts/qa_render_phase4_hard_intro.py
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
]


def _prepend_hard_intro_silence(path: Path, silence_seconds: float = 0.9) -> None:
    """Simulate a TTS engine's known "hard intro" failure mode: excess
    dead air before the voice actually starts (some Piper/Kokoro voices
    and models are known to do this on certain inputs)."""
    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        ch = handle.getnchannels()
        sampwidth = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    silence = b"\x00" * int(silence_seconds * sr * ch * sampwidth)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(ch)
        handle.setsampwidth(sampwidth)
        handle.setframerate(sr)
        handle.writeframes(silence + raw)


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
        "reverb_preset": "none",
        "eq_preset": "flat",
        "breathing_enabled": False,
    }

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
    print(f"[{label}] {len(line_paths)} lines, {total:.1f}s raw narration")

    # Simulate the hard-intro failure mode directly on the FIRST raw
    # engine output, bypassing generate_audio's own trim (which would
    # otherwise catch it at the source) -- this proves the defense-in-
    # depth trim in build_narration_track independently does its job.
    _prepend_hard_intro_silence(Path(line_paths[0]))

    narration_out = tmp / "narration.wav"
    built = audio.build_narration_track(
        f"phase4-qa-{label}", line_paths, narration_out, pause_seconds=0.3
    )
    assert built["success"], built

    final_out = tmp / "final_mix.wav"
    mix_result = audio.generate_final_mix(
        f"phase4-qa-{label}", final_out, {"narration_path": str(narration_out)}
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
    print(f"PASS[{label}]: no NaN/Inf, no clipping (peak {peak:.3f})")

    # Hard intro check: smooth start (near-zero first sample, no jump),
    # and the first phoneme's energy shows up quickly (not buried behind
    # nearly a second of dead air like the injected failure would cause).
    first_sample = float(abs(mono[0]))
    assert first_sample < 0.05, f"FAIL[{label}]: hard/abrupt narration start"
    window_300ms = int(0.3 * sr)
    early_peak = float(np.max(np.abs(mono[:window_300ms])))
    print(f"First-sample amplitude: {first_sample:.4f}, "
          f"peak within first 300ms: {early_peak:.4f}")
    assert early_peak > 0.05, (
        f"FAIL[{label}]: no real signal within the first 300ms -- "
        "hard intro silence was NOT removed"
    )
    print(f"PASS[{label}]: hard intro removed, smooth breath-safe start")

    diffs = np.abs(np.diff(mono))
    med = float(np.median(diffs)) + 1e-9
    spikes = np.flatnonzero((diffs > 0.25) & (diffs > med * 12.0))
    assert len(spikes) == 0, f"FAIL[{label}]: {len(spikes)} clicks detected"
    print(f"PASS[{label}]: no clicks/pops (regression check)")

    # min_duration is intentionally generous: the [PAUSE:LONG] tag used
    # in SENTENCES above can legitimately produce up to ~2.25s of
    # silence (base 2.0s +/- 0.25s variation), plus the 0.3s inter-line
    # pause when adjacent -- a deliberate narration pause, not a
    # dropout, so the threshold sits comfortably above that combined
    # maximum to avoid a false positive.
    silence_result = audio.detect_silence_regions(
        final_out, threshold_db=-45.0, min_duration=3.0
    )
    mid_silences = [
        r for r in silence_result["data"]["regions"]
        if r[0] > 0.5 and r[1] < duration - 0.5
    ]
    assert not mid_silences, f"FAIL[{label}]: dropout gap {mid_silences}"
    print(f"PASS[{label}]: no dropouts (regression check)")


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="autopilot_phase4_qa_"))
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

    render_and_check(tts, audio, tmp_root, 30.0, "30s")
    render_and_check(tts, audio, tmp_root, 300.0, "5min")

    print("\n=== ALL PHASE 4 (REMOVE HARD INTRO) QA CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
