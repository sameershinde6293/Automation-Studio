"""Unit tests for the D.8 plugin interface (core_engine plugin seam).

Plugins are user-provided Python files loaded from plugins/ BY FILE
PATH (so they also work beside the frozen exe). The loader mirrors
module loading: every failure - missing file, syntax error, missing
BaseModule subclass, wrong PLUGIN_API - is recorded in the report
(RULE 7/8), never raised. run_plugin normalizes returns and isolates
crashes; plugin.* events ride the bus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from core.core_engine import PLUGIN_API_VERSION, CoreEngine
from core.service_container import ServiceContainer

VALID_PLUGIN = '''
from core.service_container import BaseModule

PLUGIN_API = 1


class WidgetPlugin(BaseModule):
    def is_optional_module(self):
        return True

    def run(self, context):
        return {"success": True, "data": {"echo": context.get("msg")}}
'''

CRASH_PLUGIN = '''
from core.service_container import BaseModule

PLUGIN_API = 1


class CrashPlugin(BaseModule):
    def run(self, context):
        raise ValueError("boom")
'''

NONDICT_PLUGIN = '''
from core.service_container import BaseModule

PLUGIN_API = 1


class PlainPlugin(BaseModule):
    def run(self, context):
        return "pong"
'''

EXTRAS_PLUGIN = '''
from core.service_container import BaseModule

PLUGIN_API = 1


class ExtrasPlugin(BaseModule):
    def run(self, context):
        return {"projects": 3, "warnings": ["heads up"]}
'''

FAIL_PLUGIN = '''
from core.service_container import BaseModule

PLUGIN_API = 1


class FailPlugin(BaseModule):
    def run(self, context):
        return {"success": False, "error": "nope"}
'''

NOCLASS_PLUGIN = "PLUGIN_API = 1\nVALUE = 42\n"
BAD_API_PLUGIN = VALID_PLUGIN.replace("PLUGIN_API = 1", "PLUGIN_API = 2")
SYNTAX_PLUGIN = "def broken(:\n"


def _write_plugin(root: Path, name: str, body: str) -> None:
    folder = root / "plugins"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(body.strip() + "\n",
                                       encoding="utf-8")


def _engine(
    project_root: Path,
    tmp_path: Path,
    plugins: List[Dict[str, Any]],
) -> CoreEngine:
    root = tmp_path / "proj"
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "plugins_config.json").write_text(
        json.dumps({"plugins": plugins}), encoding="utf-8"
    )
    container = ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(cfg),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=root,
    )
    return CoreEngine(container, auto_load=False)


# ------------------------------------------------------------------
# Loader report (RULE 7: honest entries, never raises)
# ------------------------------------------------------------------
def test_loads_enabled_plugin(project_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_plugin(root, "widget", VALID_PLUGIN)
    engine = _engine(project_root, tmp_path,
                     [{"name": "widget", "enabled": True}])
    reply = engine.load_plugins()
    assert reply["success"] is True
    assert reply["data"]["loaded"] == 1
    assert engine.plugin_names() == ["widget"]
    assert engine.plugin("widget") is not None
    assert engine.plugin("ghost") is None
    report = engine.get_plugin_status()["data"]["report"]
    assert report["widget"]["loaded"] is True
    assert report["widget"]["error"] is None


def test_disabled_entry_is_recorded_not_loaded(
    project_root: Path, tmp_path: Path
) -> None:
    _write_plugin(tmp_path / "proj", "widget", VALID_PLUGIN)
    engine = _engine(project_root, tmp_path,
                     [{"name": "widget", "enabled": False}])
    engine.load_plugins()
    report = engine.get_plugin_status()["data"]["report"]
    assert report["widget"]["loaded"] is False
    assert report["widget"]["error"] == "disabled in registry"
    assert engine.plugin_names() == []


def test_missing_file_is_an_honest_report_entry(
    project_root: Path, tmp_path: Path
) -> None:
    engine = _engine(project_root, tmp_path,
                     [{"name": "ghost", "enabled": True}])
    reply = engine.load_plugins()
    assert reply["success"] is True  # loading never raises
    assert reply["data"]["loaded"] == 0
    error = reply["data"]["plugins"]["ghost"]["error"]
    assert "plugin file not found" in error


def test_broken_plugins_are_isolated(
    project_root: Path, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    _write_plugin(root, "syntax", SYNTAX_PLUGIN)
    _write_plugin(root, "noclass", NOCLASS_PLUGIN)
    _write_plugin(root, "badapi", BAD_API_PLUGIN)
    _write_plugin(root, "widget", VALID_PLUGIN)
    engine = _engine(
        project_root,
        tmp_path,
        [{"name": n, "enabled": True}
         for n in ("syntax", "noclass", "badapi", "widget")],
    )
    reply = engine.load_plugins()
    plugins = reply["data"]["plugins"]
    assert plugins["syntax"]["loaded"] is False
    assert "BaseModule" in plugins["noclass"]["error"]
    assert "API 2" in plugins["badapi"]["error"]
    assert str(PLUGIN_API_VERSION) in plugins["badapi"]["error"]
    assert plugins["widget"]["loaded"] is True  # one bad != all bad
    assert reply["data"]["loaded"] == 1


def test_empty_registry_loads_nothing(
    project_root: Path, tmp_path: Path
) -> None:
    engine = _engine(project_root, tmp_path, [])
    reply = engine.load_plugins()
    assert reply["success"] is True
    assert reply["data"]["loaded"] == 0
    assert engine.get_plugin_status()["data"]["loaded_plugins"] == []


# ------------------------------------------------------------------
# run_plugin: normalization, isolation, events
# ------------------------------------------------------------------
def test_run_plugin_success_with_context(
    project_root: Path, tmp_path: Path
) -> None:
    _write_plugin(tmp_path / "proj", "widget", VALID_PLUGIN)
    engine = _engine(project_root, tmp_path,
                     [{"name": "widget", "enabled": True}])
    engine.load_plugins()
    reply = engine.run_plugin("widget", {"msg": "hi"})
    assert reply["success"] is True
    assert reply["data"]["echo"] == "hi"
    assert reply["module"] == "core_engine"
    assert reply["duration_ms"] >= 0


def test_run_plugin_normalizes_non_dict_result(
    project_root: Path, tmp_path: Path
) -> None:
    _write_plugin(tmp_path / "proj", "plain", NONDICT_PLUGIN)
    engine = _engine(project_root, tmp_path,
                     [{"name": "plain", "enabled": True}])
    engine.load_plugins()
    reply = engine.run_plugin("plain")
    assert reply["success"] is True
    assert reply["data"]["result"] == "pong"


def test_run_plugin_folds_extras_into_data(
    project_root: Path, tmp_path: Path
) -> None:
    _write_plugin(tmp_path / "proj", "extras", EXTRAS_PLUGIN)
    engine = _engine(project_root, tmp_path,
                     [{"name": "extras", "enabled": True}])
    engine.load_plugins()
    reply = engine.run_plugin("extras")
    assert reply["success"] is True
    assert reply["data"]["projects"] == 3
    assert reply["warnings"] == ["heads up"]


def test_run_plugin_crash_is_isolated_with_event(
    project_root: Path, tmp_path: Path
) -> None:
    _write_plugin(tmp_path / "proj", "crash", CRASH_PLUGIN)
    engine = _engine(project_root, tmp_path,
                     [{"name": "crash", "enabled": True}])
    engine.load_plugins()
    events: List[str] = []
    for name in ("plugin.started", "plugin.completed", "plugin.failed"):
        engine.event_bus.subscribe(
            name, lambda d, n=name: events.append(n)
        )
    reply = engine.run_plugin("crash", {})
    assert reply["success"] is False
    assert "plugin crash: boom" in reply["error"]
    assert "plugin.failed" in events
    assert "plugin.completed" not in events


def test_run_plugin_failure_response_and_events(
    project_root: Path, tmp_path: Path
) -> None:
    _write_plugin(tmp_path / "proj", "failer", FAIL_PLUGIN)
    _write_plugin(tmp_path / "proj", "widget", VALID_PLUGIN)
    engine = _engine(
        project_root,
        tmp_path,
        [{"name": "failer", "enabled": True},
         {"name": "widget", "enabled": True}],
    )
    engine.load_plugins()
    events: List[str] = []
    for name in ("plugin.started", "plugin.completed", "plugin.failed"):
        engine.event_bus.subscribe(
            name, lambda d, n=name: events.append(n)
        )
    reply = engine.run_plugin("failer")
    assert reply["success"] is False
    assert reply["error"] == "nope"
    ok = engine.run_plugin("widget", {"msg": "x"})
    assert ok["success"] is True
    assert events.count("plugin.failed") == 1
    assert events.count("plugin.completed") == 1
    assert events.count("plugin.started") == 2


def test_run_plugin_unknown_name(project_root: Path,
                                 tmp_path: Path) -> None:
    engine = _engine(project_root, tmp_path, [])
    reply = engine.run_plugin("ghost")
    assert reply["success"] is False
    assert "plugin not loaded: ghost" in reply["error"]


# ------------------------------------------------------------------
# The shipped example plugin works against the real repo config
# ------------------------------------------------------------------
def test_shipped_example_plugin_loads_and_runs(
    project_root: Path, tmp_path: Path
) -> None:
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
    engine = CoreEngine(container)  # real auto_load: modules + plugins
    assert "hello_autopilot" in engine.plugin_names()
    reply = engine.run_plugin("hello_autopilot", {"source": "test"})
    assert reply["success"] is True, reply.get("error")
    assert "hello_autopilot" in reply["data"]["message"]
    assert isinstance(reply["data"]["projects_in_db"], int)
    assert reply["data"]["context_keys"] == ["source"]
