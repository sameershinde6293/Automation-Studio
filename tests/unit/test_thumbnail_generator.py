"""Unit tests for modules.thumbnail_generator.ThumbnailGenerator.

Settings resolution chain, Pillow composition (pixel-level gradient,
ornament, overlay, and vignette assertions), variation cycling, DB
persistence to the thumbnails table, selection/deletion semantics, and
graceful degradation. Pure Pillow - no FFmpeg doubles required.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from core.service_container import ServiceContainer
from modules.thumbnail_generator import (
    _ORNAMENT_Y,
    _FRAME_INSET,
    HEIGHT,
    WIDTH,
    ThumbnailGenerator,
)

PROJECT = "proj-thumb-1"
NOW = "2026-07-16 00:00:00"
ACCENTS = {
    "dark_history": (0x8B, 0x00, 0x00),
    "true_crime": (0xFF, 0x00, 0x00),
    "historical": (0xC9, 0xA2, 0x27),
}


def _container(project_root: Path, tmp_path: Path) -> ServiceContainer:
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
def tg(project_root: Path, tmp_path: Path) -> ThumbnailGenerator:
    return ThumbnailGenerator(_container(project_root, tmp_path))


@pytest.fixture
def project(tg: ThumbnailGenerator, tmp_path: Path) -> str:
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    tg.db.db.execute(
        "INSERT INTO projects (id, title, channel_profile_id, genre,"
        " created_at, updated_at, project_folder_path)"
        " VALUES (?, ?, 'profile_default', 'dark_history', ?, ?, ?)",
        (PROJECT, "The Black Death", NOW, NOW, str(folder)),
    )
    return PROJECT


def _scene(
    tg: ThumbnailGenerator,
    project_id: str,
    number: int,
    image_path: str,
    duration: float,
) -> None:
    tg.db.db.execute(
        "INSERT INTO scenes (id, project_id, scene_number, image_file_path,"
        " duration, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"scene-{number}", project_id, number, image_path, duration, NOW, NOW),
    )


def _solid(path: Path, rgb, size=(1600, 900)) -> Path:
    Image.new("RGB", size, rgb).save(path)
    return path


# ------------------------------------------------------------------
# Catalog and settings chain
# ------------------------------------------------------------------
def test_optional_module_flag(tg: ThumbnailGenerator) -> None:
    assert tg.is_optional_module() is True


def test_available_styles_catalog(tg: ThumbnailGenerator) -> None:
    result = tg.get_available_styles()
    assert result["success"] is True
    ids = [s["id"] for s in result["data"]["styles"]]
    assert ids == sorted(ids)
    assert "dark_history" in ids
    assert result["data"]["default_style"] == "dark_history"
    assert result["data"]["max_variations"] == 5


def test_settings_from_default_profile(
    tg: ThumbnailGenerator, project: str
) -> None:
    data = tg.get_thumbnail_settings(project)["data"]
    assert data["style_id"] == "dark_history"
    assert data["channel_text"] == "My Channel"
    assert data["title_text"] == "The Black Death"
    assert data["count"] == 5  # app_settings.thumbnail_count
    assert data["auto_enabled"] is True
    assert data["output_folder"].endswith("thumbnails")


def test_settings_missing_project_error(tg: ThumbnailGenerator) -> None:
    result = tg.get_thumbnail_settings("no-such-project")
    assert result["success"] is False
    assert "not found" in result["error"]


def test_settings_count_clamped(
    tg: ThumbnailGenerator, project: str, monkeypatch
) -> None:
    monkeypatch.setattr(tg.config, "get", lambda key, default=None: 99)
    data = tg.get_thumbnail_settings(project)["data"]
    assert data["count"] == 5  # schema contract: variations 1..5


# ------------------------------------------------------------------
# Composition (pixel level)
# ------------------------------------------------------------------
def test_gradient_card_top_bottom_differ(tg: ThumbnailGenerator) -> None:
    card = tg._gradient_card(tg._resolve_style("dark_history"))
    assert card.size == (WIDTH, HEIGHT)
    assert card.getpixel((WIDTH // 2, 0)) == (0x1A, 0x1A, 0x2E)
    assert card.getpixel((WIDTH // 2, HEIGHT - 1)) == (0x0D, 0x0D, 0x1A)


def test_accent_bar_pixel(tg: ThumbnailGenerator) -> None:
    style = dict(tg._resolve_style("dark_history"), vignette=False,
                 overlay_opacity=0.0)
    image = tg._compose(None, style, "Title", "Channel")
    assert image.getpixel((WIDTH // 2, _ORNAMENT_Y + 4)) == ACCENTS["dark_history"]
    assert image.getpixel((WIDTH // 2, _ORNAMENT_Y - 2)) != ACCENTS["dark_history"]


def test_double_bar_pixels(tg: ThumbnailGenerator) -> None:
    style = dict(tg._resolve_style("true_crime"), vignette=False,
                 overlay_opacity=0.0)
    image = tg._compose(None, style, "Title", "Channel")
    assert image.getpixel((WIDTH // 2, _ORNAMENT_Y + 1)) == ACCENTS["true_crime"]
    assert image.getpixel((WIDTH // 2, _ORNAMENT_Y + 27)) == ACCENTS["true_crime"]
    assert image.getpixel((WIDTH // 2, _ORNAMENT_Y + 10)) != ACCENTS["true_crime"]


def test_frame_lines_pixel(tg: ThumbnailGenerator) -> None:
    style = dict(tg._resolve_style("historical"), vignette=False,
                 overlay_opacity=0.0)
    image = tg._compose(None, style, "Title", "Channel")
    assert image.getpixel((_FRAME_INSET + 1, HEIGHT // 2)) == ACCENTS["historical"]
    assert image.getpixel((WIDTH // 2, _ORNAMENT_Y + 4)) != ACCENTS["historical"]


def test_overlay_darkens_source(tg: ThumbnailGenerator, tmp_path: Path) -> None:
    source = _solid(tmp_path / "white.png", (255, 255, 255))
    style = dict(tg._resolve_style("dark_history"), vignette=False)
    image = tg._compose(str(source), style, "Title", "Channel")
    # center pixel: white blended with black at opacity 0.55
    pixel = image.getpixel((WIDTH // 2, HEIGHT // 2))[0]
    assert abs(pixel - 255 * (1.0 - 0.55)) <= 2


def test_vignette_corners_darker(tg: ThumbnailGenerator) -> None:
    style = dict(tg._resolve_style("mystery"), overlay_opacity=0.0)
    image = tg._compose(None, style, "Title", "Channel")
    center = image.getpixel((WIDTH // 2, HEIGHT // 4))
    corner = image.getpixel((3, 3))
    assert sum(corner) < sum(center)


# ------------------------------------------------------------------
# Generation end-to-end (Pillow only)
# ------------------------------------------------------------------
def test_generate_creates_five_rows_and_files(
    tg: ThumbnailGenerator, project: str
) -> None:
    result = tg.generate_thumbnails(project)
    assert result["success"] is True
    assert result["data"]["count"] == 5
    rows = tg.db.db.fetch_all(
        "SELECT * FROM thumbnails WHERE project_id = ? ORDER BY variation_number",
        (project,),
    )
    assert len(rows) == 5
    for index, row in enumerate(rows, start=1):
        assert row["variation_number"] == index
        assert row["width"] == WIDTH and row["height"] == HEIGHT
        if index == 1:
            assert row["style_applied"] == "dark_history"
        assert row["file_size_bytes"] == os.path.getsize(row["file_path"])
        assert row["file_size_bytes"] > 0
        assert row["title_text"] == "The Black Death"
        assert row["channel_text"] == "My Channel"
        assert row["is_selected"] == 0
        assert Path(row["file_path"]).exists()
        assert row["file_path"].endswith(".jpg")


def test_generate_count_override_and_clamp(
    tg: ThumbnailGenerator, project: str
) -> None:
    result = tg.generate_thumbnails(project, count=2)
    assert result["data"]["count"] == 2
    assert [t["variation_number"] for t in result["data"]["thumbnails"]] == [1, 2]
    clamped = tg.generate_thumbnails(project, count=9)
    assert clamped["data"]["count"] == 5


def test_generate_uses_scene_sources_with_timestamps(
    tg: ThumbnailGenerator, project: str, tmp_path: Path
) -> None:
    img1 = _solid(tmp_path / "a.jpg", (200, 30, 30))
    img2 = _solid(tmp_path / "b.jpg", (30, 30, 200))
    _scene(tg, project, 1, str(img1), 6.0)
    _scene(tg, project, 2, str(img2), 4.0)
    result = tg.generate_thumbnails(project, count=3)
    thumbs = result["data"]["thumbnails"]
    assert thumbs[0]["source_timestamp"] == 0.0
    assert thumbs[1]["source_timestamp"] == 6.0
    assert thumbs[2]["source_timestamp"] == 0.0  # sources cycle


def test_styles_cycle_after_profile_style(
    tg: ThumbnailGenerator, project: str
) -> None:
    result = tg.generate_thumbnails(project, count=5)
    styles = [t["style_applied"] for t in result["data"]["thumbnails"]]
    assert styles[0] == "dark_history"  # profile style first
    assert len(set(styles)) == 5  # then the rest of the catalog, unique


def test_unknown_style_id_warns_and_uses_default(
    tg: ThumbnailGenerator, project: str
) -> None:
    result = tg.generate_thumbnails(project, count=1, style_ids=["nope"])
    assert result["success"] is True
    assert any("nope" in w for w in result["warnings"])
    assert result["data"]["thumbnails"][0]["style_applied"] == "dark_history"


def test_corrupt_source_falls_back_to_gradient(
    tg: ThumbnailGenerator, project: str, tmp_path: Path
) -> None:
    bogus = tmp_path / "broken.jpg"
    bogus.write_text("not an image", encoding="utf-8")
    _scene(tg, project, 1, str(bogus), 5.0)
    result = tg.generate_thumbnails(project, count=1)
    assert result["success"] is True
    assert any("unusable" in w for w in result["warnings"])
    row = tg.db.db.fetch_one(
        "SELECT source_timestamp FROM thumbnails WHERE project_id = ?",
        (project,),
    )
    assert row["source_timestamp"] == 0.0


def test_generate_missing_project_error(tg: ThumbnailGenerator) -> None:
    result = tg.generate_thumbnails("no-such-project")
    assert result["success"] is False
    assert "not found" in result["error"]


def test_generate_disabled_module_error(
    tg: ThumbnailGenerator, project: str
) -> None:
    tg.set_enabled(False)
    result = tg.generate_thumbnails(project)
    assert result["success"] is False
    assert "disabled" in result["error"]


def test_generate_publishes_event(
    tg: ThumbnailGenerator, project: str
) -> None:
    captured = []
    tg.event_bus.subscribe("thumbnails.generated", captured.append)
    tg.generate_thumbnails(project, count=2)
    assert len(captured) == 1
    assert captured[0]["project_id"] == project
    assert captured[0]["count"] == 2
    assert len(captured[0]["file_paths"]) == 2


def test_auto_generate_respects_kill_switch(
    tg: ThumbnailGenerator, project: str, monkeypatch
) -> None:
    monkeypatch.setattr(tg.config, "get", lambda key, default=None: False)
    result = tg.auto_generate_for_project(project)
    assert result["success"] is True
    assert result["data"]["skipped"] is True
    assert (
        tg.db.db.fetch_one(
            "SELECT id FROM thumbnails WHERE project_id = ?", (project,)
        )
        is None
    )


def test_title_and_channel_overrides(
    tg: ThumbnailGenerator, project: str
) -> None:
    result = tg.generate_thumbnails(
        project, count=1, title_text="X", channel_text="Y"
    )
    thumb = result["data"]["thumbnails"][0]
    assert thumb["title_text"] == "X"
    assert thumb["channel_text"] == "Y"


# ------------------------------------------------------------------
# Selection / deletion
# ------------------------------------------------------------------
def _generated_rows(tg: ThumbnailGenerator, project: str, count: int = 3):
    tg.generate_thumbnails(project, count=count)
    return tg.db.db.fetch_all(
        "SELECT * FROM thumbnails WHERE project_id = ? ORDER BY variation_number",
        (project,),
    )


def test_select_thumbnail_exclusive(
    tg: ThumbnailGenerator, project: str
) -> None:
    rows = _generated_rows(tg, project)
    result = tg.select_thumbnail(project, rows[1]["id"])
    assert result["success"] is True
    flags = {
        r["id"]: r["is_selected"]
        for r in tg.db.db.fetch_all(
            "SELECT id, is_selected FROM thumbnails WHERE project_id = ?",
            (project,),
        )
    }
    assert flags[rows[1]["id"]] == 1
    assert sum(flags.values()) == 1
    # re-selecting a different one moves the flag
    tg.select_thumbnail(project, rows[2]["id"])
    flags = {
        r["id"]: r["is_selected"]
        for r in tg.db.db.fetch_all(
            "SELECT id, is_selected FROM thumbnails WHERE project_id = ?",
            (project,),
        )
    }
    assert flags[rows[2]["id"]] == 1 and flags[rows[1]["id"]] == 0


def test_select_missing_thumbnail_error(
    tg: ThumbnailGenerator, project: str
) -> None:
    result = tg.select_thumbnail(project, "no-such-thumb")
    assert result["success"] is False


def test_delete_thumbnail_removes_row_and_file(
    tg: ThumbnailGenerator, project: str
) -> None:
    rows = _generated_rows(tg, project, count=1)
    path = Path(rows[0]["file_path"])
    assert path.exists()
    result = tg.delete_thumbnail(rows[0]["id"])
    assert result["success"] is True
    assert not path.exists()
    assert (
        tg.db.db.fetch_one(
            "SELECT id FROM thumbnails WHERE id = ?", (rows[0]["id"],)
        )
        is None
    )


def test_delete_project_thumbnails(
    tg: ThumbnailGenerator, project: str
) -> None:
    rows = _generated_rows(tg, project)
    paths = [Path(r["file_path"]) for r in rows]
    result = tg.delete_project_thumbnails(project)
    assert result["data"]["deleted"] == 3
    assert all(not p.exists() for p in paths)
    remaining = tg.list_thumbnails(project)
    assert remaining["data"]["count"] == 0


def test_list_thumbnails_order(
    tg: ThumbnailGenerator, project: str
) -> None:
    _generated_rows(tg, project, count=4)
    listed = tg.list_thumbnails(project)["data"]["thumbnails"]
    assert [r["variation_number"] for r in listed] == [1, 2, 3, 4]
