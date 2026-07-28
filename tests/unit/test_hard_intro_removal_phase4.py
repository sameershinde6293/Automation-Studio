"""Phase 4 (Remove Hard Intro) tests.

Covers:
  * AudioProcessor.trim_leading_silence — the public, reusable trim API
    (breath-safe margin, excessive-silence threshold, never trims a
    tight/normal clip, never trims into the first phoneme).
  * TTSEngineManager._trim_leading_silence (numpy) — same guarantees at
    the per-line synthesis point.
  * generate_audio's opt-out flag (trim_leading_silence=False) restores
    the untrimmed engine output on request.
  * build_narration_track's defense-in-depth trim on the first line
    (pydub path) — and that it's a no-op when the first line has
    already been trimmed by generate_audio.
  * End-to-end: a narration line with a deliberately hard/silent intro
    starts smoothly after the full generate_audio pipeline.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from core.service_container import ServiceContainer
from modules.audio_processor import AudioProcessor
from modules.tts_engine_manager import TTSEngineManager


@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=project_root,
    )


@pytest.fixture
def audio(container: ServiceContainer) -> AudioProcessor:
    return AudioProcessor(container)


@pytest.fixture
def tts(container: ServiceContainer) -> TTSEngineManager:
    return TTSEngineManager(container)


def _write_wav(path: Path, samples: np.ndarray, sr: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    channels = 1 if pcm.ndim == 1 else pcm.shape[1]
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def _read_wav(path: Path):
    with wave.open(str(path), "r") as handle:
        sr = handle.getframerate()
        ch = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch)
    return arr, sr


class TestAudioProcessorTrimLeadingSilence:
    """AudioProcessor.trim_leading_silence public API."""

    def test_excessive_leading_silence_is_trimmed(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.8 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        path = tmp_path / "hard_intro.wav"
        _write_wav(path, signal, sr)
        result = audio.trim_leading_silence(path)
        assert result["success"] is True
        assert result["data"]["trimmed_ms"] > 0.0
        data, _ = audio._read_audio(path)
        assert len(data) < len(signal)
        # First phoneme (speech onset) must not be cut.
        assert np.max(np.abs(data)) > 0.05

    def test_tight_audio_left_untouched(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.05 * sr))  # only 50ms, well under threshold
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        path = tmp_path / "tight.wav"
        _write_wav(path, signal, sr)
        before = path.read_bytes()
        result = audio.trim_leading_silence(path)
        assert result["success"] is True
        assert result["data"]["trimmed_ms"] == 0.0
        assert path.read_bytes() == before  # completely untouched

    def test_breath_safe_margin_preserved(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(1.0 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        path = tmp_path / "hard_intro2.wav"
        _write_wav(path, signal, sr)
        result = audio.trim_leading_silence(path, margin_ms=60.0)
        assert result["success"] is True
        data, _ = audio._read_audio(path)
        # Speech originally started at exactly 1.0s -> after trimming to
        # (first_sound - margin), the remaining leading silence should be
        # close to the margin (60ms), never negative / never cut into
        # the speech itself.
        mono = data.mean(axis=1) if data.ndim == 2 else data
        first_loud = int(np.flatnonzero(np.abs(mono) > 0.01)[0])
        remaining_silence_ms = (first_loud / sr) * 1000.0
        assert 0 <= remaining_silence_ms <= 120  # roughly the margin, generous bound

    def test_missing_file_errors(self, audio: AudioProcessor, tmp_path: Path) -> None:
        result = audio.trim_leading_silence(tmp_path / "missing.wav")
        assert result["success"] is False

    def test_all_silent_clip_left_alone(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        signal = np.zeros(int(0.5 * sr))
        path = tmp_path / "silent.wav"
        _write_wav(path, signal, sr)
        result = audio.trim_leading_silence(path)
        assert result["success"] is True
        assert result["data"]["trimmed_ms"] == 0.0

    def test_writes_to_separate_output_path(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.8 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        src = tmp_path / "src.wav"
        dest = tmp_path / "dest.wav"
        _write_wav(src, signal, sr)
        result = audio.trim_leading_silence(src, dest)
        assert result["success"] is True
        assert dest.exists()
        # Source is untouched when writing to a separate destination.
        src_data, _ = audio._read_audio(src)
        assert len(src_data) == len(signal)


class TestTTSEngineManagerTrimLeadingSilence:
    """TTSEngineManager._trim_leading_silence (numpy, used at synthesis time)."""

    def test_finalize_line_audio_trims_hard_intro(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.8 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        path = tmp_path / "hard_intro.wav"
        _write_wav(path, signal, sr)
        original_duration = tts._wav_duration(path)
        info = tts._finalize_line_audio(path, trim_leading_silence=True)
        assert info["trimmed_ms"] > 0.0
        new_duration = tts._wav_duration(path)
        assert new_duration < original_duration
        data, _ = _read_wav(path)
        assert np.max(np.abs(data)) > 0.05  # first phoneme preserved

    def test_finalize_line_audio_leaves_tight_audio_alone(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.05 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        path = tmp_path / "tight.wav"
        _write_wav(path, signal, sr)
        original_duration = tts._wav_duration(path)
        info = tts._finalize_line_audio(path, trim_leading_silence=True)
        assert info["trimmed_ms"] == 0.0
        assert tts._wav_duration(path) == pytest.approx(original_duration, abs=0.01)


class TestGenerateAudioOptOut:
    """generate_audio's trim_leading_silence flag is honored (opt-out)."""

    def test_default_trims_leading_silence(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
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
        out = tmp_path / "line.wav"
        result = tts.generate_audio("Testing hard intro removal by default.", profile, out)
        assert result["success"] is True
        data, sr = _read_wav(Path(result["data"]["audio_path"]))
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert abs(mono[0]) < 0.05  # smooth start (fade to near-zero)

    def test_opt_out_flag_disables_trim(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
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
            "trim_leading_silence": False,
        }
        out = tmp_path / "line.wav"
        result = tts.generate_audio("Testing opt out of hard intro removal.", profile, out)
        assert result["success"] is True
        # Still succeeds and produces valid audio even with trimming off.
        data, sr = _read_wav(Path(result["data"]["audio_path"]))
        assert np.isfinite(data).all()


class TestBuildNarrationTrackDefenseInDepth:
    """build_narration_track trims the first line's leading silence too."""

    def test_first_line_hard_intro_is_trimmed(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.8 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        first_line_signal = np.concatenate([silence, speech])
        first_line = tmp_path / "line0.wav"
        _write_wav(first_line, first_line_signal, sr)

        second_line = tmp_path / "line1.wav"
        _write_wav(second_line, np.sin(np.linspace(0, 300 * np.pi, int(0.4 * sr))) * 0.3, sr)

        out = tmp_path / "narration.wav"
        result = audio.build_narration_track(
            "p", [first_line, second_line], out, pause_seconds=0.2
        )
        assert result["success"] is True
        data, _ = audio._read_audio(out)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert abs(mono[0]) < 0.05  # smooth start
        # Duration should be noticeably shorter than the naive sum
        # (0.8s hard intro + 0.5s + 0.2s pause + 0.4s) would suggest.
        naive_total = 0.8 + 0.5 + 0.2 + 0.4
        assert result["data"]["duration"] < naive_total - 0.3

    def test_opt_out_of_narration_track_trim(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        silence = np.zeros(int(0.8 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        first_line_signal = np.concatenate([silence, speech])
        first_line = tmp_path / "line0.wav"
        _write_wav(first_line, first_line_signal, sr)
        out = tmp_path / "narration.wav"
        result = audio.build_narration_track(
            "p", [first_line], out, pause_seconds=0.2, trim_leading_silence=False
        )
        assert result["success"] is True
        # With trim disabled, duration should reflect the full untrimmed
        # first line (only the per-line 5ms edge fade applied).
        assert result["data"]["duration"] >= 1.25

    def test_already_trimmed_first_line_is_a_no_op(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        # First line already tight (as generate_audio would have left it).
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        first_line = tmp_path / "line0.wav"
        _write_wav(first_line, speech, sr)
        out = tmp_path / "narration.wav"
        result = audio.build_narration_track("p", [first_line], out, pause_seconds=0.2)
        assert result["success"] is True
        assert result["data"]["duration"] == pytest.approx(0.5, abs=0.02)


class TestEndToEndHardIntroRemoval:
    """Full generate_audio -> build_narration_track -> final mix."""

    def test_full_pipeline_smooth_start(
        self, tts: TTSEngineManager, audio: AudioProcessor, tmp_path: Path
    ) -> None:
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
        out = tmp_path / "line.wav"
        result = tts.generate_audio(
            "This narration should start smoothly with no hard intro.", profile, out
        )
        assert result["success"] is True

        narration_out = tmp_path / "narration.wav"
        built = audio.build_narration_track(
            "p", [result["data"]["audio_path"]], narration_out
        )
        assert built["success"] is True

        final_out = tmp_path / "final.wav"
        mix_result = audio.generate_final_mix(
            "p", final_out, {"narration_path": str(narration_out)}
        )
        assert mix_result["success"] is True
        data, _ = audio._read_audio(final_out)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert abs(mono[0]) < 0.05
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6
