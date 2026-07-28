"""Unit tests for modules.channel_profile_manager.ChannelProfileManager.

CRUD against the channel_profiles table (seed row profile_default),
catalog-driven validation (RULE 8), default-profile protection,
project reassignment on delete, and apply_profile_to_project field
mapping onto the projects table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from modules.channel_profile_manager import ChannelProfileManager

PROJECT = "proj-cpm-1"
NOW = "2026-07-16 00:00:00"


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
def cpm(project_root: Path, tmp_path: Path) -> ChannelProfileManager:
    return ChannelProfileManager(_container(project_root, tmp_path))


@pytest.fixture
def project(cpm: ChannelProfileManager, tmp_path: Path) -> str:
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    cpm.db.db.execute(
        "INSERT INTO projects (id, title, channel_profile_id, genre,"
        " created_at, updated_at, project_folder_path)"
        " VALUES (?, ?, 'profile_default', 'dark_history', ?, ?, ?)",
        (PROJECT, "Test Doc", NOW, NOW, str(folder)),
    )
    return PROJECT


# ------------------------------------------------------------------
# List / get / default
# ------------------------------------------------------------------
def test_optional_module_flag(cpm: ChannelProfileManager) -> None:
    assert cpm.is_optional_module() is True


def test_seed_profile_is_default(cpm: ChannelProfileManager) -> None:
    result = cpm.get_default_profile()
    assert result["success"] is True
    assert result["data"]["profile"]["id"] == "profile_default"
    assert result["data"]["profile"]["profile_name"] == "default"
    assert int(result["data"]["profile"]["is_default"]) == 1


def test_list_profiles(cpm: ChannelProfileManager) -> None:
    data = cpm.list_profiles()["data"]
    assert data["count"] == 1
    assert data["profiles"][0]["channel_name"] == "My Channel"


def test_get_profile_by_id_and_name(cpm: ChannelProfileManager) -> None:
    by_id = cpm.get_profile("profile_default")
    by_name = cpm.get_profile("default")
    assert by_id["success"] and by_name["success"]
    assert by_id["data"]["profile"]["id"] == by_name["data"]["profile"]["id"]
    assert by_name["data"]["resolved"] is True


def test_get_profile_falls_back_to_default(cpm: ChannelProfileManager) -> None:
    result = cpm.get_profile("no-such-ref")
    assert result["success"] is True  # is_default fallback (lookup only)
    assert result["data"]["resolved"] is False


# ------------------------------------------------------------------
# Create + validation
# ------------------------------------------------------------------
def test_create_profile_uses_json_defaults(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile(
        {"profile_name": "True Crime", "genre": "true_crime"}
    )
    assert result["success"] is True, result["error"]
    profile = result["data"]["profile"]
    assert profile["id"] == "profile_true_crime"
    assert profile["channel_name"] == "My Channel"  # JSON default
    assert profile["default_export_preset"] == "youtube_1080p"
    assert profile["intro_template"] == "dark_history"
    assert profile["outro_template"] == "dark_history"
    assert int(profile["intro_enabled"]) == 1
    assert abs(float(profile["music_volume"]) - 0.4) < 1e-6
    assert profile["created_at"] and profile["updated_at"]


def test_create_duplicate_name_rejected(cpm: ChannelProfileManager) -> None:
    cpm.create_profile({"profile_name": "Alpha"})
    result = cpm.create_profile({"profile_name": "Alpha"})
    assert result["success"] is False
    assert "already exists" in result["error"]


def test_create_invalid_genre_rejected(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile({"profile_name": "Bad", "genre": "nope"})
    assert result["success"] is False
    assert "genre" in result["error"]


def test_create_invalid_color_rejected(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile(
        {"profile_name": "Bad", "color_primary": "red-ish"}
    )
    assert result["success"] is False
    assert "color_primary" in result["error"]


def test_create_normalizes_color_case(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile(
        {"profile_name": "Norm", "color_primary": "#a1b2c3"}
    )
    assert result["success"] is True
    assert result["data"]["profile"]["color_primary"] == "#A1B2C3"


def test_create_clamps_with_warnings(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile(
        {"profile_name": "Clamp", "music_volume": 1.7, "intro_duration": 999}
    )
    assert result["success"] is True
    profile = result["data"]["profile"]
    assert float(profile["music_volume"]) == 1.0
    assert float(profile["intro_duration"]) == 60.0
    assert any("clamped" in w for w in result["warnings"])


def test_create_unknown_column_warns(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile({"profile_name": "Odd", "bogus_column": 1})
    assert result["success"] is True
    assert any("bogus_column" in w for w in result["warnings"])
    assert "bogus_column" not in result["data"]["profile"]


def test_create_missing_name_rejected(cpm: ChannelProfileManager) -> None:
    result = cpm.create_profile({"channel_name": "X"})
    assert result["success"] is False
    assert "profile_name" in result["error"]


# ------------------------------------------------------------------
# Update / duplicate / default flag
# ------------------------------------------------------------------
def test_update_profile_changes_columns(cpm: ChannelProfileManager) -> None:
    row = cpm.get_default_profile()["data"]["profile"]
    result = cpm.update_profile(
        row["id"], {"channel_name": "New Channel", "genre": "mystery"}
    )
    assert result["success"] is True
    updated = result["data"]["profile"]
    assert updated["channel_name"] == "New Channel"
    assert updated["genre"] == "mystery"
    assert sorted(result["data"]["updated_columns"]) == ["channel_name", "genre"]


def test_update_internal_column_ignored(cpm: ChannelProfileManager) -> None:
    row = cpm.get_default_profile()["data"]["profile"]
    result = cpm.update_profile(row["id"], {"is_default": 0})
    assert result["success"] is False  # nothing else to update
    assert any("is_default" in w for w in result["warnings"])
    assert int(cpm.get_default_profile()["data"]["profile"]["is_default"]) == 1


def test_update_unknown_profile_rejected(cpm: ChannelProfileManager) -> None:
    result = cpm.update_profile("nope", {"channel_name": "X"})
    assert result["success"] is False


def test_duplicate_profile(cpm: ChannelProfileManager) -> None:
    result = cpm.duplicate_profile("profile_default", "Copy One")
    assert result["success"] is True
    copied = result["data"]["profile"]
    assert copied["profile_name"] == "Copy One"
    assert copied["id"] != "profile_default"
    assert copied["channel_name"] == "My Channel"
    assert int(copied["is_default"]) == 0


def test_set_default_profile_exclusive(cpm: ChannelProfileManager) -> None:
    created = cpm.create_profile({"profile_name": "Second"})
    pid = created["data"]["id"]
    result = cpm.set_default_profile(pid)
    assert result["success"] is True
    rows = cpm.list_profiles()["data"]["profiles"]
    flags = {r["id"]: int(r["is_default"]) for r in rows}
    assert flags[pid] == 1
    assert flags["profile_default"] == 0


# ------------------------------------------------------------------
# Delete semantics
# ------------------------------------------------------------------
def test_delete_default_profile_blocked(cpm: ChannelProfileManager) -> None:
    result = cpm.delete_profile("profile_default")
    assert result["success"] is False
    assert "default" in result["error"]


def test_delete_reassigns_projects(
    cpm: ChannelProfileManager, project: str
) -> None:
    created = cpm.create_profile({"profile_name": "Temp"})
    pid = created["data"]["id"]
    assert cpm.apply_profile_to_project(project, pid)["success"]
    result = cpm.delete_profile(pid)
    assert result["success"] is True
    assert result["data"]["projects_reassigned"] == 1
    assert any("reassigned" in w for w in result["warnings"])
    row = cpm.db.db.fetch_one(
        "SELECT channel_profile_id FROM projects WHERE id = ?", (project,)
    )
    assert row["channel_profile_id"] == "profile_default"


def test_delete_unknown_profile_rejected(cpm: ChannelProfileManager) -> None:
    result = cpm.delete_profile("nope")
    assert result["success"] is False


# ------------------------------------------------------------------
# Apply to project
# ------------------------------------------------------------------
def test_apply_profile_maps_columns(
    cpm: ChannelProfileManager, project: str
) -> None:
    created = cpm.create_profile(
        {
            "profile_name": "Crime",
            "genre": "true_crime",
            "default_export_preset": "youtube_1080p",
            "default_transition": "crossfade",
            "default_animation": "ken_burns",
            "default_subtitle_style": "word_by_word",
            "intro_enabled": 0,
            "watermark_enabled": 0,
            "music_volume": 0.25,
        }
    )
    pid = created["data"]["id"]
    result = cpm.apply_profile_to_project(project, pid)
    assert result["success"] is True
    row = cpm.db.db.fetch_one(
        "SELECT * FROM projects WHERE id = ?", (project,)
    )
    assert row["channel_profile_id"] == pid
    assert row["genre"] == "true_crime"
    assert row["export_preset"] == "youtube_1080p"
    assert row["default_transition"] == "crossfade"
    assert row["default_animation"] == "ken_burns"
    assert row["default_subtitle_style"] == "word_by_word"
    assert int(row["has_intro"]) == 0
    assert int(row["has_watermark"]) == 0
    assert abs(float(row["music_volume"]) - 0.25) < 1e-6
    assert "channel_profile_id" in result["data"]["updated_columns"]


def test_apply_unknown_project_rejected(cpm: ChannelProfileManager) -> None:
    result = cpm.apply_profile_to_project("no-such", "profile_default")
    assert result["success"] is False
    assert "Project not found" in result["error"]


def test_apply_unknown_profile_rejected(
    cpm: ChannelProfileManager, project: str
) -> None:
    result = cpm.apply_profile_to_project(project, "nope")
    assert result["success"] is False
    assert "Profile not found" in result["error"]


def test_get_project_profile(cpm: ChannelProfileManager, project: str) -> None:
    result = cpm.get_project_profile(project)
    assert result["success"] is True
    assert result["data"]["profile"]["id"] == "profile_default"
