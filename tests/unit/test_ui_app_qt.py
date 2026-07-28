"""Qt smoke tests for ui.app (skipped where PyQt6 is unavailable).

These run on any machine with PyQt6 installed (the user's Windows
checkpoint); in the sandbox they skip cleanly. The offscreen platform
plugin keeps them headless — no display is ever opened.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.core_engine import CoreEngine  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from ui.app import NAV_PAGES, MainWindow  # noqa: E402
from ui.theme import apply_theme  # noqa: E402
from ui.viewmodel import UiViewModel  # noqa: E402
from ui.dialogs.app_dialogs import (  # noqa: E402
    PreRenderReportDialog,
    RenderCompleteDialog,
)


class _StubEngine:
    def __init__(self) -> None:
        self.event_bus = EventBus()
        self.runs = []
        self.cancelled = 0

    def stage_names(self):
        return CoreEngine.stage_names()

    def get_module_status(self):
        return {"success": True, "data": {"loaded_modules": ["m1"]}}

    def cancel_pipeline(self):
        self.cancelled += 1
        return {"success": True}

    def run_script_pipeline(self, **kwargs):
        self.runs.append(kwargs)
        return {"success": True, "data": {"output_file_path": "/out/v.mp4"}}


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _window(tmp_path: Path, engine=None, container=None) -> MainWindow:
    ctx = {
        "engine": engine if engine is not None else _StubEngine(),
        "license_data": {"status": {"status": "trial"}},
        "container": container,
    }
    return MainWindow(UiViewModel(ctx))


def _real_container(project_root: Path, tmp_path: Path):
    from core.service_container import ServiceContainer

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


def test_window_constructs_with_stage_rows(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    try:
        assert "Autopilot" in window.windowTitle()
        assert window.stage_list.count() == 18  # stage plan mirrored (D.7)
        message = window.statusBar().currentMessage().lower()
        assert "trial" in message
    finally:
        window.close()
        window.deleteLater()


def test_render_click_with_invalid_inputs_shows_error(
    qapp, tmp_path: Path
) -> None:
    window = _window(tmp_path)
    try:
        window.render_button.click()  # nothing filled in
        assert window.render_button.isEnabled()  # no worker started
        text = window.log_pane.toPlainText()
        assert "Choose an existing script" in text
        assert window._worker is None
    finally:
        window.close()
        window.deleteLater()


def test_render_happy_path_round_trips_through_worker(
    qapp, tmp_path: Path
) -> None:
    engine = _StubEngine()
    window = _window(tmp_path, engine=engine)
    try:
        script = tmp_path / "s.txt"
        script.write_text("//TITLE: Demo", encoding="utf-8")
        images = tmp_path / "imgs"
        images.mkdir()
        window.script_edit.setText(str(script))
        window.images_edit.setText(str(images))
        window.project_edit.setText(str(tmp_path / "proj"))
        window.render_button.click()
        assert window.render_button.isEnabled() is False  # worker runs
        assert window._worker is not None
        assert window._worker.wait(5000)
        QApplication.processEvents()
        assert window.render_button.isEnabled()
        assert "SUCCESS" in window.log_pane.toPlainText()
        assert engine.runs and engine.runs[0]["title"] == "s"
        # bus events flow through the bridge onto the stage list
        engine.event_bus.publish("pipeline.stage_started", {"stage": "tts"})
        engine.event_bus.publish("pipeline.stage_completed", {"stage": "tts"})
        engine.event_bus.publish(
            "pipeline.render_progress", {"progress": 55.0, "fps": 30.0}
        )
        QApplication.processEvents()
        row = window._stage_rows["tts"].text()
        assert "✓" in row and "tts" in row
        assert window.progress_bar.value() == 550
    finally:
        window.close()
        window.deleteLater()


def test_nav_switches_pages(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    try:
        # v3.0 pages: Render/Studio/Scenes/Transitions/Grade/Audio/
        # Subtitles/Voice/Voice Store/Export/Batch/Projects/Settings
        assert window.nav_list.count() == len(NAV_PAGES) == 13
        assert window.pages.currentIndex() == 0  # Render page first
        window.nav_list.setCurrentRow(1)
        assert window.pages.currentIndex() == 1  # Studio
        window.nav_list.setCurrentRow(2)
        assert window.pages.currentIndex() == 2  # Scenes
        window.nav_list.setCurrentRow(3)
        assert window.pages.currentIndex() == 3  # Transitions
        window.nav_list.setCurrentRow(10)
        assert window.pages.currentIndex() == 10  # Batch
        window.nav_list.setCurrentRow(11)
        assert window.pages.currentIndex() == 11  # Projects
        window.nav_list.setCurrentRow(12)
        assert window.pages.currentIndex() == 12  # Settings
    finally:
        window.close()
        window.deleteLater()


def test_render_form_combos_are_data_driven(
    qapp, project_root: Path, tmp_path: Path
) -> None:
    # real container -> presets/profiles come from real config + DB
    window = _window(tmp_path, container=_real_container(project_root, tmp_path))
    try:
        # '(default preset)' + exported presets from real config
        assert window.preset_combo.count() >= 2
        assert window.preset_combo.itemData(0) is None
        ids = [
            window.preset_combo.itemData(i)
            for i in range(1, window.preset_combo.count())
        ]
        assert "youtube_1080p" in ids
        # channel profile combo exists and defaults to None payload
        assert window.profile_combo.itemData(0) is None
    finally:
        window.close()
        window.deleteLater()


def test_dark_theme_applies_app_wide(qapp) -> None:
    apply_theme(qapp)
    sheet = qapp.styleSheet()
    assert "QProgressBar::chunk" in sheet
    assert "QListWidget#navList" in sheet
    assert sheet  # non-empty QSS after apply


def test_toolbar_menus_statusbar_chrome(qapp, tmp_path: Path) -> None:
    from PyQt6.QtWidgets import QToolBar

    window = _window(tmp_path)
    toolbars = window.findChildren(QToolBar)
    assert len(toolbars) == 1
    labels = [a.text() for a in toolbars[0].actions() if a.text()]
    assert labels[:9] == [
        "🆕  New Project…", "📥  Import Files…", "▶  Start Render",
        "✖  Cancel Render", "⏸  Pause Render", "▦  Batch Queue",
        "👁  Toggle Preview", "⚙  Project Settings", "📖  User Guide",
    ]  # icons + widget extras (spacer/profile/license) follow
    pause = window._actions["pause_render"]
    assert not pause.isEnabled()  # honest: engine v1 cannot pause
    assert "pause is not supported" in pause.toolTip()
    titles = [a.text() for a in window.menuBar().actions()]
    assert titles == [
        "&File", "&Edit", "&View", "&Project", "&Render", "&Tools", "&Help",
    ]  # ui_specification.txt Section 5 menu bar
    quit_action = window._actions["quit"]
    assert quit_action.shortcut().toString() == "Ctrl+Q"
    assert window._actions["start_render"].shortcut().toString() == "F9"
    assert window._actions["pause_render"].isEnabled() is False
    for action_id in (
        "import_zip", "backup_project", "undo", "redo", "select_all",
        "copy_scene", "paste_scene", "delete_scene", "theme_amoled",
        "theme_high_contrast", "toggle_toolbar", "toggle_statusbar",
        "toggle_progress_panel", "channel_profiles", "quality_check",
        "pre_render_report", "quick_preview", "resume_render",
        "batch_render", "render_settings", "voice_store", "voice_clone",
        "engine_manager", "key_generator", "user_guide",
    ):
        assert action_id in window._actions  # every menu item wired
    assert window.sb_license.text().startswith("License:")
    assert window.sb_modules.text() == "Modules: 1"


def test_theme_switch_action_reapplies(qapp, tmp_path: Path) -> None:
    from ui import theme

    window = _window(tmp_path)
    window._dispatch_action("theme_light")
    assert QApplication.instance().styleSheet() == theme.LIGHT_QSS
    assert window._actions["theme_light"].isChecked()
    assert not window._actions["theme_dark"].isChecked()
    window._dispatch_action("theme_dark")
    assert QApplication.instance().styleSheet() == theme.DARK_QSS
    assert window._actions["theme_dark"].isChecked()


def test_splash_constructs_offscreen(qapp) -> None:
    from ui.app import AutopilotSplash

    splash = AutopilotSplash(UiViewModel({}).splash_model())
    assert not splash.pixmap().isNull()
    splash.show_step(2)
    splash.close()


def test_studio_page_panels_present(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    try:
        window.nav_list.setCurrentRow(1)  # Studio
        assert window.pages.currentIndex() == 1
        # three separate drop zones, formats visible (JSON included)
        assert len(window.import_panel.drop_zones) == 3
        assert all(z.acceptDrops() for z in window.import_panel.drop_zones)
        assert window.import_panel.drop_zone is window.import_panel.drop_zones[0]
        texts = [z.text() for z in window.import_panel.drop_zones]
        assert any("JSON" in t for t in texts)  # script zone shows JSON
        assert any("DOCX" in t and "PDF" in t for t in texts)
        assert any("MP3" in t and "WAV" in t for t in texts)
        assert any("JPG" in t and "PNG" in t for t in texts)
        assert "Drop files" in window.import_panel.summary_label.text()
        assert window.preview_panel.file_label.text() == "No media loaded."
        # visual timeline: card host renders (empty state without a DB)
        assert window.timeline_panel.cards_layout.count() >= 1
        assert window.timeline_panel.cards == []
    finally:
        window.close()
        window.deleteLater()


def test_studio_dispatch_actions(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    try:
        window._dispatch_action("open_settings")
        assert window.pages.currentIndex() == 12  # Settings page
        window._dispatch_action("toggle_preview")
        assert window.pages.currentIndex() == 1  # routed to Studio
        window._dispatch_action("batch_render")
        assert window.pages.currentIndex() == 10  # Batch page
        window._dispatch_action("voice_store")
        assert window.pages.currentIndex() == 8  # Voice Store page
        window._dispatch_action("render_settings")
        assert window.pages.currentIndex() == 0  # Render form
        window._dispatch_action("copy_frame")
        assert "frame" in window.statusBar().currentMessage().lower()
        window._dispatch_action("refresh_projects")
        assert "refreshed" in window.statusBar().currentMessage().lower()
        window._dispatch_action("no_such_action")  # honest fallback
        assert "unknown" in window.statusBar().currentMessage().lower()
    finally:
        window.close()
        window.deleteLater()


def test_new_project_dialog_flow(qapp, tmp_path: Path) -> None:
    from ui.dialogs.app_dialogs import NewProjectDialog

    vm = UiViewModel({})
    dialog = NewProjectDialog(vm)
    dialog.title_edit.setText("My Test Doc")
    assert dialog.folder_edit.text() == "projects/my-test-doc"
    folder = tmp_path / "np"
    dialog.folder_edit.setText(str(folder))
    dialog._accept()
    assert dialog.result() == NewProjectDialog.DialogCode.Accepted
    payload = dialog.result_payload
    assert payload["title"] == "My Test Doc"
    assert payload["project_folder"] == str(folder)
    assert folder.is_dir()
    bad = NewProjectDialog(UiViewModel({}))
    bad.title_edit.setText("")
    bad._accept()
    assert bad.result() != NewProjectDialog.DialogCode.Accepted
    assert "title" in bad.error_label.text()
    dialog.close()
    bad.close()


def test_render_complete_dialog_states(qapp, tmp_path: Path) -> None:
    from ui.dialogs.app_dialogs import RenderCompleteDialog

    output = tmp_path / "ok.mp4"
    output.write_bytes(b"X" * 2048)
    played = []
    dialog = RenderCompleteDialog(
        UiViewModel({}),
        {"data": {"output_file_path": str(output)}, "warnings": []},
        on_play=played.append,
    )
    assert dialog.play_button.isEnabled() is True
    assert dialog.upload_button.isEnabled() is False  # no engine here
    assert dialog.upload_button.toolTip()
    # no DB container -> chapters block honestly absent/disabled
    assert dialog.copy_chapters_button.isEnabled() is False
    assert dialog.model["thumbnail_path"] is None
    dialog._play()
    assert played == [str(output)]
    dialog.close()
    missing = RenderCompleteDialog(UiViewModel({}), {"data": {}})
    assert missing.play_button.isEnabled() is False
    missing.close()


def test_pre_render_dialog_construct_and_start(qapp, tmp_path: Path) -> None:
    from ui.dialogs.app_dialogs import PreRenderReportDialog

    script = tmp_path / "s.txt"
    script.write_text("hello brave world", encoding="utf-8")
    images = tmp_path / "imgs"
    images.mkdir()
    (images / "a.jpg").write_bytes(b"x")
    dialog = PreRenderReportDialog(
        UiViewModel({}),
        {
            "script_path": str(script),
            "images_folder": str(images),
            "project_folder": str(tmp_path / "proj"),
            "title": "Demo",
        },
    )
    assert dialog.start_button.isEnabled() is True  # inputs are valid
    assert dialog.rows_list.count() >= 6
    joined = "\n".join(
        dialog.rows_list.item(i).text()
        for i in range(dialog.rows_list.count())
    )
    assert "Script format" in joined and "FFmpeg" in joined
    assert "≈" in dialog.estimate_label.text()
    assert dialog.start_requested is False
    dialog._start()
    assert dialog.start_requested is True
    # bad inputs -> start disabled honestly
    blocked = PreRenderReportDialog(UiViewModel({}), {})
    assert blocked.start_button.isEnabled() is False
    assert blocked.start_button.toolTip()
    dialog.close()
    blocked.close()


def test_recovery_dialog_construct(qapp, tmp_path: Path) -> None:
    from ui.dialogs.app_dialogs import RecoveryDialog

    candidates = [
        {"project_id": "p1", "title": "Doc One", "stage": "rendering",
         "percent": 67.0, "error_count": 0, "updated_at": "2026-07-18"},
        {"project_id": "p2", "title": "Doc Two", "stage": "tts_generating",
         "percent": 12.0, "error_count": 2, "updated_at": "2026-07-17"},
    ]
    dialog = RecoveryDialog(UiViewModel({}), candidates)
    assert dialog.list.count() == 2
    assert "Doc One" in dialog.list.item(0).text()
    dialog.list.setCurrentRow(1)
    dialog._resume()
    assert dialog.resume_id == "p2"
    dialog.close()


def test_toolbar_profile_dropdown_and_license_status(
    qapp, tmp_path: Path
) -> None:
    """ui_specification toolbar: profile dropdown + license status."""
    window = _window(tmp_path)
    assert window.toolbar_profile.count() >= 1
    assert window.toolbar_profile.itemText(0) == "(channel default)"
    assert window.toolbar_profile.toolTip()
    assert window.toolbar_license.text().startswith("🔑")
    assert "trial" in window.toolbar_license.text()
    # v3.0 #16: workspace selector lives in the toolbar too
    assert window.workspace_combo.count() >= 8
    assert window.workspace_combo.toolTip()
    window.close()


def test_three_panel_layout_inspector_card(qapp, tmp_path: Path) -> None:
    """Left nav | center pages | right Inspector — always reachable."""
    window = _window(tmp_path)
    assert window.inspector.objectName() == "card"
    # v3.0 #15: the card sits inside a dock widget (movable/float)
    assert window.inspector_dock.widget() is window.inspector
    assert window.insp_details.text() == "Nothing selected."
    window._act_toggle_inspector()
    assert not window.inspector.isVisible()
    window._act_toggle_inspector()
    assert window.inspector.isVisible()
    window._refresh_inspector(None)  # RULE 7: stays quiet
    assert window.insp_details.text() == "Nothing selected."
    window.close()


def test_fullscreen_action_toggles_safely(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    window.show()
    window._act_toggle_fullscreen()
    window._act_toggle_fullscreen()
    assert not window.isFullScreen()  # back to normal, never stuck
    window.close()


def test_splash_paints_logo_and_progress_bar(qapp) -> None:
    from ui.app import AutopilotSplash

    vm = UiViewModel({})
    splash = AutopilotSplash(vm.splash_model())
    steps = len(vm.splash_model()["steps"])
    splash.show_step(0)
    assert splash._progress > 0
    splash.show_step(steps - 1)
    assert splash._progress == 1.0


def test_license_screen_dialog_shows_and_copies_hwid(qapp) -> None:
    from ui.dialogs.app_dialogs import LicenseScreenDialog

    class _Lic:
        def generate_hwid(self) -> str:
            return "HW-TEST-99"

    vm = UiViewModel({
        "license": _Lic(),
        "license_data": {"status": {"status": "unknown"}},
    })
    dialog = LicenseScreenDialog(vm)
    assert dialog.hwid_edit.text() == "HW-TEST-99"
    dialog._copy_hwid()
    assert QApplication.clipboard().text() == "HW-TEST-99"


def test_statusbar_has_machine_project_ffmpeg_fields(
    qapp, tmp_path: Path
) -> None:
    """Deep-dive fix #5: RAM/CPU, project, FFmpeg in the status bar."""
    window = _window(tmp_path)
    assert window.sb_project.text().startswith("Project:")
    assert window.sb_ffmpeg.text().startswith("FFmpeg:")
    assert "RAM" in window.sb_system.text()
    window._refresh_system_status()  # tick is crash-free
    window.close()


