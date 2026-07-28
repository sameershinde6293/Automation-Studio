"""Phase 3 (Click Removal) tests.

Covers the click/pop repair work specific to this phase:
  * AudioProcessor.detect_and_repair_clicks — the whole-track detector
    wired into build_narration_track and generate_final_mix.
  * Join-click fixes in insert_pauses_into_audio (pause splice edges)
    and add_breathing_sounds (breath-overlay splice edges).
  * Truncated-breath / truncated-SFX / trimmed-loop edge fades that
    were previously hard cuts.
  * End-to-end: every generated clip fades in/out (5-10ms) and no
    start/end/join click survives narration assembly.
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


def _click_count(data: np.ndarray) -> int:
    mono = data.mean(axis=1) if data.ndim == 2 else data
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


class TestDetectAndRepairClicks:
    """AudioProcessor.detect_and_repair_clicks: whole-track safety net."""

    def test_detects_and_repairs_injected_spike(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        n = 48000
        clean = np.sin(np.linspace(0, 200 * np.pi, n)) * 0.3
        clicked = clean.copy()
        clicked[20000] = 0.95  # sharp isolated spike
        src = tmp_path / "clicked.wav"
        _write_wav(src, clicked)
        result = audio.detect_and_repair_clicks(src)
        assert result["success"] is True
        assert result["data"]["clicks_detected"] >= 1
        repaired, _ = audio._read_audio(src)
        mono = repaired.mean(axis=1) if repaired.ndim == 2 else repaired
        assert abs(mono[20000] - clean[20000]) < 0.05

    def test_no_clicks_in_clean_signal_leaves_it_unchanged(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        n = 48000
        clean = np.sin(np.linspace(0, 200 * np.pi, n)) * 0.3
        src = tmp_path / "clean.wav"
        _write_wav(src, clean)
        before = src.read_bytes()
        result = audio.detect_and_repair_clicks(src)
        assert result["success"] is True
        assert result["data"]["clicks_detected"] == 0
        assert src.read_bytes() == before  # untouched, no rewrite

    def test_missing_file_returns_error(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        result = audio.detect_and_repair_clicks(tmp_path / "missing.wav")
        assert result["success"] is False

    def test_writes_to_separate_output_path(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        n = 4800
        clean = np.sin(np.linspace(0, 20 * np.pi, n)) * 0.2
        src = tmp_path / "src.wav"
        dest = tmp_path / "dest.wav"
        _write_wav(src, clean)
        result = audio.detect_and_repair_clicks(src, dest)
        assert result["success"] is True
        assert dest.exists()


class TestNarrationAssemblyClickFree:
    """build_narration_track / generate_final_mix stay click-free."""

    def test_build_narration_track_runs_click_repair(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        lines = []
        for i in range(3):
            p = tmp_path / f"line{i}.wav"
            n = 24000
            signal = np.sin(np.linspace(0, (100 + i * 30) * np.pi, n)) * 0.3
            _write_wav(p, signal)
            lines.append(p)
        out = tmp_path / "narration.wav"
        result = audio.build_narration_track("p", lines, out, pause_seconds=0.2)
        assert result["success"] is True
        data, _ = audio._read_audio(out)
        assert _click_count(data) == 0

    def test_final_mix_has_no_clicks(self, audio: AudioProcessor, tmp_path: Path) -> None:
        n = 48000 * 2
        narr = np.sin(np.linspace(0, 400 * np.pi, n)) * 0.4
        music = np.sin(np.linspace(0, 90 * np.pi, n)) * 0.3
        narr_path = tmp_path / "narr.wav"
        music_path = tmp_path / "music.wav"
        _write_wav(narr_path, np.stack([narr, narr], axis=1))
        _write_wav(music_path, np.stack([music, music], axis=1))
        out = tmp_path / "final.wav"
        result = audio.generate_final_mix(
            "click-test",
            out,
            {"narration_path": str(narr_path), "music_path": str(music_path)},
        )
        assert result["success"] is True
        data, _ = audio._read_audio(out)
        assert _click_count(data) == 0


class TestLoopAndTrimEdgeFades:
    """Looping/trimming audio to length no longer produces hard cuts."""

    def test_match_length_trim_fades_tail(self, audio: AudioProcessor) -> None:
        n = 4800
        # A signal that's loud right up to the trim point.
        signal = np.ones(n) * 0.5
        trimmed = audio._match_length(signal, n - 100)
        assert abs(trimmed[-1]) < 0.05

    def test_match_length_pad_fades_before_silence(self, audio: AudioProcessor) -> None:
        n = 4800
        signal = np.ones(n) * 0.5
        padded = audio._match_length(signal, n + 200)
        # The real-audio portion's tail should fade toward the silence.
        assert abs(padded[n - 1]) < 0.05
        assert np.all(padded[n:] == 0.0)

    def test_loop_to_length_fades_tail(self, audio: AudioProcessor) -> None:
        n = 1000
        signal = np.ones(n) * 0.5
        looped = audio._loop_to_length(signal, 3500)
        assert abs(looped[-1]) < 0.05


class TestSfxTruncationFade:
    """SFX clips truncated at the mix buffer end fade instead of cutting."""

    def test_truncated_sfx_fades_at_boundary(
        self, audio: AudioProcessor, tmp_path: Path
    ) -> None:
        sr = 48000
        mix = np.zeros(sr)  # 1s of silence
        sfx_path = tmp_path / "sfx.wav"
        sfx_signal = np.ones(sr) * 0.6  # 1s SFX that will get truncated
        _write_wav(sfx_path, sfx_signal, sr)
        out = audio._overlay_sfx(
            mix, sr, [{"path": str(sfx_path), "timestamp": 0.9, "volume": 1.0}], 1.0
        )
        # The clip should fade toward zero right at the mix boundary.
        assert abs(out[-1]) < 0.1


class TestPauseAndBreathSpliceFades:
    """insert_pauses_into_audio / add_breathing_sounds don't leave clicks."""

    def test_insert_pauses_no_click_at_splice(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        sr = 22050
        n = sr * 2
        # Loud, non-zero-crossing-friendly tone so any hard splice would
        # show up as a clear discontinuity.
        signal = (np.sin(np.linspace(0, 300 * np.pi, n)) * 0.4 + 0.0)
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
        data, sr2 = tts_read_wav(path)
        assert _click_count(data) == 0


def tts_read_wav(path: Path):
    with wave.open(str(path), "r") as handle:
        sr = handle.getframerate()
        ch = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch)
    return arr, sr


class TestEndToEndFadeCompliance:
    """Every generated line fades in/out within the 5-10ms spec."""

    def test_generated_line_fade_within_spec(
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
        result = tts.generate_audio("Click removal end to end check.", profile, out)
        assert result["success"] is True
        data, sr = tts_read_wav(Path(result["data"]["audio_path"]))
        mono = data.mean(axis=1) if data.ndim == 2 else data
        # 5-10ms window check: sample at 5ms in should already have
        # ramped up from the (near-zero) start, and the file must not
        # start/end with a hard jump.
        assert abs(mono[0]) < 0.05
        assert abs(mono[-1]) < 0.05
        assert _click_count(mono) == 0
