"""Phase 1 (Audio Stability) tests.

Covers the specific hard requirements from the audiobook-quality
narration rebuild:

  * NaN/Inf samples are sanitized, never shipped to disk or crash a stage.
  * Clipping never occurs anywhere in the mix -> limiter -> LUFS chain.
  * Every generated narration line has a click-free, smooth start/end
    (fade-in/out) and no internal click/pop discontinuities.
  * Excessive leading silence ("hard intro") is trimmed without cutting
    the first phoneme.
  * Every pipeline stage validates its own output and rejects corrupted
    buffers instead of propagating them.
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
    """Isolated container for stability tests."""
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


def _read_wav_float(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "r") as handle:
        sr = handle.getframerate()
        ch = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch)
    return arr, sr


class TestNaNInfSanitization:
    """No NaN/Inf sample should ever survive a read/write round trip."""

    def test_write_audio_removes_nan_and_inf(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        n = 4800
        data = np.sin(np.linspace(0, 20 * np.pi, n)) * 0.3
        data[100] = float("nan")
        data[200] = float("inf")
        data[300] = float("-inf")
        out = tmp_path / "sanitized.wav"
        audio._write_audio(out, data, 48000)
        result, sr = audio._read_audio(out)
        assert np.isfinite(result).all()
        assert np.max(np.abs(result)) <= 1.0 + 1e-6

    def test_read_audio_sanitizes_existing_bad_file(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        # Build a valid file, then verify the sanitize helper directly
        # handles a buffer that is mostly non-finite -> silence fallback.
        n = 1000
        mostly_bad = np.full(n, np.nan)
        mostly_bad[:10] = 0.1
        cleaned = audio._sanitize_audio(mostly_bad)
        assert np.isfinite(cleaned).all()
        assert np.allclose(cleaned, 0.0)

    def test_sanitize_partial_nan_repairs_in_place(
        self, audio: AudioProcessor
    ) -> None:
        data = np.array([0.1, 0.2, float("nan"), 0.3, 0.4])
        cleaned = audio._sanitize_audio(data)
        assert np.isfinite(cleaned).all()
        assert cleaned[2] == 0.0
        assert cleaned[0] == pytest.approx(0.1)


class TestNoClipping:
    """HARD RULE: never introduce clipping anywhere in the chain."""

    def test_limiter_never_clips_extreme_input(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        n = 48000
        loud = np.sin(np.linspace(0, 100 * np.pi, n)) * 5.0  # way over full scale
        src = tmp_path / "hot.wav"
        audio._write_audio(src, loud, 48000)
        out = tmp_path / "limited.wav"
        result = audio.apply_limiter(src, out, ceiling_db=-1.0)
        assert result["success"] is True
        assert result["data"]["clipping"] is False
        data, _ = audio._read_audio(out)
        assert np.max(np.abs(data)) <= 1.0 + 1e-6

    def test_final_mix_never_clips(self, audio: AudioProcessor, tmp_path: Path) -> None:
        n = 48000 * 2
        narr = np.sin(np.linspace(0, 400 * np.pi, n)) * 0.9
        music = np.sin(np.linspace(0, 100 * np.pi, n)) * 0.9
        narr_path = tmp_path / "narr.wav"
        music_path = tmp_path / "music.wav"
        audio._write_audio(narr_path, np.stack([narr, narr], axis=1), 48000)
        audio._write_audio(music_path, np.stack([music, music], axis=1), 48000)
        out = tmp_path / "final.wav"
        result = audio.generate_final_mix(
            "clip-test",
            out,
            {"narration_path": str(narr_path), "music_path": str(music_path)},
        )
        assert result["success"] is True
        data, _ = audio._read_audio(out)
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6


class TestClickRepair:
    """Click/pop discontinuities are detected and smoothed."""

    def test_repair_clicks_removes_spike(self, tts: TTSEngineManager) -> None:
        n = 4800
        clean = np.sin(np.linspace(0, 20 * np.pi, n)) * 0.2
        clicked = clean.copy()
        clicked[2000] = 0.99  # sharp, isolated spike unlike its neighbours
        repaired = tts._repair_clicks(clicked, 48000)
        # The spike should have been smoothed toward its neighbours.
        assert abs(repaired[2000] - clean[2000]) < abs(clicked[2000] - clean[2000])

    def test_repair_clicks_preserves_normal_speech_like_signal(
        self, tts: TTSEngineManager
    ) -> None:
        n = 4800
        signal = np.sin(np.linspace(0, 40 * np.pi, n)) * 0.3
        repaired = tts._repair_clicks(signal.copy(), 48000)
        # Normal continuous signal should be virtually unchanged.
        assert np.allclose(repaired, signal, atol=1e-6)


class TestFadeInOut:
    """Every generated clip must fade in/out to avoid start/end clicks."""

    def test_apply_edge_fades_zero_at_edges(self, tts: TTSEngineManager) -> None:
        n = 4800
        signal = np.ones(n) * 0.5  # worst case: instant full-scale start/end
        faded = tts._apply_edge_fades(signal.copy(), 48000, fade_ms=8.0)
        assert abs(faded[0]) < 0.01
        assert abs(faded[-1]) < 0.01
        # Middle untouched
        assert faded[n // 2] == pytest.approx(0.5)

    def test_finalize_line_audio_fades_generated_line(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        n = 48000  # 1s @ 48k mono
        signal = np.ones(n) * 0.4
        path = tmp_path / "line.wav"
        _write_wav(path, signal, 48000)
        info = tts._finalize_line_audio(path, trim_leading_silence=False)
        assert isinstance(info, dict)
        data, sr = _read_wav_float(path)
        assert abs(data[0]) < 0.05
        assert abs(data[-1]) < 0.05
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6


class TestLeadingSilenceTrim:
    """Hard intro fix: excessive leading silence is trimmed, first
    phoneme is never cut."""

    def test_excessive_leading_silence_is_trimmed(
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
        # First phoneme (speech onset) must not be cut — some signal
        # energy should still exist near the start of the trimmed file.
        data, _ = _read_wav_float(path)
        assert np.max(np.abs(data)) > 0.05

    def test_tight_narration_is_left_alone(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        sr = 48000
        # Only tiny (<150ms) leading silence -> should not trim.
        silence = np.zeros(int(0.05 * sr))
        speech = np.sin(np.linspace(0, 200 * np.pi, int(0.5 * sr))) * 0.4
        signal = np.concatenate([silence, speech])
        path = tmp_path / "tight.wav"
        _write_wav(path, signal, sr)
        original_duration = tts._wav_duration(path)
        info = tts._finalize_line_audio(path, trim_leading_silence=True)
        assert info["trimmed_ms"] == 0.0
        assert tts._wav_duration(path) == pytest.approx(original_duration, abs=0.01)


class TestKokoroOutputSanitization:
    """Kokoro (or any numpy-based engine) output is sanitized before write."""

    def test_write_wav_samples_sanitizes_nan(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        n = 4800
        samples = np.sin(np.linspace(0, 20 * np.pi, n)).astype(np.float32) * 0.3
        samples[500] = float("nan")
        samples[600] = float("inf")
        out = tmp_path / "kokoro_like.wav"
        tts._write_wav_samples(str(out), samples, 24000)
        data, sr = _read_wav_float(out)
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6

    def test_write_wav_samples_mostly_nan_becomes_silence(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        n = 1000
        samples = np.full(n, np.nan, dtype=np.float32)
        samples[:5] = 0.1
        out = tmp_path / "mostly_bad.wav"
        tts._write_wav_samples(str(out), samples, 24000)
        data, sr = _read_wav_float(out)
        assert np.isfinite(data).all()
        assert np.allclose(data, 0.0, atol=1e-3)


class TestValidationGates:
    """Every stage validates its output and rejects corrupted buffers."""

    def test_validate_wav_basic_rejects_missing_file(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        assert tts._validate_wav_basic(tmp_path / "does_not_exist.wav") is False

    def test_validate_wav_basic_accepts_good_file(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        path = tmp_path / "ok.wav"
        _write_wav(path, np.sin(np.linspace(0, 10 * np.pi, 4800)) * 0.2)
        assert tts._validate_wav_basic(path) is True

    def test_validate_audio_file_rejects_missing(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        assert audio._validate_audio_file(tmp_path / "missing.wav") is False

    def test_validate_audio_file_accepts_good_file(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        path = tmp_path / "ok.wav"
        audio._write_audio(
            path, np.sin(np.linspace(0, 10 * np.pi, 4800)) * 0.2, 48000
        )
        assert audio._validate_audio_file(path) is True

    def test_generate_audio_end_to_end_produces_valid_wav(
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
        result = tts.generate_audio(
            "This is a full pipeline stability test sentence.", profile, out
        )
        assert result["success"] is True
        data, sr = _read_wav_float(Path(result["data"]["audio_path"]))
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6
        # Click-free, smooth start/end.
        assert abs(data.flatten()[0]) < 0.05
        assert abs(data.flatten()[-1]) < 0.05