def test_inspector_shows_app_stats_when_nothing_selected(
    qapp, tmp_path: Path
) -> None:
    """Deep-dive fix #3: stats + quick actions, never a dead end."""
    window = _window(tmp_path)
    text = window.insp_details.text()
    assert "Autopilot 3.1.0" in text
    assert "License:" in text
    assert not window.insp_scene_bar.isVisible()
    window.close()


def test_menu_shortcuts_visible_on_scene_actions(
    qapp, tmp_path: Path
) -> None:
    window = _window(tmp_path)
    assert window._actions["copy_scene"].shortcut().toString() == "Ctrl+C"
    assert window._actions["user_guide"].shortcut().toString() == "F1"
    window.close()


def test_file_menu_export_submenu(qapp, tmp_path: Path) -> None:
    """Spec 35-39: File ▸ Export holds exactly the 5 job actions."""
    window = _window(tmp_path)
    file_action = next(
        a for a in window.menuBar().actions() if a.text() == "&File")
    menus = [a.menu() for a in file_action.menu().actions()
             if a.menu() is not None]
    titles = [m.title() for m in menus]
    assert "&Export" in titles
    sub = menus[titles.index("&Export")]
    assert len(sub.actions()) == 5
    window.close()


def test_export_job_dialog_fields_per_kind(qapp) -> None:
    from ui.dialogs.app_dialogs import ExportJobDialog

    vm = UiViewModel({})
    for kind, marker in (("export_audio_mix", "narration"),
                         ("export_audio_only", "script"),
                         ("burn_subtitles", "video"),
                         ("export_thumbnails", "project_id"),
                         ("export_storyboard_pdf", "out")):
        dialog = ExportJobDialog(vm, kind)
        assert marker in dialog._fields


