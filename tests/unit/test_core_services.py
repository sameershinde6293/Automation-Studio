"""Unit tests for Phase A core services."""

from __future__ import annotations

from pathlib import Path

from core.correlation import CorrelationContext
from core.database_service import PRODUCT_TABLES
from core.event_bus import EventBus
from core.render_state_machine import (
    RenderState,
    RenderStateMachine,
    all_render_states,
)
from core.time_helper import utc_now_str


class TestTimeHelper:
    """Tests for UTC time helpers."""

    def test_utc_now_str_format(self) -> None:
        value = utc_now_str()
        assert len(value) == 19
        assert value[4] == "-" and value[10] == " "


class TestEventBus:
    """Tests for EventBus publish/subscribe."""

    def test_subscribe_publish(self, event_bus: EventBus) -> None:
        received = []

        def handler(data: object) -> None:
            received.append(data)

        event_bus.subscribe("test.event", handler)
        count = event_bus.publish("test.event", {"ok": True})
        assert count == 1
        assert received == [{"ok": True}]

    def test_unsubscribe(self, event_bus: EventBus) -> None:
        received = []

        def handler(data: object) -> None:
            received.append(data)

        event_bus.subscribe("x", handler)
        event_bus.unsubscribe("x", handler)
        event_bus.publish("x", 1)
        assert received == []


class TestDatabaseService:
    """Tests for schema and domain DB methods."""

    def test_product_table_count_constant(self) -> None:
        assert len(PRODUCT_TABLES) == 25

    def test_all_product_tables_exist(self, database_service) -> None:
        ok, missing = database_service.verify_product_tables()
        assert ok is True
        assert missing == []

    def test_integrity_check(self, database_service) -> None:
        assert database_service.integrity_check() is True

    def test_create_and_get_project(self, database_service) -> None:
        project_id = database_service.new_id()
        ok = database_service.create_project(
            {
                "id": project_id,
                "title": "Test Project",
                "channel_profile_id": "default",
                "genre": "dark_history",
                "status": "new",
                "created_at": utc_now_str(),
                "updated_at": utc_now_str(),
                "project_folder_path": "projects/test",
            }
        )
        assert ok is True
        row = database_service.get_project(project_id)
        assert row is not None
        assert row["title"] == "Test Project"

    def test_save_and_get_scene(self, database_service) -> None:
        project_id = database_service.new_id()
        database_service.create_project(
            {
                "id": project_id,
                "title": "Scene Project",
                "project_folder_path": "projects/scene",
            }
        )
        scene_id = database_service.new_id()
        ok = database_service.save_scene(
            {
                "id": scene_id,
                "project_id": project_id,
                "scene_number": 1,
                "image_filename": "dark_castle_night.jpg",
            }
        )
        assert ok is True
        scenes = database_service.get_all_scenes(project_id)
        assert len(scenes) == 1
        assert scenes[0]["image_filename"] == "dark_castle_night.jpg"


class TestConfigService:
    """Tests for config JSON loading."""

    def test_sixteen_configs_valid(self, config_service, project_root: Path) -> None:
        results = config_service.validate_all_json()
        assert len(results) == 16
        assert all(results.values()), f"Invalid configs: {results}"

    def test_modules_include_keyword_analyzer(self, config_service) -> None:
        entry = config_service.get_module_config("keyword_analyzer")
        assert entry is not None
        assert entry.get("enabled") is True

    def test_app_settings_keys(self, config_service) -> None:
        assert config_service.get("theme") == "dark"
        assert config_service.get("startup_ram_target_mb") == 150


class TestRenderStateMachine:
    """Tests for 12-state render FSM."""

    def test_twelve_states(self) -> None:
        assert len(all_render_states()) == 12

    def test_valid_happy_path(self, event_bus: EventBus) -> None:
        sm = RenderStateMachine(event_bus)
        events = []
        event_bus.subscribe("render_state_changed", lambda d: events.append(d))

        assert sm.transition_to(RenderState.LOADING)
        assert sm.transition_to(RenderState.VALIDATING)
        assert sm.transition_to(RenderState.GENERATING)
        assert sm.transition_to(RenderState.PROCESSING)
        assert sm.transition_to(RenderState.RENDERING)
        assert sm.transition_to(RenderState.EXPORTING)
        assert sm.transition_to(RenderState.COMPLETE)
        assert sm.state == RenderState.COMPLETE
        assert len(events) == 7

    def test_invalid_transition_blocked(self, event_bus: EventBus) -> None:
        sm = RenderStateMachine(event_bus)
        assert sm.transition_to(RenderState.RENDERING) is False
        assert sm.state == RenderState.IDLE

    def test_action_allowed_idle(self, event_bus: EventBus) -> None:
        sm = RenderStateMachine(event_bus)
        assert sm.is_action_allowed("render") is True
        sm.transition_to(RenderState.LOADING)
        assert sm.is_action_allowed("render") is False
        assert sm.is_action_allowed("cancel") is True


