"""Headless tests for the UI chrome view-model (full-UI Batch 1).

Menus, toolbar, status bar, splash, shortcuts and theme switching are
painted by ui/app.py from these models — so every pixel-relevant
decision is pinned here without importing PyQt6.
"""

from __future__ import annotations

from core.service_container import ServiceContainer
from ui.viewmodel import (
    ACTION_DEFS,
    DEFAULT_SHORTCUTS,
    MENU_LAYOUT,
    UiViewModel,
)


def _vm(**ctx) -> UiViewModel:
    return UiViewModel(ctx)


def _vm_with_config(get_value=None) -> UiViewModel:
    container = ServiceContainer.create_test_container()
    config = container.get("config")
    config.get_config.return_value = {}
    config.get.return_value = get_value
    return UiViewModel({"container": container})


# ------------------------------------------------------------------
# Shortcuts (config file wins; defaults fill — RULE 7)
# ------------------------------------------------------------------
def test_shortcuts_fallback_without_container() -> None:
    keys = _vm().shortcuts_map()
    assert keys == DEFAULT_SHORTCUTS
    # spec §5 pins: Start Render = F9, Quick Preview = F5
    assert keys["start_render"] == "F9"
    assert keys["quick_preview"] == "F5"
    assert _vm().shortcut_for("quit") == "Ctrl+Q"
    assert _vm().shortcut_for("nope") == ""


def test_shortcuts_merge_config_overrides() -> None:
    container = ServiceContainer.create_test_container()
    config = container.get("config")
    config.get_config.return_value = {
        "shortcuts": {"start_render": "F5", "custom_extra": "Ctrl+9"}
    }
    vm = UiViewModel({"container": container})
    keys = vm.shortcuts_map()
    assert keys["start_render"] == "F5"  # config wins
    assert keys["quit"] == "Ctrl+Q"  # default survives
    assert keys["custom_extra"] == "Ctrl+9"  # unknown actions pass through


# ------------------------------------------------------------------
# Menu + toolbar models come from ONE table (no drift possible)
# ------------------------------------------------------------------
def test_menu_model_layout_and_separators() -> None:
    model = _vm().menu_model()
    # ui_specification.txt Section 5 — full menu bar, in spec order:
    assert [m["menu"] for m in model] == [
        "file", "edit", "view", "project", "render", "tools", "help",
    ]
    assert model[0]["title"] == "&File"
    file_ids = [
        i.get("id", "|") for i in model[0]["items"]
    ]
    assert file_ids[0] == "new_project"
    assert file_ids[-1] == "quit"
    assert "|" in file_ids  # separators flow through
    assert "import_zip" in file_ids and "backup_project" in file_ids
    edit = next(m for m in model if m["menu"] == "edit")
    edit_ids = [i.get("id") for i in edit["items"]]
    assert edit_ids[:2] == ["undo", "redo"]
    for scene_op in ("copy_scene", "paste_scene", "delete_scene"):
        assert scene_op in edit_ids
    view = next(m for m in model if m["menu"] == "view")
    view_ids = [i.get("id") for i in view["items"]]
    for theme_action in (
        "theme_dark", "theme_light", "theme_amoled",
        "theme_high_contrast",
    ):
        assert theme_action in view_ids  # spec §5: four themes
    assert "toggle_progress_panel" in view_ids  # panel toggles
    project = next(m for m in model if m["menu"] == "project")
    project_ids = [i.get("id") for i in project["items"]]
    assert project_ids == [
        "open_settings", "channel_profiles", "quality_check", None,
        "pre_render_report",
    ]
    render = next(m for m in model if m["menu"] == "render")
    render_ids = [i.get("id") for i in render["items"]]
    assert render_ids[:2] == ["start_render", "quick_preview"]
    assert "resume_render" in render_ids
    assert "batch_render" in render_ids
    tools = next(m for m in model if m["menu"] == "tools")
    tools_ids = [i.get("id") for i in tools["items"]]
    assert tools_ids[:5] == [
        "voice_store", "voice_clone", "engine_manager", "setup_wizard",
        "key_generator",
    ]  # wizard re-runnable from Tools (deep-dive fix #12)
    help_menu = next(m for m in model if m["menu"] == "help")
    help_ids = [i.get("id") for i in help_menu["items"]]
    assert help_ids[0] == "user_guide"
    assert "shortcuts" in help_ids and "about" in help_ids


