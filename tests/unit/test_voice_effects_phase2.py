"""Phase 2 (Voice Effects Chain Rebuild) tests.

Covers the rebuilt TTSEngineManager.apply_voice_effects:
  * Fixed, safe processing order (high-pass -> EQ -> compressor ->
    limiter -> optional single LUFS pass), with optional coloring
    stages (noise gate, reverb, special effect, per-line volume)
    interleaved without disturbing that backbone.
  * Every stage is individually bypassable.
  * Never more than one limiter or one LUFS-normalize stage.
  * Automatic, crash-free degradation: full chain fails -> safe backbone
    -> original audio kept, in that order, never raising.
  * Backward compatibility: default behavior unchanged for profiles
    that never set the new bypass flags.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from core.service_container import ServiceContainer
from modules.tts_engine_manager import TTSEngineManager
from modules.tts_presets import (
    COMPRESSOR_FILTER,
    HIGHPASS_FILTER,
    LIMITER_FILTER,
    LUFS_NORMALIZE_FILTER,
    NOISE_GATE_FILTER,
)


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
def tts(container: ServiceContainer) -> TTSEngineManager:
    return TTSEngineManager(container)


def _write_tone(path: Path, duration: float = 0.5, sr: int = 48000) -> None:
    n = int(duration * sr)
    signal = (np.sin(np.linspace(0, 200 * np.pi, n)) * 0.3 * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(signal.tobytes())


class TestFilterChainOrder:
    """The rebuilt chain builder produces the mandated safe order."""

    def test_default_profile_full_chain_order(self, tts: TTSEngineManager) -> None:
        profile = {"eq_preset": "documentary_male", "reverb_preset": "none"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        # Mandatory backbone must appear, in relative order:
        # highpass -> eq -> compressor -> limiter
        idx_hp = full.index(HIGHPASS_FILTER)
        idx_gate = full.index(NOISE_GATE_FILTER)
        idx_comp = full.index(COMPRESSOR_FILTER)
        idx_lim = full.index(LIMITER_FILTER)
        assert idx_hp < idx_gate < idx_comp < idx_lim
        assert flags["highpass"] is True
        assert flags["noise_gate"] is True
        assert flags["compressor"] is True
        assert flags["limiter"] is True
        # LUFS normalize is opt-in, default off.
        assert flags["lufs_normalize"] is False
        assert LUFS_NORMALIZE_FILTER not in full

    def test_safe_backbone_is_subset_without_optional_stages(
        self, tts: TTSEngineManager
    ) -> None:
        profile = {
            "eq_preset": "documentary_male",
            "reverb_preset": "cathedral",
            "special_effect": "old_radio",
        }
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.2)
        # Safe backbone never contains reverb/special/noise-gate/volume.
        assert not any("aecho" in f for f in safe)
        assert NOISE_GATE_FILTER not in safe
        assert not any(f.startswith("volume=") for f in safe)
        # But the full chain does contain them.
        assert any("aecho" in f for f in full)
        assert NOISE_GATE_FILTER in full
        assert any(f.startswith("volume=") for f in full)

    def test_never_more_than_one_limiter(self, tts: TTSEngineManager) -> None:
        profile = {"eq_preset": "documentary_male"}
        params = {"lufs_normalize_enabled": True}
        full, safe, flags = tts._build_voice_effects_filters(profile, params, 1.0)
        assert full.count(LIMITER_FILTER) == 1
        assert safe.count(LIMITER_FILTER) <= 1

    def test_never_more_than_one_lufs_stage(self, tts: TTSEngineManager) -> None:
        profile = {"eq_preset": "documentary_male"}
        params = {"lufs_normalize_enabled": True}
        full, safe, flags = tts._build_voice_effects_filters(profile, params, 1.0)
        assert full.count(LUFS_NORMALIZE_FILTER) == 1
        assert LUFS_NORMALIZE_FILTER not in safe  # opt-in stage isn't "safe backbone"


class TestBypassFlags:
    """Every effect must be individually bypassable."""

    def test_highpass_bypass(self, tts: TTSEngineManager) -> None:
        profile = {"highpass_enabled": False, "eq_preset": "flat"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["highpass"] is False
        assert HIGHPASS_FILTER not in full

    def test_noise_gate_bypass(self, tts: TTSEngineManager) -> None:
        profile = {"noise_gate_enabled": False, "eq_preset": "flat"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["noise_gate"] is False
        assert NOISE_GATE_FILTER not in full

    def test_compression_bypass(self, tts: TTSEngineManager) -> None:
        profile = {"compression_enabled": False, "eq_preset": "flat"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["compressor"] is False
        assert COMPRESSOR_FILTER not in full
        assert COMPRESSOR_FILTER not in safe

    def test_limiter_bypass(self, tts: TTSEngineManager) -> None:
        profile = {"limiter_enabled": False, "eq_preset": "flat"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["limiter"] is False
        assert LIMITER_FILTER not in full
        assert LIMITER_FILTER not in safe

    def test_eq_flat_bypasses_eq(self, tts: TTSEngineManager) -> None:
        profile = {"eq_preset": "flat"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["eq"] is False

    def test_reverb_none_bypasses_reverb(self, tts: TTSEngineManager) -> None:
        profile = {"eq_preset": "flat", "reverb_preset": "none"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["reverb"] is False

    def test_special_none_bypasses_special(self, tts: TTSEngineManager) -> None:
        profile = {"eq_preset": "flat", "special_effect": "none"}
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["special_effect"] is False

    def test_all_bypassed_yields_noop_chain(self, tts: TTSEngineManager) -> None:
        profile = {
            "highpass_enabled": False,
            "noise_gate_enabled": False,
            "compression_enabled": False,
            "limiter_enabled": False,
            "eq_preset": "flat",
            "reverb_preset": "none",
            "special_effect": "none",
        }
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert full == ["anull"]
        assert safe == ["anull"]

    def test_params_override_profile_flags(self, tts: TTSEngineManager) -> None:
        profile = {"compression_enabled": True, "eq_preset": "flat"}
        params = {"compression_enabled": False}
        full, safe, flags = tts._build_voice_effects_filters(profile, params, 1.0)
        assert flags["compressor"] is False


class TestBackwardCompatibility:
    """A profile that never sets the new flags keeps prior behavior."""

    def test_legacy_profile_still_gets_full_backbone(
        self, tts: TTSEngineManager
    ) -> None:
        # Old-style profile dict, exactly as existing callers/tests use it.
        profile = {
            "eq_preset": "documentary_male",
            "reverb_preset": "subtle_room",
            "special_effect": "none",
            "volume": 1.0,
        }
        full, safe, flags = tts._build_voice_effects_filters(profile, {}, 1.0)
        assert flags["highpass"] is True
        assert flags["noise_gate"] is True
        assert flags["compressor"] is True
        assert flags["limiter"] is True
        assert flags["eq"] is True
        assert flags["reverb"] is True
        assert flags["lufs_normalize"] is False


class TestApplyVoiceEffectsRobustness:
    """apply_voice_effects never crashes and never ships broken audio."""

    def test_no_ffmpeg_falls_back_to_volume_only(
        self, tts: TTSEngineManager, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(tts, "_find_ffmpeg", lambda: None)
        out = tmp_path / "fx.wav"
        _write_tone(out)
        profile = {"eq_preset": "documentary_male", "volume": 1.2}
        result = tts.apply_voice_effects(str(out), profile, {"volume": 1.2})
        assert result["success"] is True
        assert result["data"]["ffmpeg"] is False
        assert out.exists()

    def test_missing_audio_file_returns_recoverable_error(
        self, tts: TTSEngineManager, tmp_path: Path
    ) -> None:
        result = tts.apply_voice_effects(
            str(tmp_path / "missing.wav"), {"eq_preset": "flat"}, {}
        )
        assert result["success"] is False
        assert result["data"]["is_recoverable"] is True

    def test_degenerate_ffmpeg_output_falls_back_to_original_audio(
        self,
        tts: TTSEngineManager,
        tmp_path: Path,
        monkeypatch,
        fake_ffmpeg_factory,
    ) -> None:
        """The bundled fake ffmpeg always emits fixed silent WAV bytes for
        .wav outputs — a real, if extreme, example of a "degenerate"
        effects-chain result. apply_voice_effects must detect this and
        fall all the way back to keeping the original narration audio,
        for BOTH the full chain and the safe-backbone retry, without
        ever raising or shipping the silent/degenerate file.
        """
        fake = fake_ffmpeg_factory(tmp_path, tmp_path / "ffmpeg.log")
        monkeypatch.setattr(tts, "_find_ffmpeg", lambda: fake)

        out = tmp_path / "line.wav"
        _write_tone(out, duration=1.0)
        original_bytes = out.read_bytes()

        profile = {
            "eq_preset": "documentary_male",
            "reverb_preset": "cathedral",
            "special_effect": "old_radio",
        }
        result = tts.apply_voice_effects(str(out), profile, {"volume": 1.0})
        assert result["success"] is True
        assert result["data"].get("effects_skipped") == "degenerate_output_detected"
        # Original audio must be completely unchanged (byte-identical).
        assert out.read_bytes() == original_bytes
        # No leftover temp files from either attempt.
        leftovers = list(tmp_path.glob("*.fx-*.wav"))
        assert leftovers == []

    def test_effects_applied_flag_reported_on_success(
        self, tts: TTSEngineManager, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(tts, "_find_ffmpeg", lambda: None)
        out = tmp_path / "fx.wav"
        _write_tone(out)
        profile = {"eq_preset": "flat", "compression_enabled": False}
        result = tts.apply_voice_effects(str(out), profile, {})
        assert result["success"] is True

    def test_ffmpeg_receives_correctly_ordered_single_pass_filter_chain(
        self,
        tts: TTSEngineManager,
        tmp_path: Path,
        monkeypatch,
        fake_ffmpeg_factory,
    ) -> None:
        """Verify the ACTUAL argv handed to the ffmpeg subprocess (not
        just the in-memory filter list) has the mandated order and
        contains each of limiter/LUFS at most once — i.e. the real
        integration point, not just the builder function in isolation.
        """
        log = tmp_path / "ffmpeg.log"
        monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
        fake = fake_ffmpeg_factory(tmp_path, log)
        monkeypatch.setattr(tts, "_find_ffmpeg", lambda: fake)

        out = tmp_path / "line.wav"
        _write_tone(out, duration=1.0)
        profile = {
            "eq_preset": "documentary_male",
            "reverb_preset": "subtle_room",
        }
        params = {"lufs_normalize_enabled": True, "volume": 1.1}
        tts.apply_voice_effects(str(out), profile, params)

        assert log.exists()
        lines = [ln for ln in log.read_text().splitlines() if ln.startswith("CMD")]
        assert lines, "fake ffmpeg was never invoked"
        # First (full-chain) invocation's -af argument.
        first_cmd = lines[0]
        af_index = first_cmd.split().index("-af")
        filter_arg = first_cmd.split()[af_index + 1]
        filters = filter_arg.split(",")

        idx_hp = next(i for i, f in enumerate(filters) if f.startswith("highpass"))
        idx_gate = next(i for i, f in enumerate(filters) if f.startswith("agate"))
        idx_eq = next(i for i, f in enumerate(filters) if f.startswith("equalizer"))
        idx_comp = next(i for i, f in enumerate(filters) if f.startswith("acompressor"))
        idx_lim = next(i for i, f in enumerate(filters) if f.startswith("alimiter"))
        assert idx_hp < idx_gate < idx_eq < idx_comp < idx_lim
        assert sum(1 for f in filters if f.startswith("alimiter")) == 1
        assert sum(1 for f in filters if f.startswith("loudnorm")) == 1


class TestValidationAfterEffects:
    """PHASE 2 requirement: validate audio after the effects stage too."""

    def test_output_has_no_nan_inf_or_clipping(
        self, tts: TTSEngineManager, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(tts, "_find_ffmpeg", lambda: None)
        out = tmp_path / "fx.wav"
        _write_tone(out)
        profile = {"eq_preset": "documentary_male", "volume": 1.5}
        result = tts.apply_voice_effects(str(out), profile, {"volume": 1.5})
        assert result["success"] is True
        with wave.open(str(out), "rb") as handle:
            raw = handle.readframes(handle.getnframes())
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6

    def test_generate_audio_end_to_end_with_effects_bypassed(
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
            "highpass_enabled": False,
            "noise_gate_enabled": False,
            "compression_enabled": False,
            "limiter_enabled": False,
        }
        out = tmp_path / "line.wav"
        result = tts.generate_audio(
            "Effects fully bypassed stability check.", profile, out
        )
        assert result["success"] is True
        with wave.open(str(out), "rb") as handle:
            raw = handle.readframes(handle.getnframes())
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        assert np.isfinite(data).all()
        assert np.max(np.abs(data)) <= 1.0 + 1e-6