# ---------------------------------------------------------------------------
# 3.0.4 expert-review round 4: revert buttons, match report section,
# expandable render warnings, waveform interaction.
# ---------------------------------------------------------------------------
def test_review_revert_buttons_on_panels(qapp, tmp_path: Path) -> None:
    from PyQt6.QtWidgets import QPushButton

    window = _window(tmp_path)
    buttons = window.findChildren(QPushButton, "revertBtn")
    assert len(buttons) >= 5
    assert "↺ Revert" in {button.text() for button in buttons}


def test_review_pre_render_dialog_match_section(
        qapp, tmp_path: Path) -> None:
    (tmp_path / "sunset.jpg").write_bytes(b"x")
    vm = UiViewModel({
        "engine": _StubEngine(),
        "license_data": {"status": {"status": "trial"}},
        "container": None,
    })
    dialog = PreRenderReportDialog(
        vm,
        {"script_path": "", "images_folder": str(tmp_path),
         "project_folder": str(tmp_path), "title": "demo"})
    has_section = (getattr(dialog, "match_list", None) is not None
                   or getattr(dialog, "match_note", None) is not None)
    assert has_section


def test_review_render_complete_warnings_expand(
        qapp, tmp_path: Path) -> None:
    vm = UiViewModel({
        "engine": _StubEngine(),
        "license_data": {"status": {"status": "trial"}},
        "container": None,
    })
    result = {"data": {"output_file_path": "", "warnings": [
        "Scene 12: image not found, using placeholder",
        "Scene 18: low-confidence image match"]}}
    dialog = RenderCompleteDialog(vm, result)
    assert dialog.warnings_list.count() == 2
    assert dialog.warnings_list.isHidden()
    assert dialog.warn_toggle.text().startswith("▸")
    dialog._toggle_warnings()
    assert not dialog.warnings_list.isHidden()
    assert dialog.warn_toggle.text().startswith("▾")
    dialog._toggle_warnings()
    assert dialog.warnings_list.isHidden()


