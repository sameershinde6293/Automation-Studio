"""Phase 3 (Click & Transition Refinement) tests.

This phase goes beyond the earlier basic click-repair work and adds:
  * Zero-crossing snapping for every splice point (narration joins,
    pause insertion, breath overlay) — not just "near zero", a true
    sign-change crossing (or closest-to-zero fallback).
  * Equal-power crossfades (constant perceived loudness through the
    transition) replacing pydub's default linear crossfade for
    narration-line joins and the public crossfade_join API.
  * Per-transition validation: every join/splice is checked for
    validity (non-empty, finite samples) immediately after it's made,
    falling back to the previous good state on failure instead of
    propagating a corrupted transition.
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


def _click_count(mono: np.ndarray) -> int:
    diffs = np.abs(np.diff(mono))
    med = float(np.median(diffs)) + 1e-9
    return int(np.count_nonzero((diffs > 0.25) & (diffs > med * 12.0)))


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


class TestZeroCrossingSnap:
    """_nearest_zero_crossing_ms finds a true crossing, not just "close"."""

    def test_snaps_to_genuine_sign_change(self, audio: AudioProcessor) -> None:
        from pydub import AudioSegment

        sr = 48000
        n = int(0.1 * sr)
        # A sine wave has genuine zero crossings; pick a position
        # deliberately away from one and confirm the snap lands on an
        # actual sign change nearby.
        t = np.arange(n) / sr
        sine = (np.sin(2 * np.pi * 100 * t) * 0.5 * 32767).astype("<i2")
        seg = AudioSegment(
            sine.tobytes(), sample_width=2, frame_rate=sr, channels=1
        )
        target_ms = 37  # arbitrary, almost certainly not exactly at zero
        snapped_ms = audio._nearest_zero_crossing_ms(seg, target_ms, search_ms=5)
        # Sample at the snapped position should be very close to zero
        # relative to full scale.
        idx = int(snapped_ms / 1000.0 * sr)
        idx = max(0, min(len(sine) - 1, idx))
        assert abs(int(sine[idx])) < 3300  # within ~10% of full scale

    def test_returns_original_on_empty_or_bad_input(
        self, audio: AudioProcessor
    ) -> None:
        from pydub import AudioSegment

        empty = AudioSegment.silent(duration=0)
        result = audio._nearest_zero_crossing_ms(empty, 0)
        assert result == 0

    def test_tts_engine_manager_has_matching_helper(
        self, tts: TTSEngineManager
    ) -> None:
        from pydub import AudioSegment

        sr = 48000
        n = int(0.1 * sr)
        t = np.arange(n) / sr
        sine = (np.sin(2 * np.pi * 100 * t) * 0.5 * 32767).astype("<i2")
        seg = AudioSegment(sine.tobytes(), sample_width=2, frame_rate=sr, channels=1)
        snapped_ms = tts._nearest_zero_crossing_ms(seg, 37, search_ms=5)
        idx = max(0, min(len(sine) - 1, int(snapped_ms / 1000.0 * sr)))
        assert abs(int(sine[idx])) < 3300


class TestEqualPowerCrossfade:
    """Equal-power crossfade keeps constant perceived loudness."""

    def test_output_length_matches_expected(self, audio: AudioProcessor) -> None:
        from pydub.generators import Sine

        a = Sine(300).to_audio_segment(duration=500).set_channels(2).set_frame_rate(48000)
        b = Sine(500).to_audio_segment(duration=500).set_channels(2).set_frame_rate(48000)
        joined = audio._equal_power_crossfade(a, b, 50)
        assert len(joined) == len(a) + len(b) - 50

    def test_no_loudness_dip_worse_than_linear_crossfade(
        self, audio: AudioProcessor
    ) -> None:
        from pydub.generators import Sine

        a = Sine(300).to_audio_segment(duration=500).set_channels(2).set_frame_rate(48000)
        b = Sine(500).to_audio_segment(duration=500).set_channels(2).set_frame_rate(48000)

        equal_power = audio._equal_power_crossfade(a, b, 50)
        linear = a.append(b, crossfade=50)

        def _mid_dip_ratio(seg) -> float:
            arr = np.frombuffer(seg.raw_data, dtype="<i2").astype(np.float64)
            arr = arr.reshape(-1, 2).mean(axis=1)
            sr = 48000
            mid_lo = int((len(a) - 25) / 1000 * sr)
            mid_hi = int((len(a) + 25) / 1000 * sr)
            mid_rms = np.sqrt(np.mean(arr[mid_lo:mid_hi] ** 2))
            edge_rms = np.sqrt(np.mean(arr[:1000] ** 2))
            return mid_rms / edge_rms

        eq_ratio = _mid_dip_ratio(equal_power)
        lin_ratio = _mid_dip_ratio(linear)
        # Equal-power crossfade must dip less (higher ratio = less dip)
        # than the linear crossfade for the same signals/duration.
        assert eq_ratio >= lin_ratio

    def test_falls_back_gracefully_on_mismatched_input(
        self, audio: AudioProcessor
    ) -> None:
        from pydub import AudioSegment

        a = AudioSegment.silent(duration=200, frame_rate=48000).set_channels(2)
        b = AudioSegment.silent(duration=200, frame_rate=48000).set_channels(2)
        joined = audio._equal_power_crossfade(a, b, 500)  # crossfade > lengths
        assert len(joined) > 0


class TestSegmentValidation:
    """_pydub_segment_is_valid catches broken transitions."""

    def test_valid_segment_passes(self, audio: AudioProcessor) -> None:
        from pydub.generators import Sine

        seg = Sine(300).to_audio_segment(duration=100)
        assert audio._pydub_segment_is_valid(seg) is True

    def test_empty_segment_is_invalid(self, audio: AudioProcessor) -> None:
        from pydub import AudioSegment

        seg = AudioSegment.silent(duration=0)
        assert audio._pydub_segment_is_valid(seg) is False

    def test_tts_engine_manager_matching_validator(
        self, tts: TTSEngineManager
    ) -> None:
        from pydub.generators import Sine
        from pydub import AudioSegment

        good = Sine(300).to_audio_segment(duration=100)
        assert tts._pydub_segment_is_valid(good) is True
        empty = AudioSegment.silent(duration=0)
        assert tts._pydub_segment_is_valid(empty) is False


class TestCrossfadeJoinRefined:
    """Public crossfade_join API uses zero-crossing snap + equal power."""

    def test_crossfade_join_click_free(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        n = int(0.5 * sr)
        a_signal = np.sin(np.linspace(0, 300 * np.pi, n)) * 0.4
        b_signal = np.sin(np.linspace(0, 500 * np.pi, n)) * 0.4
        a_path = tmp_path / "a.wav"
        b_path = tmp_path / "b.wav"
        _write_wav(a_path, a_signal, sr)
        _write_wav(b_path, b_signal, sr)
        out = tmp_path / "joined.wav"
        result = audio.crossfade_join(a_path, b_path, out, crossfade_ms=50)
        assert result["success"] is True
        data, _ = _read_wav(out)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert _click_count(mono) == 0

    def test_crossfade_join_missing_file_errors(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        result = audio.crossfade_join(
            tmp_path / "missing_a.wav", tmp_path / "missing_b.wav", tmp_path / "out.wav"
        )
        assert result["success"] is False


class TestNarrationJoinRefinement:
    """build_narration_track's improved join quality."""

    def test_click_free_with_many_lines(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        lines = []
        for i in range(8):
            p = tmp_path / f"line{i}.wav"
            n = int(0.4 * sr)
            signal = np.sin(np.linspace(0, (150 + i * 40) * np.pi, n)) * 0.35
            _write_wav(p, signal, sr)
            lines.append(p)
        out = tmp_path / "narration.wav"
        result = audio.build_narration_track("p", lines, out, pause_seconds=0.2)
        assert result["success"] is True
        data, _ = audio._read_audio(out)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert _click_count(mono) == 0

    def test_timestamps_still_monotonic_after_refinement(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        lines = []
        for i in range(4):
            p = tmp_path / f"line{i}.wav"
            n = int(0.3 * sr)
            signal = np.sin(np.linspace(0, (200 + i * 20) * np.pi, n)) * 0.3
            _write_wav(p, signal, sr)
            lines.append(p)
        out = tmp_path / "narration.wav"
        result = audio.build_narration_track("p", lines, out, pause_seconds=0.25)
        assert result["success"] is True
        timestamps = result["data"]["segment_timestamps"]
        for i in range(1, len(timestamps)):
            assert timestamps[i]["start"] >= timestamps[i - 1]["start"]
            assert timestamps[i]["end"] >= timestamps[i]["start"]


class TestPauseAndBreathZeroCrossingSnap:
    """insert_pauses_into_audio / add_breathing_sounds snap splices."""

    def test_insert_pauses_still_click_free_with_snap(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        sr = 22050
        n = sr * 2
        signal = np.sin(np.linspace(0, 300 * np.pi, n)) * 0.4
        path = tmp_path / "speech.wav"
        _write_wav(path, signal, sr)
        timestamps = [
            {"word": "one", "start": 0.0, "end": 0.5},
            {"word": "two", "start": 0.5, "end": 1.0},
            {"word": "three", "start": 1.0, "end": 1.5},
        ]
        markers = [{"word_index": 2, "type": "MEDIUM", "duration": 0.3}]
        result = tts.insert_pauses_into_audio(str(path), markers, timestamps)
        assert result["success"] is True
        data, sr2 = _read_wav(path)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert _click_count(mono) == 0

    def test_add_breathing_sounds_click_free(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        sr = 22050
        n = sr * 3
        signal = np.sin(np.linspace(0, 300 * np.pi, n)) * 0.4
        # Carve a real silence gap in the middle so detect_silence finds it.
        signal[int(1.0 * sr):int(1.5 * sr)] = 0.0
        path = tmp_path / "speech_with_gap.wav"
        _write_wav(path, signal, sr)
        profile = {"breathing_enabled": True, "voice_model": "default"}
        result = tts.add_breathing_sounds(str(path), profile)
        assert result["success"] is True
        data, sr2 = _read_wav(path)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert _click_count(mono) == 0


class TestEndToEndTransitionQuality:
    """Full generate_audio -> build_narration_track -> final mix chain."""

    def test_full_pipeline_click_free_with_pauses(
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
            "breathing_enabled": True,
        }
        lines = []
        sentences = [
            "This is the first sentence. [PAUSE:SHORT] Onward.",
            "[PAUSE:MEDIUM] A second sentence follows here.",
            "And a third, [PAUSE:MICRO] final sentence to close it out.",
        ]
        for i, text in enumerate(sentences):
            out = tmp_path / f"line{i}.wav"
            result = tts.generate_audio(text, profile, out)
            assert result["success"], result
            lines.append(result["data"]["audio_path"])

        narration_out = tmp_path / "narration.wav"
        built = audio.build_narration_track("p", lines, narration_out, pause_seconds=0.25)
        assert built["success"], built

        final_out = tmp_path / "final.wav"
        mix_result = audio.generate_final_mix(
            "p", final_out, {"narration_path": str(narration_out)}
        )
        assert mix_result["success"], mix_result

        data, _ = audio._read_audio(final_out)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6
        assert _click_count(mono) == 0
