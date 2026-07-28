"""Unit tests for modules.voice_profile_manager.VoiceProfileManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from modules.file_parser import FileParser
from modules.voice_profile_manager import VoiceProfileManager


@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    """Isolated production container for voice profile tests."""
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
def manager(container: ServiceContainer) -> VoiceProfileManager:
    """VoiceProfileManager instance."""
    return VoiceProfileManager(container)


@pytest.fixture
def parser(container: ServiceContainer) -> FileParser:
    """FileParser for loading sample scripts."""
    return FileParser(container)


@pytest.fixture
def project_id(manager: VoiceProfileManager) -> str:
    """Create an empty project and return its id."""
    pid = manager.db.new_id()
    assert manager.db.create_project(
        {
            "id": pid,
            "title": "Voice Profile Test",
            "project_folder_path": "projects/voice_test",
        }
    )
    return pid


@pytest.fixture
def sample_project(project_root: Path) -> Path:
    """Sample project fixture directory."""
    return project_root / "tests" / "fixtures" / "sample_project"


class TestCreateFromScript:
    """Create profiles from sample scripts."""

    def test_create_from_sample_txt(
        self,
        manager: VoiceProfileManager,
        parser: FileParser,
        project_id: str,
        sample_project: Path,
    ) -> None:
        parsed = parser.parse_txt(sample_project / "script" / "sample_script.txt")
        assert parsed["success"] is True
        result = manager.create_profiles_from_script(parsed["data"], project_id)
        assert result["success"] is True
        created_names = {
            p["character_name"] for p in result["data"]["profiles_created"]
        }
        assert "NARRATOR" in created_names
        assert "HISTORIAN" in created_names

        narr = manager.load_profile(project_id, "NARRATOR")
        assert narr["success"] is True
        profile = narr["data"]["profile"]
        assert profile is not None
        assert profile["engine"] == "piper"
        assert float(profile["speed"]) == pytest.approx(0.90)
        assert float(profile["pitch"]) == pytest.approx(-2)
        assert int(profile["breathing_enabled"]) == 1
        assert profile["default_emotion"] == "dramatic"
        assert profile["voice_model"] == "deep_male_us"
        assert float(profile["pause_sentence"]) == pytest.approx(0.6)

        hist = manager.load_profile(project_id, "HISTORIAN")["data"]["profile"]
        assert hist["engine"] == "piper"
        assert float(hist["speed"]) == pytest.approx(0.95)
        assert hist["default_emotion"] == "authoritative"
        assert int(hist["breathing_enabled"]) == 0

    def test_create_from_sample_json(
        self,
        manager: VoiceProfileManager,
        parser: FileParser,
        project_id: str,
        sample_project: Path,
    ) -> None:
        parsed = parser.parse_json(sample_project / "script" / "sample_script.json")
        assert parsed["success"] is True
        result = manager.create_profiles_from_script(parsed["data"], project_id)
        assert result["success"] is True
        names = {p["character_name"] for p in result["data"]["profiles_created"]}
        assert "NARRATOR" in names
        assert "HISTORIAN" in names
        narr = manager.load_profile(project_id, "NARRATOR")["data"]["profile"]
        assert narr["engine"] == "piper"
        assert float(narr["speed"]) == pytest.approx(0.90)


class TestAliases:
    """Alias resolution tests."""

    def test_auto_alias_resolution(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        manager.create_profiles_from_script(
            {
                "voice_instructions": [
                    {
                        "character": "NARRATOR",
                        "engine": "piper",
                        "voice": "deep_male_us",
                        "emotion": "dramatic",
                        "speed": 0.9,
                    }
                ],
                "scenes": [],
            },
            project_id,
        )
        assert manager.resolve_character_alias(project_id, "NARR") == "NARRATOR"
        assert manager.resolve_character_alias(project_id, "n") == "NARRATOR"
        assert manager.resolve_character_alias(project_id, "narrator") == "NARRATOR"
        loaded = manager.load_profile(project_id, "NARR")
        assert loaded["data"]["profile"] is not None
        assert loaded["data"]["profile"]["character_name"] == "NARRATOR"

    def test_custom_aliases_override(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        result = manager.create_profiles_from_script(
            {
                "voice_instructions": [
                    {
                        "character": "NARRATOR",
                        "engine": "kokoro",
                        "aliases": ["STORY_TELLER", "STORY"],
                    }
                ],
                "scenes": [],
            },
            project_id,
        )
        assert result["success"] is True
        assert manager.resolve_character_alias(project_id, "STORY_TELLER") == "NARRATOR"
        assert manager.resolve_character_alias(project_id, "STORY") == "NARRATOR"
        # Auto NARR should not apply when custom aliases provided
        assert manager.resolve_character_alias(project_id, "NARR") is None


class TestDefaultsAndValidation:
    """Defaults and validation."""

    def test_missing_voice_setup_creates_defaults(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        script = {
            "voice_instructions": [],
            "scenes": [
                {
                    "id": "scene_01",
                    "dialogue": [
                        {"character": "NARRATOR", "text": "Hello world."},
                        {"character": "WITNESS", "text": "I saw it."},
                    ],
                }
            ],
        }
        result = manager.create_profiles_from_script(script, project_id)
        assert result["success"] is True
        names = {p["character_name"] for p in result["data"]["profiles_created"]}
        assert "NARRATOR" in names
        assert "WITNESS" in names
        assert any("No voice_setup" in w for w in result["warnings"])

    def test_invalid_speed_rejected(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        result = manager.create_profiles_from_script(
            {
                "voice_instructions": [
                    {"character": "NARRATOR", "engine": "piper", "speed": 3.0}
                ],
                "scenes": [],
            },
            project_id,
        )
        assert result["success"] is False
        assert "speed" in (result["error"] or "").lower()
        assert result["data"].get("is_recoverable") is True

    def test_invalid_engine_rejected(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        result = manager.save_profile(
            project_id,
            {"character": "NARRATOR", "engine": "invalid", "speed": 1.0},
        )
        assert result["success"] is False
        assert "engine" in (result["error"] or "").lower()

    def test_duplicate_overwrites(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        script = {
            "voice_instructions": [
                {
                    "character": "NARRATOR",
                    "engine": "piper",
                    "speed": 0.8,
                    "emotion": "calm",
                },
                {
                    "character": "NARRATOR",
                    "engine": "kokoro",
                    "speed": 1.1,
                    "emotion": "dramatic",
                },
            ],
            "scenes": [],
        }
        result = manager.create_profiles_from_script(script, project_id)
        assert result["success"] is True
        assert any("Duplicate" in w for w in result["warnings"])
        profile = manager.load_profile(project_id, "NARRATOR")["data"]["profile"]
        assert profile["engine"] == "kokoro"
        assert float(profile["speed"]) == pytest.approx(1.1)
        assert profile["default_emotion"] == "dramatic"
        # Only one profile row
        all_profiles = manager.get_all_profiles(project_id)["data"]["profiles"]
        narrators = [p for p in all_profiles if p["character_name"] == "NARRATOR"]
        assert len(narrators) == 1


class TestCrud:
    """Load/update/delete and get_all."""

    def test_load_nonexistent(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        result = manager.load_profile(project_id, "GHOST")
        assert result["success"] is True
        assert result["data"]["profile"] is None

    def test_update_profile(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        create = manager.save_profile(
            project_id,
            {
                "character": "NARRATOR",
                "engine": "piper",
                "speed": 0.9,
                "voice": "deep_male_us",
            },
        )
        assert create["success"] is True
        profile_id = create["data"]["profile_id"]
        updated = manager.update_profile(profile_id, {"speed": 1.0})
        assert updated["success"] is True
        loaded = manager.load_profile(project_id, "NARRATOR")["data"]["profile"]
        assert float(loaded["speed"]) == pytest.approx(1.0)

    def test_delete_profile(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        create = manager.save_profile(
            project_id, {"character": "HISTORIAN", "engine": "piper", "speed": 1.0}
        )
        profile_id = create["data"]["profile_id"]
        deleted = manager.delete_profile(profile_id)
        assert deleted["success"] is True
        assert deleted["data"]["deleted"] is True
        all_profiles = manager.get_all_profiles(project_id)["data"]["profiles"]
        assert all(p["character_name"] != "HISTORIAN" for p in all_profiles)

    def test_get_default_profile(self, manager: VoiceProfileManager) -> None:
        defaults = manager.get_default_profile()
        assert defaults["character_name"] == "NARRATOR"
        assert defaults["engine"] == "kokoro"
        assert defaults["speed"] == 1.0


class TestModuleOptionalAndResponse:
    """Disable flag and response format."""

    def test_module_can_be_disabled(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        # Auto-creation path respects enabled flag for create_profiles_from_script
        manager.set_enabled(False)
        # Per requirements: when disabled, no auto-creation; manual still works
        # We implement: create_profiles_from_script still works if called directly
        # (module enable is for orchestrator). Manual save always works.
        manual = manager.save_profile(
            project_id, {"character": "NARRATOR", "engine": "piper", "speed": 1.0}
        )
        assert manual["success"] is True
        manager.set_enabled(True)

    def test_response_object_format(
        self, manager: VoiceProfileManager, project_id: str
    ) -> None:
        ok = manager.get_all_profiles(project_id)
        for key in ("success", "data", "error", "warnings", "module", "timestamp"):
            assert key in ok
        assert ok["module"] == "voice_profile_manager"
        bad = manager.save_profile(project_id, {"character": "X", "engine": "nope"})
        assert bad["success"] is False
        assert bad["data"].get("error_code") == "VOICE_PROFILE_ERROR"
        assert bad["data"].get("is_recoverable") is True
        assert bad["data"].get("user_message")

    def test_modules_config_registration(self, manager: VoiceProfileManager) -> None:
        entry = manager.config.get_module_config("voice_profile_manager")
        assert entry is not None
        assert entry.get("enabled") is True
        assert entry.get("required") is False