class TestCorrelation:
    """Tests for correlation ID context."""

    def test_set_get_clear(self) -> None:
        CorrelationContext.clear()
        assert CorrelationContext.get() == ""
        CorrelationContext.set("ABCD1234")
        assert CorrelationContext.get() == "ABCD1234"
        assert CorrelationContext.format_prefix() == "[ABCD1234] "
        CorrelationContext.clear()
        assert CorrelationContext.format_prefix() == ""

    def test_new_id(self) -> None:
        CorrelationContext.clear()
        cid = CorrelationContext.new_id()
        assert len(cid) == 8
        assert cid == CorrelationContext.get()
        CorrelationContext.clear()


class TestCacheService:
    """Tests for disk cache."""

    def test_set_get_json(self, tmp_path: Path) -> None:
        from core.cache_service import CacheService

        cache = CacheService(cache_folder=tmp_path / "cache", max_size_mb=16)
        key = CacheService.make_key("unit", "test")
        assert cache.set_json(key, {"a": 1}) is True
        assert cache.get_json(key) == {"a": 1}


class TestServiceContainer:
    """Tests for DI container."""

    def test_production_container_services(self, production_container) -> None:
        names = set(production_container.get_all_service_names())
        for required in ("database", "config", "cache", "hardware", "log", "event_bus"):
            assert required in names
        db = production_container.get("database")
        ok, missing = db.verify_product_tables()
        assert ok is True
        assert missing == []

    def test_base_module_response(self, production_container) -> None:
        from core.service_container import BaseModule

        module = BaseModule(production_container, "test_module")
        response = module.make_response(True, {"x": 1})
        assert response["success"] is True
        assert response["module"] == "test_module"
        assert response["data"]["x"] == 1
        assert "timestamp" in response


class TestHardwareService:
    """Tests for hardware abstraction."""

    def test_platform_info(self) -> None:
        from core.hardware_service import HardwareService

        hw = HardwareService()
        info = hw.get_platform_info()
        assert "system" in info
        assert "python" in info

    def test_process_rss_non_negative(self) -> None:
        from core.hardware_service import HardwareService

        hw = HardwareService()
        rss = hw.get_process_rss_mb()
        assert rss >= 0.0


class TestCacheWindowsFilenames:
    """Windows-incompatible characters must not appear in cache file paths."""

    def test_sanitize_replaces_colons(self) -> None:
        from core.cache_service import CacheService

        safe = CacheService.sanitize_filename("kw:p:s:4834fbaf621575b4")
        assert ":" not in safe
        assert safe == "kw_p_s_4834fbaf621575b4"

    def test_sanitize_strips_all_forbidden(self) -> None:
        from core.cache_service import CacheService

        raw = 'a<>:"/\\|?*b'
        safe = CacheService.sanitize_filename(raw)
        for ch in '<>:"/\\|?*':
            assert ch not in safe
        assert safe.startswith("a")
        assert safe.endswith("b")

    def test_set_json_with_colon_key(self, tmp_path: Path) -> None:
        from core.cache_service import CacheService

        cache = CacheService(cache_folder=tmp_path / "cache", max_size_mb=16)
        key = "kw:project-id:scene-id:deadbeefcafebabe"
        assert cache.set_json(key, {"primary_mood": "dramatic"}) is True
        # On-disk path must not contain colons
        path = cache._entry_path(key, suffix=".json")
        assert ":" not in str(path.name)
        assert path.exists()
        # Logical key still retrieves the value
        assert cache.get_json(key) == {"primary_mood": "dramatic"}

    def test_keyword_analyzer_cache_key_on_disk(
        self, project_root: Path, tmp_path: Path
    ) -> None:
        """Regression: keyword analyzer keys use colons; must still cache on Windows."""
        from core.service_container import ServiceContainer
        from modules.keyword_analyzer import KeywordAnalyzer

        container = ServiceContainer.create_production_container(
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
        analyzer = KeywordAnalyzer(container)
        text = "A mysterious unexplained secret vanished without a trace."
        first = analyzer.analyze_scene_text(text)
        assert first["success"] is True
        key = analyzer._cache_key("p", "s", text)
        assert ":" in key  # logical key may still use colons
        assert analyzer.cache.set_json(key, first["data"]) is True
        cached = analyzer.cache.get_json(key)
        assert cached is not None
        assert cached["primary_mood"] == first["data"]["primary_mood"]
        # Ensure actual file was written without colons in the name
        disk_path = analyzer.cache._entry_path(key, suffix=".json")
        assert ":" not in disk_path.name
        assert disk_path.exists()
