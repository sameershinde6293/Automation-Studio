"""Phase 3 (Click & Transition Refinement) QA.

Renders 30-second, 5-minute, and 30-minute narration samples (pauses +
breathing enabled, exercising every splice/join path refined in this
phase) through the real production pipeline and verifies:

  - Zero clicks / zero pops (sample-discontinuity detector)
  - Smooth transitions (no loudness "dip" bigger than expected at any
    line join / pause splice / breath overlay)
  - No dropouts
  - No clipping
  - No regressions vs. earlier phases (NaN/Inf, smooth start/end)

Run from anywhere with:
    python scripts/qa_render_phase3_transitions.py [--skip-30min]
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
    t1 = time.perf_counter()
    built = audio.build_narration_track(
        f"phase3refine-qa-{label}", line_paths, narration_out, pause_seconds=0.3
    )
    assert built["success"], built
    print(f"[{label}] narration assembly: {time.perf_counter() - t1:.1f}s wall clock")
    segment_timestamps = built["data"]["segment_timestamps"]

    final_out = tmp / "final_mix.wav"
    mix_result = audio.generate_final_mix(
        f"phase3refine-qa-{label}",
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
    print(f"PASS[{label}]: no NaN/Inf, no clipping (peak {peak:.3f})")

    clicks = _click_count(mono)
    print(f"Click-candidate discontinuities: {clicks}")
    assert clicks == 0, f"FAIL[{label}]: {clicks} clicks detected"
    print(f"PASS[{label}]: zero clicks, zero pops")

    first_sample = float(abs(mono[0]))
    last_sample = float(abs(mono[-1]))
    assert first_sample < 0.05, f"FAIL[{label}]: hard start"
    assert last_sample < 0.05, f"FAIL[{label}]: hard end"
    print(f"PASS[{label}]: smooth start/end")

    silence_result = audio.detect_silence_regions(
        final_out, threshold_db=-45.0, min_duration=2.0
    )
    mid_silences = [
        r for r in silence_result["data"]["regions"]
        if r[0] > 0.5 and r[1] < duration - 0.5
    ]
    assert not mid_silences, f"FAIL[{label}]: dropout gap {mid_silences}"
    print(f"PASS[{label}]: no dropouts")

    # Smooth-transition check: measure crossfade quality precisely AT
    # each known line-to-line join (from build_narration_track's own
    # segment_timestamps), comparing the crossfade region's minimum RMS
    # against the average RMS of full-amplitude audio just outside it on
    # both sides. A properly equal-power crossfade dips only slightly
    # (~5-10%); a bad linear/zipper crossfade or a real join click would
    # dip much further. This is join-aware, so it never confuses a
    # legitimate fade into inter-line pause silence (not a transition
    # at all) with an actual same-amplitude-to-same-amplitude crossfade.
    bad_transitions = 0
    checked = 0
    for seg_info in segment_timestamps[1:]:  # skip the first line (no join before it)
        join_ms = seg_info["start"] * 1000.0
        window_ms = 60.0
        lo = int(max(0, join_ms - window_ms) / 1000.0 * sr)
        hi = int(min(duration * 1000.0, join_ms + window_ms) / 1000.0 * sr)
        far_lo = int(max(0, join_ms - window_ms * 3) / 1000.0 * sr)
        far_hi = int(min(duration * 1000.0, join_ms + window_ms * 3) / 1000.0 * sr)
        if hi <= lo or far_hi <= far_lo:
            continue
        near_region = mono[lo:hi]
        far_region = np.concatenate([mono[far_lo:lo], mono[hi:far_hi]])
        if far_region.size == 0:
            continue
        far_rms = float(np.sqrt(np.mean(far_region ** 2)))
        if far_rms < 0.02:
            continue  # this join lands in/near pause silence, not a same-level crossfade
        near_rms = float(np.sqrt(np.mean(near_region ** 2))) if near_region.size else 0.0
        checked += 1
        if near_rms < far_rms * 0.5:
            bad_transitions += 1
    print(f"Crossfade quality checked at {checked} same-level joins, "
          f"{bad_transitions} with an excessive dip")
    assert bad_transitions == 0, (
        f"FAIL[{label}]: {bad_transitions} joins had an excessive "
        "loudness dip (zipper/bad crossfade artifact)"
    )
    print(f"PASS[{label}]: smooth transitions (no zipper/dip artifacts)")


def main() -> int:
    skip_30min = "--skip-30min" in sys.argv
    tmp_root = Path(tempfile.mkdtemp(prefix="autopilot_phase3_refine_qa_"))
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
    if not skip_30min:
        render_and_check(tts, audio, tmp_root, 1800.0, "30min")

    print("\n=== ALL PHASE 3 (CLICK & TRANSITION REFINEMENT) QA CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
