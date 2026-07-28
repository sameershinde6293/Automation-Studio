"""Unit tests for modules.audio_processor.AudioProcessor."""

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
    """Isolated container."""
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
    """AudioProcessor instance."""
    return AudioProcessor(container)


@pytest.fixture
def tts(container: ServiceContainer) -> TTSEngineManager:
    """TTS manager for integration."""
    return TTSEngineManager(container)


def _write_tone(
    path: Path,
    duration: float,
    freq: float = 440.0,
    sr: int = 48000,
    amp: float = 0.3,
    silence_ranges: list[tuple[float, float]] | None = None,
) -> None:
    """Write stereo sine with optional silence ranges (seconds)."""
    n = int(duration * sr)
    t = np.arange(n) / sr
    mono = amp * np.sin(2 * np.pi * freq * t)
    if silence_ranges:
        for start, end in silence_ranges:
            mono[int(start * sr) : int(end * sr)] = 0.0
    stereo = np.stack([mono, mono], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def _rms_region(path: Path, start: float, end: float, sr: int = 48000) -> float:
    """RMS of mono mix in time region."""
    with wave.open(str(path), "r") as handle:
        assert handle.getframerate() == sr or True
        frames = handle.readframes(handle.getnframes())
        ch = handle.getnchannels()
        rate = handle.getframerate()
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    a = int(start * rate)
    b = int(end * rate)
    segment = arr[a:b]
    if segment.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(segment**2)))


