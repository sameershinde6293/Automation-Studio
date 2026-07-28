"""Unit tests for main.py (D.2 entry point + CLI).

Boot helpers, argument plumbing, per-command exit codes, and fault
isolation. CoreEngine/LicenseManager are faked through main.boot for
CLI tests; one real-boot test verifies first-launch wiring against the
repo's real config + schema.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as app_main


# ------------------------------------------------------------------
# Boot helpers
# ------------------------------------------------------------------
def test_find_project_root_dev() -> None:
    assert app_main.find_project_root() == Path(app_main.__file__).resolve().parent


def test_find_project_root_frozen(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_exe = tmp_path / "dist" / "Autopilot.exe"
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert app_main.find_project_root() == fake_exe.parent


def test_find_project_root_frozen_onedir_internal(
    monkeypatch, tmp_path: Path
) -> None:
    """PyInstaller 6 onedir: bundled datas live under _internal/.

    D.5 frozen smoke found boot failing without this branch — the
    root must be _internal so config/schema.sql resolve, UNLESS the
    user drops an override config/ next to the exe (that wins).
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_exe = tmp_path / "Autopilot" / "Autopilot.exe"
    internal = fake_exe.parent / "_internal"
    (internal / "config").mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert app_main.find_project_root() == internal
    # user override beside the exe takes precedence
    (fake_exe.parent / "config").mkdir()
    assert app_main.find_project_root() == fake_exe.parent


def test_load_app_settings_real(project_root: Path) -> None:
    settings = app_main.load_app_settings(project_root)
    assert settings["database_path"].endswith("autopilot.db")
    assert settings["thumbnail_count"] == 5


def test_load_app_settings_missing(tmp_path: Path) -> None:
    assert app_main.load_app_settings(tmp_path) == {}  # RULE 7 graceful


def test_build_container_creates_folders(
    project_root: Path, tmp_path: Path
) -> None:
    # Bootstrap into a scratch root that reuses the real config + schema.
    (tmp_path / "config").mkdir()
    (tmp_path / "database").mkdir()
    import shutil

    shutil.copy(project_root / "database" / "schema.sql",
                tmp_path / "database" / "schema.sql")
    shutil.copy(project_root / "config" / "app_settings.json",
                tmp_path / "config" / "app_settings.json")
    (tmp_path / "config" / "modules_config.json").write_text(
        '{"modules": []}', encoding="utf-8"
    )
    container = app_main.build_container(tmp_path)
    assert (tmp_path / "temp").is_dir()
    assert (tmp_path / "projects").is_dir()
    assert (tmp_path / "cache").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert container.get("database") is not None


def test_boot_real_first_launch(project_root: Path, tmp_path: Path) -> None:
    import shutil

    shutil.copytree(project_root / "config", tmp_path / "config")
    (tmp_path / "database").mkdir()
    shutil.copy(project_root / "database" / "schema.sql",
                tmp_path / "database" / "schema.sql")
    ctx = app_main.boot(tmp_path)
    assert ctx["engine"].module("file_parser") is not None
    hwid = (ctx["license_data"] or {}).get("hwid")
    assert hwid and "-" in hwid  # six 4-char dash groups
    status = (ctx["license_data"] or {}).get("status") or {}
    assert status.get("status") in ("trial", "active")


# ------------------------------------------------------------------
# CLI plumbing (boot faked)
# ------------------------------------------------------------------
class _FakeEngine:
    def __init__(self) -> None:
        self.calls = []
        self._modules = {}

    def run_script_pipeline(self, **kwargs):
        self.calls.append(("run_script_pipeline", kwargs))
        return {
            "success": True,
            "data": {"stages": [], "output_file_path": "/out/video.mp4",
                     "project_id": "p1"},
            "error": None,
            "warnings": [],
        }

    def run_project_pipeline(self, project_id, **kwargs):
        self.calls.append(("run_project_pipeline", project_id, kwargs))
        return {"success": True, "data": {"stages": [],
                "output_file_path": "/out/v.mp4"},
                "error": None, "warnings": []}

    def module(self, name):
        return self._modules.get(name)

    def get_module_status(self):
        return {
            "success": True,
            "data": {"report": {
                "file_parser": {"loaded": True, "enabled": True,
                                "required": True, "priority": 1,
                                "error": None},
            }},
            "error": None,
            "warnings": [],
        }

    def make_batch_processor(self):
        return lambda item: {"success": True, "data": {}, "error": None}

    def get_plugin_status(self):
        return {
            "success": True,
            "data": {"report": {
                "hello": {"loaded": True, "enabled": True,
                          "error": None},
            }},
            "error": None,
            "warnings": [],
        }

    def run_plugin(self, name, context=None):
        self.calls.append(("run_plugin", name, context))
        if name == "hello":
            return {"success": True, "data": {"echo": context},
                    "error": None, "warnings": []}
        return {"success": False, "data": {},
                "error": f"plugin not loaded: {name}", "warnings": []}


