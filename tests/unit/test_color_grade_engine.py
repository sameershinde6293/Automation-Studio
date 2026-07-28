"""Unit tests for modules.color_grade_engine.ColorGradeEngine.

Filter construction is pure string generation — no real FFmpeg required.
FFmpeg execution paths are covered with a fake ffmpeg executable that
records its argv and creates the output file (cross-platform test double
from tests/conftest.py: bash on POSIX, Python + subprocess shim on
Windows).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core.service_container import ServiceContainer
from modules.color_grade_engine import ColorGradeEngine

PRESET_COUNT = 14  # File 11 color_grade_presets.json


def _make_container(
    project_root: Path, tmp_path: Path, ffmpeg_path: str = "ffmpeg"
) -> ServiceContainer:
    """Isolated container with the real project config folder."""
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": ffmpeg_path,
        },
        project_root=project_root,
    )


@pytest.fixture
def engine(project_root: Path, tmp_path: Path) -> ColorGradeEngine:
    """Color grade engine with the default (unresolverable) ffmpeg hint."""
    return ColorGradeEngine(_make_container(project_root, tmp_path))


class TestCatalog:
    """Preset catalog completeness."""

    def test_all_presets_load(self, engine: ColorGradeEngine) -> None:
        result = engine.get_available_presets()
        assert result["success"] is True
        ids = {p["id"] for p in result["data"]["presets"]}
        expected = {
            # File 07 core ten
            "dark_moody",
            "true_crime",
            "ancient_mystery",
            "conspiracy",
            "war_documentary",
            "horror_documentary",
            "space_documentary",
            "black_white_classic",
            "vintage_1920s",
            "clean_modern",
            # File 11 extended
            "dark_history",
            "investigation",
            "vintage_1950s",
            "cinematic_teal_orange",
        }
        assert expected <= ids
        assert result["data"]["count"] == PRESET_COUNT
        assert result["data"]["default_preset"] == "dark_moody"


class TestFilterBuilding:
    """build_grade_filter chain structure and ordering."""

    def test_dark_moody_components(self, engine: ColorGradeEngine) -> None:
        data = engine.build_grade_filter("dark_moody", {})["data"]
        g = data["filtergraph"]
        assert g.startswith("eq=brightness=-0.05:contrast=1.2:saturation=0.7:gamma=0.9")
        assert "colorbalance=rs=-0.10" in g
        assert "vignette=" in g and "eval=frame" in g
        assert "noise=c0s=4:c0f=t+u" in g
        assert "unsharp=5:5:0.3" in g
        assert (
            data["grain_applied"]
            and data["vignette_applied"]
            and data["sharpen_applied"]
        )

    def test_chain_order_matches_spec(self, engine: ColorGradeEngine) -> None:
        g = engine.build_grade_filter("dark_moody", {})["data"]["filtergraph"]
        assert (
            g.index("eq=")
            < g.index("colorbalance")
            < g.index("vignette")
            < g.index("noise")
        )

    def test_clean_modern_is_passthrough(self, engine: ColorGradeEngine) -> None:
        data = engine.build_grade_filter("clean_modern", {})["data"]
        assert data["components"] == ["eq=brightness=0:contrast=1:saturation=1:gamma=1"]
        assert data["grain_applied"] is False and data["vignette_applied"] is False

    def test_lut_missing_warns_and_skips(self, engine: ColorGradeEngine) -> None:
        # assets/luts has no .cube files in this environment.
        result = engine.build_grade_filter("dark_moody", {})
        assert "lut3d" not in result["data"]["filtergraph"]
        assert result["data"]["lut_applied"] is False
        assert any("LUT not found" in w for w in result["warnings"])

    def test_lut_applied_when_file_exists(
        self, engine: ColorGradeEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        luts = tmp_path / "luts"
        luts.mkdir()
        (luts / "dark_moody.cube").write_text(
            "TITLE fake\nLUT_3D_SIZE 2\n", encoding="utf-8"
        )
        monkeypatch.setattr(engine, "_luts_folder", lambda: luts)
        data = engine.build_grade_filter("dark_moody", {})["data"]
        assert (
            "lut3d='" in data["filtergraph"]
            and "dark_moody.cube" in data["filtergraph"]
        )
        assert data["lut_applied"] is True
        assert data["lut_opacity_pending"] is True  # 0.80 < 1.0 -> blend at export

    def test_overlays_deferred_with_warning(self, engine: ColorGradeEngine) -> None:
        result = engine.build_grade_filter("dark_history", {})
        assert any("DEBT-B10a" in w for w in result["warnings"])
        assert "overlay" not in result["data"]["filtergraph"]


class TestValidation:
    """Clamping, defaults, unknown keys."""

    def test_unknown_preset_defaults_with_warning(
        self, engine: ColorGradeEngine
    ) -> None:
        result = engine.build_grade_filter("nope", {})
        assert result["data"]["preset_name"] == "dark_moody"
        assert any("Unknown grade preset" in w for w in result["warnings"])

    def test_eq_overrides_clamped(self, engine: ColorGradeEngine) -> None:
        data = engine.build_grade_filter("clean_modern", {"brightness": 5.0})
        assert "brightness=1" in data["data"]["filtergraph"].split(":")[0]
        assert any("clamped" in w for w in data["warnings"])
        ok = engine.build_grade_filter("clean_modern", {"saturation": 0.5})
        assert "saturation=0.5" in ok["data"]["filtergraph"]
        assert ok["warnings"] == []

    def test_unit_overrides_toggle_components(self, engine: ColorGradeEngine) -> None:
        data = engine.build_grade_filter(
            "clean_modern", {"vignette_strength": 0.8, "film_grain_amount": 9.0}
        )
        g = data["data"]["filtergraph"]
        assert "vignette=" in g and "noise=c0s=100" in g
        assert any(
            "film_grain_amount" in w for w in data["warnings"]
        )  # 9 -> clamped to 1
        off = engine.build_grade_filter(
            "dark_moody", {"vignette_strength": 0, "sharpen_amount": 0}
        )
        assert "vignette=" not in off["data"]["filtergraph"]
        assert "unsharp" not in off["data"]["filtergraph"]

    def test_unknown_override_ignored(self, engine: ColorGradeEngine) -> None:
        result = engine.build_grade_filter("dark_moody", {"bogus_key": 1})
        assert any("Ignoring unknown" in w for w in result["warnings"])


class TestSpecHelpers:
    """add_film_grain / add_vignette / genre resolution / disable flag."""

    def test_add_film_grain(self, engine: ColorGradeEngine) -> None:
        data = engine.add_film_grain("eq=brightness=0", 0.12)["data"]
        assert data["filtergraph"] == "eq=brightness=0,noise=c0s=12:c0f=t+u"
        assert data["grain_added"] is True
        zero = engine.add_film_grain("eq=brightness=0", 0.0)["data"]
        assert zero["filtergraph"] == "eq=brightness=0" and zero["grain_added"] is False

    def test_add_vignette_strength_to_angle(self, engine: ColorGradeEngine) -> None:
        data = engine.add_vignette("eq=brightness=0", 0.7)["data"]
        assert data["filtergraph"].endswith("vignette=0.5498:eval=frame")
        assert data["angle"] == pytest.approx(0.7 * 3.14159265 / 4, abs=1e-4)
        full = engine.add_vignette("", 1.0)["data"]
        assert full["filtergraph"].startswith("vignette=0.7854")
        neg = engine.add_vignette("eq=brightness=0", -1)["data"]
        assert neg["vignette_added"] is False

    def test_genre_resolution_documentary_genres(
        self, engine: ColorGradeEngine
    ) -> None:
        data = engine.get_preset_for_genre("dark_history")["data"]
        assert data["preset_name"] == "dark_moody"
        assert data["source"] == "documentary_genres"
        assert (
            engine.get_preset_for_genre("conspiracy")["data"]["preset_name"]
            == "conspiracy"
        )

    def test_genre_resolution_fallback(self, engine: ColorGradeEngine) -> None:
        data = engine.get_preset_for_genre("horror_documentary")["data"]
        assert data["preset_name"] in {"horror_documentary", "dark_moody"}
        unknown = engine.get_preset_for_genre("xyzzy")["data"]
        assert unknown["preset_name"] == "dark_moody" and unknown["source"] == "default"

    def test_module_can_be_disabled(self, engine: ColorGradeEngine) -> None:
        assert engine.is_optional_module() is True
        engine.set_enabled(False)
        result = engine.build_grade_filter("dark_moody", {})
        assert result["success"] is False and "disabled" in (result["error"] or "")
        engine.set_enabled(True)
        assert engine.build_grade_filter("dark_moody", {})["success"] is True


class TestFFmpegExecution:
    """apply_grade paths: graceful failures + fake-ffmpeg success."""

    def test_apply_image_missing_input(
        self, engine: ColorGradeEngine, tmp_path: Path
    ) -> None:
        result = engine.apply_grade_to_image(
            tmp_path / "nope.png", "dark_moody", tmp_path / "o.png"
        )
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_apply_image_without_ffmpeg(
        self, engine: ColorGradeEngine, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # D4b: deterministic absence. Hosts with a REAL ffmpeg bundled
        # under engines/ (e.g. the render-gate machine) made this fail
        # because detection depends on host state, not test intent.
        monkeypatch.setattr(engine.hardware, "find_ffmpeg", lambda: None)
        src = tmp_path / "in.png"
        Image.new("RGB", (64, 64), (10, 10, 40)).save(src)
        result = engine.apply_grade_to_image(src, "dark_moody", tmp_path / "out.png")
        assert result["success"] is False
        assert "FFmpeg not available" in (result["error"] or "")

    def test_apply_image_with_fake_ffmpeg(
        self, project_root: Path, tmp_path: Path, fake_ffmpeg_factory
    ) -> None:
        fake = fake_ffmpeg_factory(tmp_path, tmp_path / "ffmpeg_argv.txt")
        src = tmp_path / "in.png"
        out = tmp_path / "graded.png"
        Image.new("RGB", (64, 64), (10, 10, 40)).save(src)
        engine = ColorGradeEngine(_make_container(project_root, tmp_path, str(fake)))
        result = engine.apply_grade_to_image(src, "dark_moody", out)
        assert result["success"] is True
        assert out.exists()
        argv = (tmp_path / "ffmpeg_argv.txt").read_text(encoding="utf-8")
        assert "-frames:v 1" in argv
        assert "-vf eq=brightness=-0.05" in argv
        assert result["data"]["preset_name"] == "dark_moody"

    def test_apply_video_copies_audio(
        self, project_root: Path, tmp_path: Path, fake_ffmpeg_factory
    ) -> None:
        fake = fake_ffmpeg_factory(tmp_path, tmp_path / "ffmpeg_argv.txt")
        src = tmp_path / "in.mp4"
        out = tmp_path / "graded.mp4"
        src.write_bytes(b"fake video bytes")
        engine = ColorGradeEngine(_make_container(project_root, tmp_path, str(fake)))
        result = engine.apply_grade_to_video_segment(src, "true_crime", out)
        assert result["success"] is True
        assert result["data"]["audio_copied"] is True
        argv = (tmp_path / "ffmpeg_argv.txt").read_text(encoding="utf-8")
        assert "-c:a copy" in argv
        assert "contrast=1.3" in argv  # true_crime eq applied