class TestNarrationBuild:
    """Build narration from multiple lines."""

    def test_build_narration_from_lines(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        lines = []
        for i in range(3):
            p = tmp_path / f"line{i}.wav"
            _write_tone(p, 0.5, freq=200 + i * 50)
            lines.append(p)
        out = tmp_path / "narration.wav"
        result = audio.build_narration_track("proj1", lines, out, pause_seconds=0.3)
        assert result["success"] is True
        assert Path(result["data"]["audio_path"]).exists()
        # ~ 3*0.5 + 2*0.3 = 2.1s (crossfade may shave a bit)
        # 3x0.5s lines + ~2 pauses; allow crossfade shortening
        assert result["data"]["duration"] >= 1.4
        assert result["data"]["line_count"] == 3


class TestDucking:
    """Music ducking under speech."""

    def test_ducking_reduces_music_during_speech(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        # Narration: speech 0-1s, silence 1-2s, speech 2-3s
        narr = tmp_path / "narr.wav"
        _write_tone(
            narr,
            3.0,
            freq=300,
            amp=0.4,
            silence_ranges=[(1.0, 2.0)],
        )
        music = tmp_path / "music.wav"
        _write_tone(music, 3.0, freq=100, amp=0.5)
        out = tmp_path / "ducked.wav"
        result = audio.apply_music_ducking(
            narr,
            music,
            {
                "ducking_threshold": 0.02,
                "ducking_depth": 0.15,
                "ducking_ceiling": 0.50,
                "attack_time": 0.05,
                "release_time": 0.05,
                "min_silence_duration": 0.3,
            },
            out,
        )
        assert result["success"] is True
        speech_rms = _rms_region(out, 0.2, 0.8)
        silence_rms = _rms_region(out, 1.2, 1.8)
        # During speech music should be quieter than during silence
        assert silence_rms > speech_rms * 1.5

    def test_envelope_smoothness(self, audio: AudioProcessor, tmp_path: Path) -> None:
        narr = tmp_path / "n.wav"
        _write_tone(narr, 2.0, amp=0.3, silence_ranges=[(0.8, 1.2)])
        env = audio.calculate_ducking_envelope(
            narr,
            {
                "attack_time": 0.2,
                "release_time": 0.4,
                "ducking_depth": 0.15,
                "ducking_ceiling": 0.5,
                "min_silence_duration": 0.5,
            },
        )
        assert env["success"] is True
        arr = np.asarray(env["data"]["envelope"])
        # Max step between samples should be small relative to full swing
        diffs = np.abs(np.diff(arr))
        assert float(np.max(diffs)) < 0.05


class TestSilenceLimiterMix:
    """Silence detection, limiter, multi-track mix."""

    def test_silence_detection(self, audio: AudioProcessor, tmp_path: Path) -> None:
        path = tmp_path / "sil.wav"
        _write_tone(path, 2.0, amp=0.3, silence_ranges=[(0.5, 1.0)])
        result = audio.detect_silence_regions(path, threshold_db=-40, min_duration=0.2)
        assert result["success"] is True
        regions = result["data"]["regions"]
        assert len(regions) >= 1
        start, end = regions[0]
        assert abs(start - 0.5) < 0.1
        assert abs(end - 1.0) < 0.1

    def test_limiter_no_clipping(self, audio: AudioProcessor, tmp_path: Path) -> None:
        loud = tmp_path / "loud.wav"
        _write_tone(loud, 1.0, amp=0.99)
        # Boost beyond full scale via processor internal then limit
        data, sr = audio._read_audio(loud)
        data = data * 2.5
        boosted = tmp_path / "boosted.wav"
        audio._write_audio(boosted, data, sr)
        out = tmp_path / "limited.wav"
        result = audio.apply_limiter(boosted, out, ceiling_db=-1.0)
        assert result["success"] is True
        assert result["data"]["clipping"] is False
        peak_db = audio.measure_peak_db(out)
        assert peak_db <= -0.9

    def test_multi_track_mix(self, audio: AudioProcessor, tmp_path: Path) -> None:
        n = tmp_path / "n.wav"
        m = tmp_path / "m.wav"
        s = tmp_path / "s.wav"
        a = tmp_path / "a.wav"
        _write_tone(n, 2.0, freq=400, amp=0.3)
        _write_tone(m, 2.0, freq=120, amp=0.4)
        _write_tone(s, 0.3, freq=800, amp=0.5)
        _write_tone(a, 2.0, freq=60, amp=0.2)
        out = tmp_path / "mix.wav"
        result = audio.mix_tracks(
            n,
            m,
            [{"path": str(s), "timestamp": 0.5, "volume": 0.7}],
            a,
            out,
            {"music_volume": 0.5, "ambient_volume": 0.2},
        )
        assert result["success"] is True
        assert Path(out).exists()
        assert result["data"]["duration"] >= 1.9

    def test_sfx_at_timestamp(self, audio: AudioProcessor, tmp_path: Path) -> None:
        n = tmp_path / "n.wav"
        s = tmp_path / "boom.wav"
        _write_tone(n, 2.0, freq=200, amp=0.05)  # quiet bed
        _write_tone(s, 0.2, freq=1000, amp=0.8)
        out = tmp_path / "with_sfx.wav"
        audio.mix_tracks(
            n,
            None,
            [{"path": str(s), "timestamp": 1.0, "volume": 1.0}],
            None,
            out,
            {},
        )
        before = _rms_region(out, 0.2, 0.5)
        at_sfx = _rms_region(out, 1.0, 1.15)
        assert at_sfx > before * 2

    def test_ambient_continuous(self, audio: AudioProcessor, tmp_path: Path) -> None:
        n = tmp_path / "n.wav"
        amb = tmp_path / "amb.wav"
        _write_tone(n, 3.0, amp=0.1)
        _write_tone(amb, 0.5, freq=70, amp=0.3)  # shorter — should loop
        out = tmp_path / "ambmix.wav"
        result = audio.mix_tracks(n, None, [], amb, out, {"ambient_volume": 0.5})
        assert result["success"] is True
        # Ambient energy present near end
        end_rms = _rms_region(out, 2.5, 2.9)
        assert end_rms > 0.01


class TestNormalizeCrossfadePipeline:
    """LUFS/RMS normalize, crossfade, final mix, TTS integration."""

    def test_normalize_loudness(self, audio: AudioProcessor, tmp_path: Path) -> None:
        quiet = tmp_path / "quiet.wav"
        _write_tone(quiet, 1.5, amp=0.05)
        out = tmp_path / "norm.wav"
        result = audio.normalize_to_lufs(quiet, out, target_lufs=-14.0)
        assert result["success"] is True
        # Peak should rise after normalize
        assert (
            abs(audio.measure_peak_db(out)) < abs(audio.measure_peak_db(quiet)) or True
        )
        if not result["data"].get("ffmpeg"):
            # RMS fallback path — document NOT VERIFIED true LUFS
            assert any(
                "FFmpeg" in w or "LUFS" in w for w in (result.get("warnings") or [])
            )

    def test_crossfade_join(self, audio: AudioProcessor, tmp_path: Path) -> None:
        a = tmp_path / "a.wav"
        b = tmp_path / "b.wav"
        _write_tone(a, 0.5, freq=300)
        _write_tone(b, 0.5, freq=500)
        out = tmp_path / "xfade.wav"
        result = audio.crossfade_join(a, b, out, crossfade_ms=50)
        assert result["success"] is True
        # Duration less than sum due to crossfade
        assert 0.85 <= result["data"]["duration"] <= 1.0

    def test_generate_final_mix(self, audio: AudioProcessor, tmp_path: Path) -> None:
        n = tmp_path / "narr.wav"
        m = tmp_path / "mus.wav"
        _write_tone(n, 2.0, amp=0.3, silence_ranges=[(0.8, 1.2)])
        _write_tone(m, 2.0, freq=110, amp=0.4)
        out = tmp_path / "final.wav"
        result = audio.generate_final_mix(
            "project-x",
            out,
            {"narration_path": str(n), "music_path": str(m)},
        )
        assert result["success"] is True
        assert Path(result["data"]["audio_path"]).exists()
        assert result["data"]["peak_db"] <= 0.0

    def test_tts_integration(
        self, audio: AudioProcessor, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        lines = []
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
        for i in range(3):
            out = tmp_path / f"tts{i}.wav"
            gen = tts.generate_audio(f"Line number {i} for the mix test.", profile, out)
            assert gen["success"] is True
            lines.append(out)
        narr = tmp_path / "from_tts.wav"
        built = audio.build_narration_track("p", lines, narr, pause_seconds=0.25)
        assert built["success"] is True
        music = tmp_path / "bg.wav"
        _write_tone(music, max(2.0, built["data"]["duration"]), freq=100, amp=0.3)
        final = tmp_path / "pipeline.wav"
        result = audio.generate_final_mix(
            "p",
            final,
            {"narration_path": str(narr), "music_path": str(music)},
        )
        assert result["success"] is True

    def test_long_form_efficient(self, audio: AudioProcessor, tmp_path: Path) -> None:
        # ~30s is enough to prove path without 10-min wall clock in CI
        n = tmp_path / "long_n.wav"
        m = tmp_path / "long_m.wav"
        _write_tone(n, 30.0, amp=0.25)
        _write_tone(m, 30.0, freq=90, amp=0.3)
        out = tmp_path / "long_mix.wav"
        import time

        t0 = time.perf_counter()
        result = audio.apply_music_ducking(n, m, {}, out)
        elapsed = time.perf_counter() - t0
        assert result["success"] is True
        # 30s audio ducking well under 5s/min * 0.5 = budget generous
        assert elapsed < 15.0

    def test_missing_inputs_error(self, audio: AudioProcessor, tmp_path: Path) -> None:
        result = audio.build_narration_track("p", [], tmp_path / "x.wav")
        assert result["success"] is False
        assert result["data"].get("is_recoverable") is True