class _FakeLicense:
    def __init__(self, status: str = "trial") -> None:
        self._status = status
        self.activated_with = None

    def initialize_license(self):
        return {"success": True,
                "data": {"hwid": "AAAA-BBBB", "status": {"status": self._status}},
                "error": None, "warnings": []}

    def check_license(self):
        return {"success": True,
                "data": {"status": self._status, "days_remaining": 30,
                         "clock_tampered": False},
                "error": None, "warnings": []}

    def activate_license(self, key):
        self.activated_with = key
        ok = key.startswith("GOOD")
        return {"success": ok, "data": {}, "warnings": [],
                "error": None if ok else "bad key"}


def _fake_ctx(monkeypatch, license_status: str = "trial") -> dict:
    engine = _FakeEngine()
    ctx = {
        "engine": engine,
        "license": _FakeLicense(license_status),
        "license_data": {"hwid": "AAAA-BBBB",
                         "status": {"status": license_status}},
    }
    monkeypatch.setattr(app_main, "boot", lambda root: ctx)
    return ctx


def test_main_no_args_prints_help(monkeypatch, capsys) -> None:
    _fake_ctx(monkeypatch)
    assert app_main.main([]) == app_main.EXIT_OK
    assert "render" in capsys.readouterr().out


def test_main_bad_args_usage_error(monkeypatch) -> None:
    _fake_ctx(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        app_main.main(["bogus-command"])
    assert exc.value.code == app_main.EXIT_USAGE


def test_render_happy_path(monkeypatch, tmp_path: Path, capsys) -> None:
    ctx = _fake_ctx(monkeypatch)
    script = tmp_path / "demo.txt"
    script.write_text("//TITLE: Demo", encoding="utf-8")
    code = app_main.main(
        [
            "render", "--script", str(script), "--title", "My Doc",
            "--preset", "youtube_1080p", "--profile", "default",
            "--quality-gate", "--skip-license",
            "--skip-stage", "thumbnails",
        ]
    )
    assert code == app_main.EXIT_OK
    call = ctx["engine"].calls[0]
    assert call[0] == "run_script_pipeline"
    kwargs = call[1]
    assert kwargs["title"] == "My Doc"
    assert kwargs["export_preset"] == "youtube_1080p"
    assert kwargs["channel_profile_id"] == "default"
    assert kwargs["quality_gate"] is True
    assert kwargs["enforce_license"] is False
    assert kwargs["skip_stages"] == ("thumbnails",)
    assert "Output: /out/video.mp4" in capsys.readouterr().out


def test_render_missing_script_fails(monkeypatch, tmp_path: Path) -> None:
    _fake_ctx(monkeypatch)
    code = app_main.main(["render", "--script", str(tmp_path / "nope.txt")])
    assert code == app_main.EXIT_FAIL


def test_render_pipeline_failure_exit_1(monkeypatch, tmp_path: Path) -> None:
    ctx = _fake_ctx(monkeypatch)

    def _fail(**kwargs):
        return {"success": False, "data": {"stages": []},
                "error": "boom", "warnings": []}

    ctx["engine"].run_script_pipeline = _fail
    script = tmp_path / "demo.txt"
    script.write_text("x", encoding="utf-8")
    assert app_main.main(["render", "--script", str(script)]) == 1


def test_render_project_command(monkeypatch) -> None:
    ctx = _fake_ctx(monkeypatch)
    code = app_main.main(["render-project", "proj-42"])
    assert code == app_main.EXIT_OK
    call = ctx["engine"].calls[0]
    assert call[0] == "run_project_pipeline"
    assert call[1] == "proj-42"


def test_check_command_ready_vs_not_ready(monkeypatch, capsys) -> None:
    ctx = _fake_ctx(monkeypatch)

    class _Checker:
        def __init__(self, ready):
            self._ready = ready

        def run_full_check(self, pid):
            return {"success": True, "warnings": [],
                    "data": {"is_render_ready": self._ready}, "error": None}

        def generate_report(self, data):
            return "REPORT TEXT"

    ctx["engine"]._modules["quality_checker"] = _Checker(True)
    assert app_main.main(["check", "p1"]) == app_main.EXIT_OK
    assert "REPORT TEXT" in capsys.readouterr().out
    ctx["engine"]._modules["quality_checker"] = _Checker(False)
    assert app_main.main(["check", "p1"]) == app_main.EXIT_FAIL


def test_batch_command(monkeypatch) -> None:
    ctx = _fake_ctx(monkeypatch)
    seen = {}

    class _Batch:
        def process_queue(self, processor=None):
            seen["processor"] = processor
            return {"success": True, "warnings": [], "error": None,
                    "data": {"processed": 2, "completed": 2, "failed": 0,
                             "stopped_early": False}}

    ctx["engine"]._modules["batch_engine"] = _Batch()
    assert app_main.main(["batch"]) == app_main.EXIT_OK
    assert callable(seen["processor"])  # core wiring seam passed through


def test_batch_stopped_after_failure_exit_1(monkeypatch) -> None:
    ctx = _fake_ctx(monkeypatch)

    class _Batch:
        def process_queue(self, processor=None):
            return {"success": True, "warnings": [], "error": None,
                    "data": {"processed": 1, "completed": 0, "failed": 1,
                             "stopped_early": True}}

    ctx["engine"]._modules["batch_engine"] = _Batch()
    assert app_main.main(["batch"]) == app_main.EXIT_FAIL


def test_batch_add_command(monkeypatch) -> None:
    ctx = _fake_ctx(monkeypatch)
    seen = {}

    class _Batch:
        def add_to_queue(self, **kwargs):
            seen.update(kwargs)
            return {"success": True, "data": {"id": "q1"},
                    "error": None, "warnings": []}

    ctx["engine"]._modules["batch_engine"] = _Batch()
    code = app_main.main(["batch-add", "--project", "p1", "--priority", "2"])
    assert code == app_main.EXIT_OK
    assert seen["project_id"] == "p1"
    assert seen["priority"] == 2


def test_modules_command(monkeypatch, capsys) -> None:
    _fake_ctx(monkeypatch)
    assert app_main.main(["modules"]) == app_main.EXIT_OK
    out = capsys.readouterr().out
    assert "file_parser" in out
    assert "required" in out
    assert "hello" in out  # D.8 plugin section


def test_plugin_command_list(monkeypatch, capsys) -> None:
    _fake_ctx(monkeypatch)
    assert app_main.main(["plugin", "--list"]) == app_main.EXIT_OK
    out = capsys.readouterr().out
    assert "hello" in out and "[ok]" in out
    _fake_ctx(monkeypatch)
    assert app_main.main(["plugin"]) == app_main.EXIT_OK  # name omitted


def test_plugin_command_run_with_args(monkeypatch, capsys) -> None:
    ctx = _fake_ctx(monkeypatch)
    code = app_main.main(
        ["plugin", "hello", "--arg", "source=test", "--arg", "x=1"]
    )
    assert code == app_main.EXIT_OK
    out = capsys.readouterr().out
    assert "source" in out and "test" in out
    ran = [c for c in ctx["engine"].calls if c[0] == "run_plugin"]
    assert ran[0][1] == "hello"
    assert ran[0][2] == {"source": "test", "x": "1"}


def test_plugin_command_paths(monkeypatch, capsys) -> None:
    _fake_ctx(monkeypatch)
    assert app_main.main(["plugin", "hello", "--arg", "bad"]) == 2
    capsys.readouterr()
    _fake_ctx(monkeypatch)
    assert app_main.main(["plugin", "ghost"]) == app_main.EXIT_FAIL
    assert "plugin not loaded" in capsys.readouterr().out


def test_license_command_status_exit(monkeypatch, capsys) -> None:
    _fake_ctx(monkeypatch, license_status="trial")
    assert app_main.main(["license"]) == app_main.EXIT_OK
    assert "trial" in capsys.readouterr().out
    _fake_ctx(monkeypatch, license_status="expired")
    assert app_main.main(["license"]) == app_main.EXIT_FAIL


def test_license_activation_paths(monkeypatch) -> None:
    ctx = _fake_ctx(monkeypatch)
    assert app_main.main(["license", "--activate", "GOOD-KEY-1"]) == 0
    assert ctx["license"].activated_with == "GOOD-KEY-1"
    assert app_main.main(["license", "--activate", "BAD-KEY"]) == 1


def test_ui_command_degrades_gracefully(monkeypatch, capsys) -> None:
    """No PyQt6 on the machine: friendly hint + EXIT_OK (D.4)."""
    _fake_ctx(monkeypatch)
    monkeypatch.setitem(sys.modules, "ui.app", None)  # import fails
    assert app_main.main(["ui"]) == app_main.EXIT_OK
    assert "requirements_ui.txt" in capsys.readouterr().out


def test_ui_command_dispatches_to_launch(monkeypatch) -> None:
    """With ui.app importable, cmd_ui returns launch(ctx)'s code."""
    ctx = _fake_ctx(monkeypatch)
    calls = []

    def _fake_launch(received_ctx):
        calls.append(received_ctx)
        return 0

    monkeypatch.setitem(
        sys.modules, "ui.app",
        SimpleNamespace(launch=_fake_launch),
    )
    assert app_main.main(["ui"]) == app_main.EXIT_OK
    assert calls == [ctx]


def test_handler_exception_isolated(monkeypatch, capsys) -> None:
    ctx = _fake_ctx(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("unhandled")

    ctx["engine"].run_project_pipeline = _boom
    assert app_main.main(["render-project", "p1"]) == app_main.EXIT_FAIL
    assert "unexpected error" in capsys.readouterr().out