def test_menu_model_shortcuts_and_enablement() -> None:
    model = _vm().menu_model()
    render = next(m for m in model if m["menu"] == "render")
    start = next(i for i in render["items"] if i.get("id") == "start_render")
    assert start["shortcut"] == "F9"  # spec §5
    assert start["enabled"] is True
    pause = next(i for i in render["items"] if i.get("id") == "pause_render")
    assert pause["enabled"] is False  # honest: engine v1 cannot pause
    assert "not supported" in pause["reason"]
    # every non-theme action id in the layout exists in ACTION_DEFS
    defined = {a["id"] for a in ACTION_DEFS}
    for _menu, _title, order in MENU_LAYOUT:
        for action_id in order:
            if action_id != "|":
                assert action_id in defined


def test_toolbar_is_a_strict_subset_of_menu_actions() -> None:
    vm = _vm()
    toolbar = vm.toolbar_model()
    assert [t["id"] for t in toolbar] == [
        "new_project", "import_files", "start_render",
        "cancel_render", "pause_render", "batch_render",
        "toggle_preview", "open_settings", "user_guide",
    ]  # complete toolbar (deep-dive fix #4)
    menu_ids = {
        i.get("id")
        for m in vm.menu_model()
        for i in m["items"]
        if not i.get("separator")
    }
    assert {t["id"] for t in toolbar} <= menu_ids
    assert all("&" not in t["text"] for t in toolbar)  # plain labels


# ------------------------------------------------------------------
# Status bar / splash / theme
# ------------------------------------------------------------------
def test_status_bar_model_fields_and_trial_days() -> None:
    vm = _vm(
        license_data={"status": {"status": "trial", "days_remaining": 7}}
    )
    model = vm.status_bar_model()
    assert model["license"] == "License: trial (7d)"
    assert model["modules"].startswith("Modules:")
    assert model["plugins"].startswith("Plugins:")
    assert set(model) == {"license", "modules", "plugins"}


def test_splash_model_brand_and_steps() -> None:
    model = _vm().splash_model()
    assert model["title"] == "AUTOPILOT"
    assert model["subtitle"]
    assert len(model["steps"]) >= 5
    assert model["steps"][0] == "Loading configuration"
    assert model["steps"][-1] == "Preparing window"


def test_theme_names_and_current_default() -> None:
    vm = _vm()
    # spec §5: Dark / Light / AMOLED / High Contrast
    assert set(vm.theme_names()) == {
        "dark", "light", "amoled", "high_contrast",
    }
    assert vm.current_theme() == "dark"  # no container -> default


def test_set_theme_persists_and_rejects_unknown() -> None:
    container = ServiceContainer.create_test_container()
    config = container.get("config")
    config.get.return_value = "dark"
    vm = UiViewModel({"container": container})
    ok, message = vm.set_theme("light")
    assert ok is True and "light" in message
    config.set.assert_called_with("theme", "light")
    ok, message = vm.set_theme("neon")
    assert ok is False and "Unknown theme" in message
    config.get.return_value = "light"
    assert vm.current_theme() == "light"
    config.get.return_value = "neon"  # corrupt persisted value
    assert vm.current_theme() == "dark"  # falls back honestly


# ------------------------------------------------------------------
# Static texts
# ------------------------------------------------------------------
def test_about_and_shortcuts_texts() -> None:
    vm = _vm()
    about = vm.about_text()
    assert "offline documentary" in about
    assert "FFmpeg:" in about and "Plugins loaded:" in about
    text = vm.shortcuts_text()
    assert "F9" in text and "Start Render" in text
    # disabled pause action still listed, with its honest reason
    assert "Ctrl+P" in text and "not supported" in text
