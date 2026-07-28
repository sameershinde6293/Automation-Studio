"""Unit tests for modules.intro_outro_engine.IntroOutroEngine.

Templates/config resolution, Pillow card rendering (pixel-level ornament
and gradient assertions), zoompan construction, custom-video handling,
and graceful degradation. Execution uses the shared cross-platform fake
ffmpeg/ffprobe doubles from tests/conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from core.service_container import ServiceContainer
from modules.intro_outro_engine import IntroOutroEngine

PROJECT = "proj-io-1"
NOW = "2026-07-16 00:00:00"
ACCENTS = {
    "dark_history": (0x8B, 0x00, 0x00),
    "true_crime": (0xFF, 0x00, 0x00),
    "historical": (0xC9, 0xA2, 0x27),
    "conspiracy": (0x00, 0xFF, 0x41),
    "mystery": (0x4A, 0x90, 0xD9),
}


def _container(
    project_root: Path, tmp_path: Path, ffmpeg_path: str = "ffmpeg"
) -> ServiceContainer:
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
def ioe(project_root: Path, tmp_path: Path) -> IntroOutroEngine:
    """Engine without a resolvable ffmpeg."""
    return IntroOutroEngine(_container(project_root, tmp_path))


@pytest.fixture
def ioe_ff(
    project_root: Path,
    tmp_path: Path,
    fake_ffmpeg_factory,
    fake_ffprobe_factory,
) -> IntroOutroEngine:
    """Engine wired to fake ffmpeg/ffprobe."""
    fake = fake_ffmpeg_factory(tmp_path, tmp_path / "ffmpeg.log")
    fake_ffprobe_factory(tmp_path)
    return IntroOutroEngine(_container(project_root, tmp_path, str(fake)))


@pytest.fixture
def project(ioe_ff: IntroOutroEngine, tmp_path: Path) -> str:
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    ioe_ff.db.db.execute(
        "INSERT INTO projects (id, title, channel_profile_id, genre,"
        " created_at, updated_at, project_folder_path)"
        " VALUES (?, ?, 'profile_default', 'dark_history', ?, ?, ?)",
        (PROJECT, "The Black Death", NOW, NOW, str(folder)),
    )
    return PROJECT


def _argv(tmp_path: Path) -> str:
    return (tmp_path / "ffmpeg.log").read_text(encoding="utf-8")


class TestCatalogAndSettings:
    def test_catalog_covers_genre_references(
        self, ioe: IntroOutroEngine, project_root: Path
    ) -> None:
        data = ioe.get_available_templates()["data"]
        assert data["count"] == 6
        ids = {t["id"] for t in data["templates"]}
        assert data["default_intro_template"] in ids
        genres = json.loads(
            (project_root / "config" / "documentary_genres.json").read_text()
        )
        for genre in genres.get("genres", []):
            for key in ("intro_template", "outro_template"):
                if genre.get(key):
                    assert genre[key] in ids, f"{genre[key]} missing from catalog"

    def test_settings_default_project(
        self, ioe_ff: IntroOutroEngine, project: str
    ) -> None:
        data = ioe_ff.get_intro_outro_settings(project)["data"]
        assert data["intro"] == {
            "enabled": True,
            "template": "dark_history",
            "duration": 5.0,
            "custom_video": None,
        }
        assert data["outro"]["duration"] == 20.0
        assert data["channel_name"] == "My Channel"
        assert data["title"] == "The Black Death"

    def test_project_kill_switch_disables(
        self, ioe_ff: IntroOutroEngine, project: str
    ) -> None:
        ioe_ff.db.db.execute(
            "UPDATE projects SET has_intro = 0 WHERE id = ?", (project,)
        )
        data = ioe_ff.get_intro_outro_settings(project)["data"]
        assert data["intro"]["enabled"] is False
        assert data["outro"]["enabled"] is True

    def test_custom_profile_override(
        self, ioe_ff: IntroOutroEngine, project: str
    ) -> None:
        ioe_ff.db.db.execute(
            "INSERT INTO channel_profiles (id, profile_name, channel_name,"
            " intro_template, intro_duration, outro_template, outro_duration,"
            " created_at, updated_at)"
            " VALUES ('prof_custom', 'custom', 'Docu Channel', 'mystery', 7.5,"
            " 'conspiracy', 15.0, ?, ?)",
            (NOW, NOW),
        )
        ioe_ff.db.db.execute(
            "UPDATE projects SET channel_profile_id = 'prof_custom' WHERE id = ?",
            (project,),
        )
        data = ioe_ff.get_intro_outro_settings(project)["data"]
        assert data["intro"]["template"] == "mystery"
        assert data["intro"]["duration"] == 7.5
        assert data["outro"]["template"] == "conspiracy"
        assert data["channel_name"] == "Docu Channel"

    def test_genre_template_fallback(
        self, ioe_ff: IntroOutroEngine, project: str
    ) -> None:
        ioe_ff.db.db.execute(
            "UPDATE channel_profiles SET intro_template = '',"
            " outro_template = '' WHERE id = 'profile_default'"
        )
        ioe_ff.db.db.execute(
            "UPDATE projects SET genre = 'true_crime' WHERE id = ?", (project,)
        )
        data = ioe_ff.get_intro_outro_settings(project)["data"]
        assert data["intro"]["template"] == "true_crime"

    def test_duration_clamped(self, ioe_ff: IntroOutroEngine, project: str) -> None:
        ioe_ff.db.db.execute(
            "UPDATE channel_profiles SET intro_duration = 999.0,"
            " outro_duration = 0.1 WHERE id = 'profile_default'"
        )
        data = ioe_ff.get_intro_outro_settings(project)["data"]
        assert data["intro"]["duration"] == 60.0
        assert data["outro"]["duration"] == 0.5


class TestGeneration:
    def test_generate_intro_segment(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        result = ioe_ff.generate_intro(project)
        assert result["success"] is True, result.get("error")
        data = result["data"]
        assert data["template"] == "dark_history"
        assert data["duration"] == 5.0
        assert Path(data["segment_path"]).exists()
        card = Path(data["card_image"])
        assert card.exists()
        with Image.open(card) as img:
            assert img.size == (1920, 1080)
        argv = _argv(tmp_path)
        assert "-loop 1" in argv and "-t 5.000" in argv
        assert "z='1+0.060*on/150'" in argv  # slow_zoom_in, 5s * 30fps
        assert "iw/2-(iw/zoom/2)" in argv
        assert "libx264" in argv and "-crf 18" in argv and "-preset slow" in argv
        assert "-s 1920x1080" in argv and "-pix_fmt yuv420p" in argv
        assert "-an" in argv

    def test_generate_outro_segment(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        result = ioe_ff.generate_outro(project)
        assert result["success"] is True
        assert result["data"]["duration"] == 20.0
        assert "-t 20.000" in _argv(tmp_path)

    def test_disabled_intro_skips_without_ffmpeg(
        self, ioe: IntroOutroEngine, tmp_path: Path
    ) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        ioe.db.db.execute(
            "INSERT INTO projects (id, title, channel_profile_id, has_intro,"
            " created_at, updated_at, project_folder_path)"
            " VALUES ('p2', 'T', 'profile_default', 0, ?, ?, ?)",
            (NOW, NOW, str(folder)),
        )
        f2 = folder / "render"
        result = ioe.generate_intro("p2", f2 / "intro.mp4")
        assert result["success"] is True
        assert result["data"]["skipped"] is True
        assert not (f2 / "intro.mp4").exists()  # ffmpeg never invoked

    def test_zoom_out_template(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        out = tmp_path / "z.mp4"
        result = ioe_ff.generate_intro(
            project, out, overrides={"template": "conspiracy", "duration": 10.0}
        )
        assert result["success"] is True
        assert "z='1.080-0.080*on/300'" in _argv(tmp_path)

    def test_static_template(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        result = ioe_ff.generate_intro(
            project, tmp_path / "s.mp4", overrides={"template": "historical"}
        )
        assert result["success"] is True
        assert "zoompan=z='1':x=" in _argv(tmp_path)

    def test_unknown_template_falls_back(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        result = ioe_ff.generate_intro(
            project, tmp_path / "u.mp4", overrides={"template": "nope"}
        )
        assert result["success"] is True
        assert result["data"]["template"] == "default"
        assert any("Unknown template" in w for w in result["warnings"])

    def test_no_ffmpeg_graceful(
        self,
        ioe: IntroOutroEngine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        ioe.db.db.execute(
            "INSERT INTO projects (id, title, channel_profile_id, created_at,"
            " updated_at, project_folder_path)"
            " VALUES ('p3', 'T', 'profile_default', ?, ?, ?)",
            (NOW, NOW, str(folder)),
        )
        monkeypatch.setattr(ioe.hardware, "find_ffmpeg", lambda: None)
        result = ioe.generate_intro("p3", tmp_path / "x.mp4")
        assert result["success"] is False
        assert "ffmpeg not available" in (result["error"] or "").lower()

    def test_custom_video_missing(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        result = ioe_ff.generate_intro(
            project,
            tmp_path / "c.mp4",
            overrides={"custom_video": str(tmp_path / "ghost.mp4")},
        )
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_custom_video_normalize(
        self,
        ioe_ff: IntroOutroEngine,
        project: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom = tmp_path / "custom.mp4"
        custom.write_bytes(b"RIFF fake video")
        monkeypatch.setenv("FAKE_PROBE_DURATION", "3.0")
        result = ioe_ff.generate_intro(
            project,
            tmp_path / "c.mp4",
            overrides={"custom_video": str(custom), "duration": 5.0},
        )
        assert result["success"] is True
        assert result["data"]["custom_video"] == str(custom)
        argv = _argv(tmp_path)
        assert "-t 3.000" in argv  # trimmed to probed duration
        assert "scale=1920:1080:force_original_aspect_ratio=decrease" in argv
        assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2" in argv

    def test_template_preview(self, ioe: IntroOutroEngine, tmp_path: Path) -> None:
        result = ioe.generate_template_preview(
            "mystery", "outro", tmp_path / "prev.png"
        )
        assert result["success"] is True
        with Image.open(result["data"]["preview_path"]) as img:
            assert img.size == (1920, 1080)


class TestCardPixels:
    def _card(self, engine: IntroOutroEngine, project: str, tmp: Path, tpl: str):
        out = tmp / f"{tpl}.mp4"
        result = engine.generate_intro(project, out, overrides={"template": tpl})
        assert result["success"] is True
        card = Image.open(result["data"]["card_image"]).convert("RGB")
        return card

    def test_gradient_background(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        card = self._card(ioe_ff, project, tmp_path, "dark_history")
        top = card.getpixel((100, 0))  # ratio 0 -> exact background_top
        bottom = card.getpixel((100, 1079))  # ratio 1 -> exact background_bottom
        assert top != bottom
        assert top == (0x0D, 0x0D, 0x1A)
        assert bottom == (0x1A, 0x0A, 0x0A)

    def test_accent_bar_ornament(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        card = self._card(ioe_ff, project, tmp_path, "dark_history")
        assert card.getpixel((960, 540 + 144)) == ACCENTS["dark_history"]
        assert card.getpixel((960, 540)) != ACCENTS["dark_history"]

    def test_double_bar_ornament(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        card = self._card(ioe_ff, project, tmp_path, "true_crime")
        accent = ACCENTS["true_crime"]
        assert card.getpixel((960, 671)) == accent  # thin bar
        assert card.getpixel((960, 697)) == accent  # thick bar
        assert card.getpixel((960, 681)) != accent  # gap between bars

    def test_frame_lines_ornament(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        card = self._card(ioe_ff, project, tmp_path, "historical")
        accent = ACCENTS["historical"]
        assert card.getpixel((49, 540)) == accent  # left frame line
        assert card.getpixel((300, 540)) != accent  # interior

    def test_font_fallback_still_renders(
        self, ioe_ff: IntroOutroEngine, project: str, tmp_path: Path
    ) -> None:
        ioe_ff._font_search_paths = []  # force ImageFont.load_default()
        result = ioe_ff.generate_intro(project, tmp_path / "f.mp4")
        assert result["success"] is True
        assert Path(result["data"]["card_image"]).exists()

    def test_optional_module(self, ioe: IntroOutroEngine) -> None:
        assert ioe.is_optional_module() is True