def test_review_waveform_click_to_seek(qapp) -> None:
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtCore import Qt as _Qt
    from PyQt6.QtGui import QMouseEvent

    from ui.panels.waveform_widget import WaveformWidget

    widget = WaveformWidget()
    widget.resize(200, 120)
    assert widget._playhead < 0
    widget.set_peaks([0.2, 0.9, 0.5])
    widget.set_duration(90.0)
    heard = []
    widget.seekRequested.connect(heard.append)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(100.0, 60.0),
        _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
        _Qt.KeyboardModifier.NoModifier)
    widget.mousePressEvent(event)
    assert heard and abs(heard[0] - 45.0) < 0.5
    assert abs(widget._playhead - 0.5) < 0.01
    assert widget.seek_requested is widget.seekRequested
    widget.set_playhead(0.25)
    assert abs(widget._playhead - 0.25) < 0.01
    widget.set_playhead(9.9)  # out of range hides the playhead
    assert widget._playhead < 0


# ---------------------------------------------------------------------------
# Hotfix 3.0.5: inspector must exist before its first lines render
# (startup crashed with ''MainWindow'' object has no attribute inspector'').
# ---------------------------------------------------------------------------
def test_hotfix_inspector_created_at_startup(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    assert getattr(window, "inspector", None) is not None
    assert window.inspector_dock.widget() is window.inspector
    assert window.insp_details.text() != ""


# ---------------------------------------------------------------------------
# 3.0.6 round 5: layout readability — widths, word-wrap over elide.
# ---------------------------------------------------------------------------
def test_review_layout_readability_306(qapp, tmp_path: Path) -> None:
    window = _window(tmp_path)
    assert window.nav_list.minimumWidth() >= 180
    assert window.inspector.minimumWidth() >= 280
    assert window.pages.minimumWidth() >= 600
    text = window.insp_details.text()
    assert "…" not in text  # no elision anywhere in the Inspector
    assert "Documentary Studio" in text  # full app name, not cut
    assert "inspect its media" in text   # full hint sentence


# ---------------------------------------------------------------------------
# 3.0.7 round 6: slider value readability + CRF spinbox.
# ---------------------------------------------------------------------------
def test_review_slider_readability_307(qapp, tmp_path: Path) -> None:
    from PyQt6.QtWidgets import QLabel, QSpinBox

    window = _window(tmp_path)
    labels = window.findChildren(QLabel, "sliderValue")
    assert len(labels) >= 10  # audio, grade, voice, subtitle sliders
    assert all(lbl.minimumWidth() >= 50 for lbl in labels)
    spins = window.findChildren(QSpinBox)
    assert any(spin.minimum() == 0 and spin.maximum() == 51
               for spin in spins)  # CRF spinbox, full codec domain
