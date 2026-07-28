"""PyQt6 main window for Autopilot (full UI — Batch 1 chrome).

Thin shell: decisions live in ui/viewmodel.py (Qt-free, unit-tested
headless); styling lives in ui/theme.py. This module imports PyQt6 at
the top level — without PyQt6 the import fails and ``main.py cmd_ui``
answers with the friendly CLI hint instead of a traceback.

Structure (Batch 1): branded splash at boot, QToolBar + full model-
driven menu bar (File/Edit/Render/View/Help incl. theme switcher),
status bar with permanent license/modules/plugins fields, keyboard
shortcuts from config/keyboard_shortcuts.json — all painted from
UiViewModel chrome models (headless-tested). Pages remain:
  Render    (script/images/project form + live pipeline monitor)
  Projects  (recent projects from the DB, open-folder reveal)
  Settings  (license status + activation, engines, Drive backup)
Every menu/toolbar/shortcut action routes through ONE dispatcher
(_dispatch_action), so Batch 2/3 panels only re-point handlers.

Threading: the pipeline runs on a QThread; event-bus callbacks may
fire on that worker thread and are re-emitted through a queued Qt
signal (_EventBridge) before any widget is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import (
    QByteArray,
    QObject,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSplashScreen,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ui.dialogs.app_dialogs import (
    ChannelProfileDialog,
    EngineInstallDialog,
    KeyGeneratorDialog,
    LicenseScreenDialog,
    NewProjectDialog,
    PreRenderReportDialog,
    QualityCheckDialog,
    RecoveryDialog,
    RenderCompleteDialog,
    VoiceCloneDialog,
)
from ui.panels.audio_panel import AudioPanel
from ui.panels.batch_panel import BatchPanel
from ui.panels.export_settings_panel import ExportSettingsPanel
from ui.panels.grade_panel import GradePanel
from ui.panels.import_panel import ImportPanel
from ui.panels.preview_panel import PreviewPanel
from ui.panels.progress_panel import ProgressPanel
from ui.panels.scene_controls_panel import SceneControlsPanel
from ui.panels.subtitle_style_panel import SubtitleStylePanel
from ui.panels.timeline_panel import TimelinePanel
from ui.panels.transitions_panel import TransitionsPanel
from ui.panels.voice_controls_panel import VoiceControlsPanel
from ui.panels.voice_panel import VoicePanel
from ui.theme import (
    _NAV_WIDTH,
    SPLASH_ACCENT,
    SPLASH_BG,
    SPLASH_MUTED,
)
from ui.viewmodel import ACTION_DEFS, UiViewModel, notification_model

_APPEND = "[Render]"
_STATE_MARK = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}
NAV_PAGES = (
    "▶  Render", "🎬  Studio", "🖼  Scenes", "🔀  Transitions",
    "🎨  Grade", "🔊  Audio", "💬  Subtitles", "🎚  Voice",
    "🎙  Voice Store", "📤  Export", "▦  Batch", "▤  Projects",
    "⚙  Settings",
)
_NAV = {
    "render": 0, "studio": 1, "scenes": 2, "transitions": 3,
    "grade": 4, "audio": 5, "subtitles": 6, "voice": 7,
    "voices": 8, "export": 9, "batch": 10, "projects": 11,
    "settings": 12,
}
_THEME_ACTION_IDS = tuple(
    a["id"] for a in ACTION_DEFS if a.get("theme")
)


class _EventBridge(QObject):
    """Worker-thread bus events -> queued GUI-thread signal."""

    received = pyqtSignal(dict)


class _RenderWorker(QThread):
    """Runs a pipeline job off the GUI thread.

    ``request`` runs the script pipeline (normal path); ``job`` runs
    any zero-arg callable (Batch 3: crash-recovery resume reuses the
    same worker/monitor plumbing with run_project_pipeline).
    """

    finished_with_result = pyqtSignal(dict)

    def __init__(
        self,
        engine: Any,
        request: Optional[Dict[str, Any]] = None,
        job: Optional[Any] = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._request = request or {}
        self._job = job

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            if self._job is not None:
                result = self._job()
            else:
                result = self._engine.run_script_pipeline(**self._request)
        except Exception as exc:  # noqa: BLE001 - never kill the GUI
            result = {"success": False, "error": f"render crashed: {exc}"}
        self.finished_with_result.emit(result)


class AutopilotSplash(QSplashScreen):
    """Branded boot splash: badge logo, version, animated load bar.

    The base pixmap (logo + title + version) is painted once in
    ``__init__``; every ``show_step`` re-stamps it with a progress bar
    filled to ``(index + 1) / len(steps)`` — a REAL loading bar, not
    just step text (ui_specification §1).
    """

    _W = 520
    _H = 300

    def __init__(self, model: Dict[str, Any]) -> None:
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QColor, QPainter, QPixmap

        self._steps: list = list(model.get("steps") or [])
        self._logo = str(model.get("logo") or "▶")
        self._version = str(model.get("version") or "")
        self._progress = 0.0
        base = QPixmap(self._W, self._H)
        base.fill(QColor(SPLASH_BG))
        painter = QPainter(base)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Badge logo: accent rounded square with a dark play glyph.
        badge = QRect(228, 24, 64, 64)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(SPLASH_ACCENT))
        painter.drawRoundedRect(badge, 12, 12)
        font = painter.font()
        font.setPointSize(22)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(SPLASH_BG))
        painter.drawText(
            badge, int(Qt.AlignmentFlag.AlignCenter), self._logo
        )
        font.setPointSize(28)
        painter.setFont(font)
        painter.setPen(QColor(SPLASH_ACCENT))
        painter.drawText(
            QRect(0, 106, self._W, 44),
            int(Qt.AlignmentFlag.AlignHCenter),
            str(model.get("title") or "AUTOPILOT"),
        )
        font.setPointSize(12)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(SPLASH_MUTED))
        painter.drawText(
            QRect(0, 156, self._W, 24),
            int(Qt.AlignmentFlag.AlignHCenter),
            str(model.get("subtitle") or ""),
        )
        if self._version:
            painter.drawText(
                QRect(0, 8, self._W - 14, 20),
                int(Qt.AlignmentFlag.AlignRight),
                f"v{self._version}",
            )
        painter.end()
        self._base = base
        super().__init__(base)

    def show_step(self, index: int) -> None:
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QColor, QPainter

        total = max(len(self._steps), 1)
        self._progress = min(1.0, max(0, index + 1) / total)
        pixmap = self._base.copy()
        painter = QPainter(pixmap)
        track = QRect(60, 232, self._W - 120, 10)
        painter.setPen(QColor(SPLASH_MUTED))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(track)
        fill = QRect(
            track.x() + 2,
            track.y() + 2,
            int((track.width() - 4) * self._progress),
            track.height() - 4,
        )
        painter.fillRect(fill, QColor(SPLASH_ACCENT))
        painter.end()
        self.setPixmap(pixmap)
        text = ""
        if 0 <= index < len(self._steps):
            text = (
                f"{self._steps[index]}…  ({index + 1}/"
                f"{len(self._steps)})"
            )
        self.showMessage(
            text,
            int(Qt.AlignmentFlag.AlignHCenter
                | Qt.AlignmentFlag.AlignBottom),
            Qt.GlobalColor.lightGray,
        )
        app = QApplication.instance()
        if app is not None:
            app.processEvents()


class MainWindow(QMainWindow):
    """Autopilot main window: nav rail + Render/Projects/Settings pages."""

    def __init__(self, viewmodel: UiViewModel) -> None:
        super().__init__()
        self.vm = viewmodel
        self._worker: Optional[_RenderWorker] = None
        self._bridge = _EventBridge(self)
        self._bridge.received.connect(self._on_pipeline_event)
        self._unsubscribe = self.vm.subscribe_pipeline(self._bridge.received.emit)
        self._stage_rows: Dict[str, QListWidgetItem] = {}
        self._toasts: list = []
        self._batch_stop_flag = False

        self.setWindowTitle(self.vm.window_title())
        self.resize(1080, 680)
        self.setCentralWidget(self._build_shell())
        self._actions: Dict[str, QAction] = {}
        self._build_toolbar()
        self._build_menus()
        self._build_statusbar_fields()
        self._install_shortcuts()
        self.statusBar().showMessage(
            self.vm.license_summary()["message"] + " — ready"
        )
        self._refresh_projects()
        self._reset_stages()
        self._update_undo_actions()
        self.nav_list.setCurrentRow(0)
        # workflow_spec auto-save: rotating DB snapshots on a timer.
        interval_ms = self.vm.autosave_interval_seconds() * 1000
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(
            lambda: self.vm.autosave_tick()
        )
        self._autosave_timer.start(interval_ms)
        # Window memory (ui_specification): restore last geometry and
        # toolbar/dock state; saved again on close below.
        saved = self.vm.window_state_load()
        if saved.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(
                saved["geometry"].encode("ascii")))
        if saved.get("state"):
            self.restoreState(QByteArray.fromBase64(
                saved["state"].encode("ascii")))
        # v3.0 #16: boot into the remembered workspace layout.
        self._apply_workspace(self.vm.workspace_model()["current"])
        # Deep-dive fix #7: any button without a hand-written tip gets
        # its label as tooltip, so no control is undiscoverable.
        for button in self.findChildren(QPushButton):
            if not button.toolTip():
                button.setToolTip(button.text())

    # ------------------------------------------------------------------
    # Shell: nav rail + stacked pages
    # ------------------------------------------------------------------
    def _build_shell(self) -> QWidget:
        shell = QWidget()
        column = QVBoxLayout(shell)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        self.nav_list.setFixedWidth(_NAV_WIDTH)
        self.nav_list.addItems(NAV_PAGES)
        nav_tips = (
            "Render — script, images and the live pipeline monitor",
            "Studio — import zones, preview tabs, visual timeline",
            "Scenes — per-scene animation, intensity and duration",
            "Transitions — type, duration, apply-to-all per scene",
            "Grade — color correction, presets and LUT",
            "Audio — mixer, ducking, fades, narration waveform",
            "Subtitles — one style burned into every export",
            "Voice — speed, pitch, emotion, reverb, preview",
            "Voice Store — TTS voice catalog and installed voices",
            "Export — resolution, FPS, codec, folder, naming",
            "Batch — queue many renders with priorities",
            "Projects — recent projects and their folders",
            "Settings — license, engines, backup, logs",
        )
        for index, tip in enumerate(nav_tips):
            item = self.nav_list.item(index)
            if item is not None:
                item.setToolTip(tip)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_render_page())
        self.pages.addWidget(self._build_studio_page())
        self.pages.addWidget(self._build_scenes_page())
        self.pages.addWidget(self._build_transitions_page())
        self.pages.addWidget(self._build_grade_page())
        self.pages.addWidget(self._build_audio_page())
        self.pages.addWidget(self._build_subtitles_page())
        self.pages.addWidget(self._build_voice_controls_page())
        self.pages.addWidget(self._build_voices_page())
        self.pages.addWidget(self._build_export_settings_page())
        self.pages.addWidget(self._build_batch_page())
        self.pages.addWidget(self._build_projects_page())
        self.pages.addWidget(self._build_settings_page())
        self.nav_list.currentRowChanged.connect(self.pages.setCurrentIndex)
        # 3-panel layout (ui_specification §4): left nav rail, center
        # pages, right Inspector — v3.0 #15 makes the Inspector a real
        # dock: drag it left/right, float it, or close it.
        self.inspector = self._build_inspector()
        row.addWidget(self.nav_list)
        row.addWidget(self.pages, 1)
        column.addLayout(row, 1)
        self.inspector_dock = QDockWidget("Inspector", self)
        self.inspector_dock.setObjectName("inspectorDock")
        self.inspector_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.inspector_dock.setWidget(self.inspector)
        self.addDockWidget(
            Qt.DockWidgetArea.RightDockWidgetArea,
            self.inspector_dock,
        )
        # Collapsible bottom Render Progress strip (spec §12).
        self.progress_panel = ProgressPanel(
            self.vm, on_cancel=self._on_cancel_clicked
        )
        column.addWidget(self.progress_panel)
        # 3.0.6 round-5 readability: every form on every page gets
        # breathing room, and every field is wide enough to read —
        # Settings included, not Settings-only.
        for form in self.findChildren(QFormLayout):
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(10)
        for field in self.findChildren(QLineEdit):
            if field.minimumWidth() < 340:
                field.setMinimumWidth(340)
        for combo in self.findChildren(QComboBox):
            if combo.minimumWidth() < 220:
                combo.setMinimumWidth(220)
        self.pages.setMinimumWidth(600)
        return shell

    # ------------------------------------------------------------------
    # Right Inspector card (3-panel layout, ui_specification §4/§9)
    # ------------------------------------------------------------------
    def _build_inspector(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        # UI REDESIGN (v3.2.9): was 280-340px, reserved on every single
        # page regardless of whether there's anything to inspect — a
        # meaningful chunk of window width spent on mostly-static app
        # info most of the time. Narrower default; still wide enough for
        # the actual inspector content (chapter/timing text), and still
        # hideable entirely via View → Show Inspector for users who want
        # the space back completely.
        frame.setMinimumWidth(220)
        frame.setMaximumWidth(260)
        frame.setToolTip(
            "Inspector — details of the scene selected on the "
            "timeline (hide via View → Show Inspector)")
        box = QVBoxLayout(frame)
        # Review fix (3.0.1): the dock already carries the "Inspector"
        # title — a second in-card header duplicated it. Removed.
        # Default content: app stats (deep-dive fix #3) — the panel is
        # never an empty "nothing selected" dead end.
        self.insp_details = QLabel("")
        self.insp_details.setWordWrap(True)
        self._set_inspector_lines(self.vm.inspector_stats_model())
        box.addWidget(self.insp_details)
        box.addStretch(1)
        # Scene quick actions (deep-dive fix #9) — only with selection.
        self.insp_scene_bar = QWidget()
        scene_row = QHBoxLayout(self.insp_scene_bar)
        scene_row.setContentsMargins(0, 0, 0, 0)
        copy_btn = QPushButton("📄 Copy")
        copy_btn.setToolTip("Copy selected scene (Ctrl+C)")
        copy_btn.clicked.connect(self._act_copy_scene)
        delete_btn = QPushButton("🗑 Delete")
        delete_btn.setObjectName("danger")
        delete_btn.setToolTip("Delete selected scene (Del)")
        delete_btn.clicked.connect(self._act_delete_scene)
        scene_row.addWidget(copy_btn)
        scene_row.addWidget(delete_btn)
        self.insp_scene_bar.setVisible(False)
        box.addWidget(self.insp_scene_bar)
        # App quick actions — always available at the panel bottom.
        actions = QHBoxLayout()
        new_btn = QPushButton("🆕 New")
        new_btn.setToolTip("New Project (Ctrl+N)")
        new_btn.clicked.connect(
            lambda: self._dispatch_action("new_project"))
        render_btn = QPushButton("▶ Render")
        render_btn.setObjectName("primary")
        render_btn.setToolTip("Start Render (F9)")
        render_btn.clicked.connect(
            lambda: self._dispatch_action("start_render"))
        actions.addWidget(new_btn)
        actions.addWidget(render_btn)
        box.addLayout(actions)
        return frame

    def _set_inspector_lines(self, lines: list) -> None:
        """Review fix (3.0.1): elide long lines (raw Windows paths
        overflowed the card); the full text rides the tooltip."""
        # 3.0.6 round 5: never truncate — the card word-wraps every
        # line (Qt breaks long anywhere-fit words like paths) and the
        # tooltip mirrors the full text. Replaces the 3.0.1 middle
        # elide that produced "Autopilot 3.0.7 ...cumentary" cut-offs
        # (and makes the 3.0.5 getattr width guard unnecessary: no
        # width is read here at all any more).
        full = "\n".join(str(x) for x in lines)
        self.insp_details.setWordWrap(True)
        self.insp_details.setText(full)
        tooltip = full
        extra = self._engine_paths_tooltip(self.vm.engines_status())
        if extra and "not found" not in extra:
            tooltip += "\n\n" + extra
        self.insp_details.setToolTip(tooltip)

    @staticmethod
    def _engine_paths_tooltip(status: Dict[str, Any]) -> str:
        rows = []
        for name, key in (("FFmpeg", "ffmpeg"),
                          ("FFprobe", "ffprobe"),
                          ("Piper TTS", "piper")):
            value = status.get(key)
            if value:
                rows.append(f"{name}: {value}")
        return "\n".join(rows)

    def _refresh_inspector(self, number: Optional[int]) -> None:
        """Timeline selection -> Inspector content (RULE 7)."""
        lines: list = []
        try:
            project_id = str(
                self.timeline_panel.current_project_id() or "")
            if number is not None and project_id:
                model = self.vm.scene_details_model(project_id, number)
                lines = list(model.get("lines") or [])
        except Exception:  # noqa: BLE001 - inspector is advisory
            lines = []
        if lines:
            self._set_inspector_lines(lines)
            self.insp_scene_bar.setVisible(True)
        else:
            self._set_inspector_lines(self.vm.inspector_stats_model())
            self.insp_scene_bar.setVisible(False)

    # ------------------------------------------------------------------
    # Render page
    # ------------------------------------------------------------------
    def _build_render_page(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_form())
        splitter.addWidget(self._build_monitor())
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_form(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        title = QLabel("New render")
        title.setObjectName("panelTitle")
        form.addRow(title)
        self.script_edit, browse_script = self._path_row(
            "Browse…", self._browse_script
        )
        self.images_edit, browse_images = self._path_row(
            "Browse…", self._browse_images
        )
        self.project_edit, browse_project = self._path_row(
            "Browse…", self._browse_project
        )
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText(
            "Video title (defaults to script name)"
        )
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("(default preset)", None)
        for preset in self.vm.export_presets():
            self.preset_combo.addItem(preset["label"], preset["id"])
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("(channel default)", None)
        for profile in self.vm.channel_profiles():
            self.profile_combo.addItem(profile["label"], profile["id"])
        self.quality_gate_check = QCheckBox(
            "Abort on critical quality issues"
        )
        self.render_button = QPushButton("▶ Render video")
        self.render_button.setObjectName("primary")
        self.render_button.clicked.connect(self._on_render_clicked)
        self.pre_render_button = QPushButton("📋 Report")
        self.pre_render_button.setToolTip(
            "Pre-render report: quality checks before you burn time"
        )
        self.pre_render_button.clicked.connect(self._act_pre_render_report)
        self.cancel_button = QPushButton("■ Cancel")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        buttons = QHBoxLayout()
        buttons.addWidget(self.render_button)
        buttons.addWidget(self.pre_render_button)
        buttons.addWidget(self.cancel_button)
        form.addRow("Script file:", self._wrap(self.script_edit, browse_script))
        form.addRow("Images folder:", self._wrap(self.images_edit, browse_images))
        form.addRow("Project folder:", self._wrap(self.project_edit, browse_project))
        form.addRow("Title:", self.title_edit)
        form.addRow("Export preset:", self.preset_combo)
        form.addRow("Channel profile:", self.profile_combo)
        form.addRow("", self.quality_gate_check)
        form.addRow("", buttons)
        hint = QLabel("Narration, grade, transitions, subtitles and"
                      " thumbnails are generated fully offline.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form.addRow(hint)
        return panel

    def _build_monitor(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        heading = QLabel("Pipeline")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.stage_list = QListWidget()
        self.stage_list.setObjectName("stageList")
        layout.addWidget(self.stage_list)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        layout.addWidget(self.progress_bar)
        log_head = QLabel("Log")
        log_head.setObjectName("panelTitle")
        layout.addWidget(log_head)
        self.log_pane = QPlainTextEdit()
        self.log_pane.setReadOnly(True)
        layout.addWidget(self.log_pane)
        return panel

    # ------------------------------------------------------------------
    # Studio page: Import + Preview + Timeline (Batch 2)
    # ------------------------------------------------------------------
    def _build_studio_page(self) -> QWidget:
        self.import_panel = ImportPanel(
            self.vm,
            on_staged=self._on_import_staged,
            project_folder_provider=lambda: self.project_edit.text().strip(),
            status_sink=self.statusBar().showMessage,
        )
        self.preview_panel = PreviewPanel(
            self.vm, status_sink=self.statusBar().showMessage
        )
        self.timeline_panel = TimelinePanel(
            self.vm,
            status_sink=self.statusBar().showMessage,
            on_scene_changed=self._on_scene_structure_changed,
            cta=lambda: self.nav_list.setCurrentRow(_NAV["render"]),
        )
        self.timeline_panel.on_scene_selected = self._refresh_inspector
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.import_panel)
        top.addWidget(self.preview_panel)
        top.setStretchFactor(1, 2)
        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(top)
        vertical.addWidget(self.timeline_panel)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        return vertical

    def _on_scene_structure_changed(self) -> None:
        """Scene ops (timeline/edit menu) -> storyboard stays honest."""
        self.preview_panel.reload_storyboard()

    def _build_scenes_page(self) -> QWidget:
        self.scene_controls_panel = SceneControlsPanel(
            self.vm,
            status_sink=self._notify_status,
            on_structure_changed=self._on_scene_structure_changed,
            open_grade=lambda: self.nav_list.setCurrentRow(
                _NAV["grade"]),
        )
        return self.scene_controls_panel

    def _build_transitions_page(self) -> QWidget:
        self.transitions_panel = TransitionsPanel(
            self.vm,
            status_sink=self._notify_status,
            on_structure_changed=self._on_scene_structure_changed,
        )
        return self.transitions_panel

    def _build_grade_page(self) -> QWidget:
        self.grade_panel = GradePanel(
            self.vm, status_sink=self._notify_status
        )
        return self.grade_panel

    def _build_subtitles_page(self) -> QWidget:
        self.subtitle_style_panel = SubtitleStylePanel(
            self.vm, status_sink=self._notify_status
        )
        return self.subtitle_style_panel

    def _build_voice_controls_page(self) -> QWidget:
        self.voice_controls_panel = VoiceControlsPanel(
            self.vm,
            status_sink=self._notify_status,
            preview_callback=self._preview_audio_file,
        )
        return self.voice_controls_panel

    def _build_export_settings_page(self) -> QWidget:
        self.export_settings_panel = ExportSettingsPanel(
            self.vm, status_sink=self._notify_status
        )
        return self.export_settings_panel

    def _build_audio_page(self) -> QWidget:
        self.audio_panel = AudioPanel(
            self.vm,
            status_sink=self._notify_status,
            preview_callback=self._preview_audio_file,
        )
        return self.audio_panel

    def _build_voices_page(self) -> QWidget:
        self.voice_panel = VoicePanel(
            self.vm,
            status_sink=self._notify_status,
            preview_callback=self._preview_audio_file,
        )
        return self.voice_panel

    def _build_batch_page(self) -> QWidget:
        self.batch_panel = BatchPanel(
            self.vm,
            status_sink=self._notify_status,
            add_current_provider=self._batch_payload,
            on_start=self._batch_start,
            on_stop=self._batch_stop_now,
        )
        return self.batch_panel

    def _preview_audio_file(self, path: str) -> None:
        self.nav_list.setCurrentRow(_NAV["studio"])
        self.preview_panel.open_source(path, "narration preview")

    def _batch_payload(self) -> Dict[str, Any]:
        # BUGFIX (caught before shipping): profile_combo's first item is
        # a literal "(channel default)" placeholder (data=None) — using
        # .currentText() unconditionally would store that placeholder
        # STRING as the channel name when nothing real is selected.
        # Check .currentData() first; only use the label when a real
        # channel profile is actually selected.
        channel = (
            self.profile_combo.currentText().strip()
            if self.profile_combo.currentData() is not None
            else ""
        )
        return {
            "script_path": self.script_edit.text().strip(),
            "images_folder": self.images_edit.text().strip(),
            "project_folder": self.project_edit.text().strip(),
            "title": self.title_edit.text().strip(),
            # UI REDESIGN (v3.2.7): thread the already-selected channel
            # profile through to the queue — previously this widget's
            # selection was used only for the immediate render, so a
            # multi-channel creator had no way to tell queued items
            # apart by channel once several were queued up.
            "channel": channel,
        }

    def _batch_start(self, ids: list) -> None:
        """Render queued items sequentially with the existing worker."""
        if self._worker is not None and self._worker.isRunning():
            self.statusBar().showMessage("A render is already running.")
            return
        if not self.vm.engine_ready():
            self._log("ERROR: engine not ready — cannot run batch.",
                      error=True)
            return
        engine = self.vm.engine
        self._batch_stop_flag = False

        def _run_queue() -> Dict[str, Any]:
            rows = {
                row["id"]: row for row in self.vm.batch_model()["rows"]
            }
            done = failed = 0
            for batch_id in ids:
                if self._batch_stop_flag:
                    break
                row = rows.get(str(batch_id))
                if not row:
                    continue
                self.vm.batch_set_status(str(batch_id), "running")
                # FEATURE (v3.2.14): "audio only" queued jobs skip every
                # video-generation stage — much faster to run overnight
                # for narration review before committing to a full
                # video render. skip_stages already safely bypasses
                # even "required" stages (confirmed: the stage-skip
                # check runs before the required-module check, so this
                # never trips a false failure).
                skip_stages = (
                    (
                        "images", "intro_outro", "subtitles", "timeline",
                        "export", "burn_subtitles", "verify", "thumbnails",
                        "drive_upload",
                    )
                    if row.get("job_type") == "audio_only" else ()
                )
                try:
                    result = engine.run_script_pipeline(
                        script_path=row["script_path"],
                        images_folder=row["images_folder"],
                        project_folder=row["folder"],
                        title=row["title"],
                        skip_stages=skip_stages,
                    )
                except Exception as exc:  # noqa: BLE001 - keep queue alive
                    result = {"success": False, "error": str(exc)}
                if result.get("success"):
                    done += 1
                    output = str(
                        (result.get("data") or {}).get("output_file_path")
                        or ""
                    )
                    self.vm.batch_set_status(
                        str(batch_id), "completed", output=output
                    )
                else:
                    failed += 1
                    error = str(result.get("error") or "render failed")
                    self.vm.batch_set_status(
                        str(batch_id), "failed", error=error
                    )
                    if self._batch_stop_flag:
                        break
            return {"success": failed == 0,
                    "data": {"done": done, "failed": failed}}

        self._reset_stages()
        self.progress_bar.setValue(0)
        self._log(f"{_APPEND} batch: {len(ids)} item(s)")
        self.render_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_panel.set_running(True)
        self.batch_panel.set_running(True)
        self.nav_list.setCurrentRow(_NAV["batch"])
        self._worker = _RenderWorker(engine, job=_run_queue)
        self._worker.finished_with_result.connect(self._on_batch_finished)
        self._worker.start()

    def _on_batch_finished(self, result: Dict[str, Any]) -> None:
        data = result.get("data") or {}
        message = (
            f"Batch finished: {int(data.get('done') or 0)} rendered,"
            f" {int(data.get('failed') or 0)} failed."
        )
        self._log(f"{_APPEND} {message}",
                  error=not result.get("success"))
        self.render_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_panel.set_running(False)
        self.batch_panel.set_running(False)
        self.batch_panel.refresh()
        self._refresh_projects()
        self.timeline_panel.reload_projects()
        self.show_notification(
            "success" if result.get("success") else "warning", message
        )
        self._refresh_statusbar_fields()

    def _batch_stop_now(self) -> None:
        self._batch_stop_flag = True
        try:
            self.vm.engine.cancel_pipeline()
        except Exception:  # noqa: BLE001
            pass
        self.statusBar().showMessage("Batch stop requested…")

    def _notify_status(self, text: str) -> None:
        """Panel status sink: status bar + toast for save-style msgs."""
        self.statusBar().showMessage(text)
        if text.lower().startswith(("grade applied", "audio settings",
                                    "scene pasted", "deleted scene")):
            self.show_notification("success", text)

    def _on_import_staged(
        self, script_path: Optional[str], images_folder: Optional[str]
    ) -> None:
        if script_path:
            self.script_edit.setText(script_path)
        if images_folder:
            self.images_edit.setText(images_folder)
        self.nav_list.setCurrentRow(_NAV["render"])
        self.statusBar().showMessage(
            "Imported files staged — render form pre-filled."
        )

    # ------------------------------------------------------------------
    # Projects page
    # ------------------------------------------------------------------
    def _build_projects_page(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        heading = QLabel("Recent projects")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.projects_list = QListWidget()
        layout.addWidget(self.projects_list)
        buttons = QHBoxLayout()
        refresh = QPushButton("↻ Refresh")
        refresh.clicked.connect(self._refresh_projects)
        self.open_folder_button = QPushButton("📁 Open project folder")
        self.open_folder_button.clicked.connect(self._open_selected_project)
        buttons.addWidget(refresh)
        buttons.addWidget(self.open_folder_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return panel

    def _open_selected_project(self) -> None:
        item = self.projects_list.currentItem()
        folder = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not folder:
            self.statusBar().showMessage("Select a project first.")
            return
        if not Path(str(folder)).is_dir():
            self.statusBar().showMessage(f"Folder missing: {folder}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ------------------------------------------------------------------
    # Settings page
    # ------------------------------------------------------------------
    def _build_settings_page(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        lic_head = QLabel("License")
        lic_head.setObjectName("panelTitle")
        layout.addWidget(lic_head)
        summary = self.vm.license_summary()
        self.license_label = QLabel(
            f"Status: {summary['status']}   —   {summary['message']}"
        )
        layout.addWidget(self.license_label)
        key_row = QHBoxLayout()
        self.license_key_edit = QLineEdit()
        self.license_key_edit.setPlaceholderText(
            "XXXX-XXXX-XXXX-XXXX (license key)"
        )
        activate = QPushButton("Activate")
        activate.setObjectName("primary")
        activate.clicked.connect(self._on_activate_license)
        key_row.addWidget(self.license_key_edit, 1)
        key_row.addWidget(activate)
        layout.addLayout(key_row)
        eng_head = QLabel("Engines")
        eng_head.setObjectName("panelTitle")
        layout.addWidget(eng_head)
        status = self.vm.engines_status()
        self.engines_label = QLabel(self._engines_text(status))
        self.engines_label.setObjectName("muted")
        self.engines_label.setWordWrap(True)
        self.engines_label.setToolTip(
            self._engine_paths_tooltip(status)
            or "Engine binaries not found on this machine yet."
        )
        layout.addWidget(self.engines_label)
        drive_head = QLabel("Google Drive backup")
        drive_head.setObjectName("panelTitle")
        layout.addWidget(drive_head)
        self.drive_label = QLabel(
            self._drive_text(self.vm.drive_upload_status())
        )
        self.drive_label.setObjectName("muted")
        self.drive_label.setWordWrap(True)
        layout.addWidget(self.drive_label)
        self.drive_resume_button = QPushButton("Resume pending uploads")
        self.drive_resume_button.clicked.connect(self._on_resume_drive)
        layout.addWidget(self.drive_resume_button)
        about = QLabel(
            "Autopilot 3.1.0 — offline documentary video automation.\n"
            "Renders run locally; no footage or scripts leave this machine."
        )
        about.setObjectName("muted")
        about.setWordWrap(True)
        layout.addWidget(about)
        layout.addStretch(1)
        return panel

    @staticmethod
    def _engines_text(status: Dict[str, Any]) -> str:
        # Review fix (3.0.1): green/red indicators instead of raw
        # paths on screen; the full paths sit on the tooltip.
        def _line(name: str, value: Optional[str]) -> str:
            mark = "✓ found" if value else "✗ not found"
            return f"  {name}: {mark}"

        lines = [
            _line("FFmpeg", status.get("ffmpeg")),
            _line("FFprobe", status.get("ffprobe")),
            _line("Piper TTS", status.get("piper")),
            f"  Modules loaded: {status.get('modules_loaded', 0)}",
            f"  Plugins loaded: {status.get('plugins_loaded', 0)}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _drive_text(status: Dict[str, Any]) -> str:
        if not status.get("available"):
            return "  Module not loaded."
        if not status.get("enabled"):
            return "  Disabled (enable in config/drive_upload.json)."
        if not status.get("configured"):
            detail = status.get("detail") or "not configured"
            return f"  Not configured — {detail}"
        return f"  Ready — {status.get('pending', 0)} pending upload(s)."

    def _on_resume_drive(self) -> None:
        ok, message = self.vm.resume_drive_uploads()
        self.drive_label.setText(
            self._drive_text(self.vm.drive_upload_status())
        )
        if ok:
            self.statusBar().showMessage(message)
        else:
            QMessageBox.warning(self, "Google Drive backup", message)

    def _on_activate_license(self) -> None:
        ok, message = self.vm.activate_license(self.license_key_edit.text())
        if ok:
            summary = self.vm.license_summary()
            self.license_label.setText(
                f"Status: {summary['status']}   —   {summary['message']}"
            )
            self.statusBar().showMessage(summary["message"] + " — ready")
            QMessageBox.information(self, "License", message)
        else:
            QMessageBox.warning(self, "License", message)

    # ------------------------------------------------------------------
    # Chrome: toolbar / menus / status bar / shortcuts (model-driven)
    # ------------------------------------------------------------------
    def _make_action(self, spec: Dict[str, Any]) -> QAction:
        icon = str(spec.get("icon") or "")
        text = f"{icon}  {spec['text']}" if icon else spec["text"]
        action = QAction(text, self)
        if spec.get("shortcut"):
            action.setShortcut(QKeySequence(spec["shortcut"]))
        action.setEnabled(bool(spec.get("enabled", True)))
        if spec.get("reason"):
            action.setToolTip(spec["reason"])
        action.triggered.connect(
            lambda _checked=False, aid=spec["id"]: self._dispatch_action(aid)
        )
        self._actions[spec["id"]] = action
        return action

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        for spec in self.vm.toolbar_model():
            toolbar.addAction(self._make_action(spec))
        self.addToolBar(toolbar)
        self.toolbar = toolbar
        # Right side of the toolbar (ui_specification): channel
        # profile dropdown + at-a-glance license status.
        spacer = QWidget(self)
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(spacer)
        profile_label = QLabel("Profile:", self)
        profile_label.setObjectName("muted")
        profile_label.setToolTip(
            "Channel profile applied to the next render")
        toolbar.addWidget(profile_label)
        self.toolbar_profile = QComboBox(self)
        self.toolbar_profile.setMinimumWidth(150)
        self.toolbar_profile.setToolTip(
            "Channel profile applied to the next render")
        self.toolbar_profile.currentIndexChanged.connect(
            self._sync_toolbar_profile
        )
        toolbar.addWidget(self.toolbar_profile)
        # v3.0 #16: workspace selector — saved layout recipes.
        workspace_label = QLabel("Workspace:", self)
        workspace_label.setObjectName("muted")
        workspace_label.setToolTip(
            "Workspace — page + Inspector layout recipe")
        toolbar.addWidget(workspace_label)
        self.workspace_combo = QComboBox(self)
        self.workspace_combo.setMinimumWidth(118)
        self.workspace_combo.setToolTip(
            "Workspace — switch page and Inspector layout; the "
            "choice is remembered")
        self.workspace_combo.currentTextChanged.connect(
            self._on_workspace_changed)
        toolbar.addWidget(self.workspace_combo)
        self.toolbar_license = QLabel("", self)
        self.toolbar_license.setObjectName("muted")
        self.toolbar_license.setToolTip(
            "License status — Help → License Status shows the HWID")
        self.toolbar_license.setContentsMargins(8, 0, 12, 0)
        toolbar.addWidget(self.toolbar_license)
        self._reload_toolbar_profile()
        self._reload_workspace_combo()
        self._refresh_toolbar_license()

    def _reload_toolbar_profile(self) -> None:
        """Fill the toolbar profile dropdown from channel_profiles."""
        combo = self.toolbar_profile
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(channel default)", None)
        for profile in self.vm.channel_profiles():
            combo.addItem(
                f"{profile.get('icon', '')}{profile['label']}".strip(),
                profile["id"],
            )
        combo.blockSignals(False)

    def _sync_toolbar_profile(self, _index: int) -> None:
        """Toolbar dropdown -> render form combo (single source)."""
        data = self.toolbar_profile.currentData()
        form = self.profile_combo
        form.setCurrentIndex(max(0, form.findData(data)))

    def _refresh_toolbar_license(self) -> None:
        summary = self.vm.license_summary()
        text = f"🔑 {summary['status']}"
        days = summary.get("days_remaining")
        if days is not None:
            text += f" ({days}d)"
        self.toolbar_license.setText(text)

    # -- v3.0 #16: workspaces ------------------------------------------
    def _reload_workspace_combo(self) -> None:
        model = self.vm.workspace_model()
        self.workspace_combo.blockSignals(True)
        self.workspace_combo.clear()
        self.workspace_combo.addItems(model["names"])
        self.workspace_combo.setCurrentText(model["current"])
        self.workspace_combo.blockSignals(False)

    def _on_workspace_changed(self, name: str) -> None:
        ok, message = self.vm.set_workspace(name)
        self._apply_workspace(name)
        self.statusBar().showMessage(message)
        if ok:
            self.show_notification("info", message)

    def _apply_workspace(self, name: str) -> None:
        layout = self.vm.workspace_layout(name)
        page = str(layout.get("page") or "")
        if page in _NAV:
            self.nav_list.setCurrentRow(_NAV[page])
        inspector = layout.get("inspector")
        if inspector is not None and hasattr(self, "inspector_dock"):
            self.inspector_dock.setVisible(bool(inspector))

    def _build_menus(self) -> None:
        for menu_spec in self.vm.menu_model():
            menu = self.menuBar().addMenu(menu_spec["title"])
            for item in menu_spec["items"]:
                if item.get("separator"):
                    menu.addSeparator()
                    continue
                if item.get("submenu"):
                    # File ▸ Export — partial-workflow exports (35-39)
                    # live in ONE submenu to keep File scannable.
                    sub = menu.addMenu(item["submenu"])
                    for child in item["items"]:
                        if child["id"] in self._actions:
                            sub.addAction(self._actions[child["id"]])
                        else:
                            sub.addAction(self._make_action(child))
                    continue
                if item["id"] in self._actions:
                    menu.addAction(self._actions[item["id"]])
                else:
                    menu.addAction(self._make_action(item))

    def _build_statusbar_fields(self) -> None:
        model = self.vm.status_bar_model()
        self.sb_project = QLabel("Project: —")
        self.sb_ffmpeg = QLabel("FFmpeg: —")
        self.sb_system = QLabel("RAM — · CPU —")
        self.sb_plugins = QLabel(model.get("plugins", ""))
        self.sb_modules = QLabel(model.get("modules", ""))
        self.sb_license = QLabel(model.get("license", ""))
        for label in (self.sb_project, self.sb_ffmpeg, self.sb_system,
                      self.sb_plugins, self.sb_modules, self.sb_license):
            label.setObjectName("muted")
            self.statusBar().addPermanentWidget(label)
        self.sb_project.setToolTip("Project loaded on the timeline")
        self.sb_ffmpeg.setToolTip("External encoder availability")
        self.sb_system.setToolTip(
            "Machine load (updates every 5 s; em-dash if the "
            "platform reports nothing)")
        # RAM/CPU/project tick (deep-dive fix #5); cheap, best-effort.
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_system_status)
        self._status_timer.start(5000)
        self._refresh_system_status()

    def _refresh_system_status(self) -> None:
        try:
            m = self.vm.system_status_model()
            self.sb_system.setText(f"{m['ram']} · {m['cpu']}")
            self.sb_ffmpeg.setText(m["ffmpeg"])
        except Exception:  # noqa: BLE001 - status is advisory
            pass
        project = ""
        try:
            project = str(
                self.timeline_panel.current_project_id() or "")
        except Exception:  # noqa: BLE001
            project = ""
        self.sb_project.setText(f"Project: {project or '—'}")

    def _refresh_statusbar_fields(self) -> None:
        model = self.vm.status_bar_model()
        self.sb_plugins.setText(model.get("plugins", ""))
        self.sb_modules.setText(model.get("modules", ""))
        self.sb_license.setText(model.get("license", ""))
        if hasattr(self, "toolbar_license"):
            self._refresh_toolbar_license()
        if hasattr(self, "sb_system"):
            self._refresh_system_status()

    def _install_shortcuts(self) -> None:
        # toggle_preview binds in Batch 2 (Space is hostile while typing
        # in the render form until the preview panel owns it).
        skip = {"toggle_preview"}
        for action_id, key in self.vm.shortcuts_map().items():
            if action_id in skip:
                continue
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(
                lambda aid=action_id: self._dispatch_action(aid)
            )

    # ------------------------------------------------------------------
    # Single action dispatcher (menus, toolbar, shortcuts share this)
    # ------------------------------------------------------------------
    def _dispatch_action(self, action_id: str) -> None:
        handlers = {
            "new_project": self._act_new_project,
            "open_project": self._act_open_project,
            "save_project": self._act_save_project,
            "import_files": self._act_import_files,
            "import_zip": self._act_import_zip,
            "export_video": self._act_export_video,
            "export_audio_only":
                lambda: self._act_export("export_audio_only"),
            "export_audio_mix":
                lambda: self._act_export("export_audio_mix"),
            "burn_subtitles":
                lambda: self._act_export("burn_subtitles"),
            "export_thumbnails":
                lambda: self._act_export("export_thumbnails"),
            "export_storyboard_pdf":
                lambda: self._act_export("export_storyboard_pdf"),
            "backup_project": self._act_backup,
            "quit": self.close,
            "undo": self._act_undo,
            "redo": self._act_redo,
            "select_all": self._act_select_all,
            "copy_scene": self._act_copy_scene,
            "paste_scene": self._act_paste_scene,
            "delete_scene": self._act_delete_scene,
            "toggle_preview": self._act_toggle_preview,
            "refresh_projects": self._act_refresh,
            "theme_dark": lambda: self._switch_theme("dark"),
            "theme_light": lambda: self._switch_theme("light"),
            "theme_amoled": lambda: self._switch_theme("amoled"),
            "theme_high_contrast":
                lambda: self._switch_theme("high_contrast"),
            "toggle_toolbar": self._act_toggle_toolbar,
            "toggle_statusbar": self._act_toggle_statusbar,
            "toggle_progress_panel":
                self.progress_panel.toggle_body,
            "toggle_fullscreen": self._act_toggle_fullscreen,
            "toggle_inspector": self._act_toggle_inspector,
            "open_settings": self._act_open_settings,
            "channel_profiles": self._act_channel_profiles,
            "quality_check": self._act_quality_check,
            "pre_render_report": self._act_pre_render_report,
            "start_render": self._on_render_clicked,
            "quick_preview": self._act_quick_preview,
            "cancel_render": self._on_cancel_clicked,
            "pause_render": self._act_pause_render,
            "resume_render": self._act_resume,
            "batch_render": self._act_batch_page,
            "render_settings": self._act_render_settings,
            "voice_store": self._act_voice_store,
            "voice_clone": self._act_voice_clone,
            "engine_manager": self._act_engine_manager,
            "setup_wizard": self._act_setup_wizard,
            "key_generator": self._act_key_generator,
            "modules": self._show_modules,
            "plugins_status": self._show_plugins,
            "open_logs": self._act_open_logs,
            "user_guide": self._act_user_guide,
            "shortcuts": self._show_shortcuts,
            "license_status": self._show_license,
            "about": self._show_about,
        }
        handler = handlers.get(str(action_id))
        if handler is None:
            self.statusBar().showMessage(f"Unknown action: {action_id}")
            return
        handler()

    def _switch_theme(self, name: str) -> None:
        from ui.theme import apply_theme  # lazy: needs running Qt env

        ok, message = self.vm.set_theme(name)
        app = QApplication.instance()
        if ok and app is not None:
            apply_theme(app, name)
        for definition in ACTION_DEFS:
            theme_name = definition.get("theme")
            if not theme_name:
                continue
            action = self._actions.get(definition["id"])
            if action is not None:
                action.setCheckable(True)
                action.setChecked(theme_name == name)
        self.statusBar().showMessage(message)
        if ok:
            self.show_notification("success", message)

    # -- Batch 1 placeholder actions (Batch 2/3 re-point these) --------
    def _act_new_project(self) -> None:
        dialog = NewProjectDialog(self.vm, self)
        if dialog.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload or {}
        self.project_edit.setText(str(payload.get("project_folder") or ""))
        self.title_edit.setText(str(payload.get("title") or ""))
        if payload.get("script_path"):
            self.script_edit.setText(str(payload["script_path"]))
        if payload.get("images_folder"):
            self.images_edit.setText(str(payload["images_folder"]))
        self.nav_list.setCurrentRow(_NAV["render"])
        self.statusBar().showMessage(
            f"Project '{payload.get('title')}' ready — press Render."
        )

    def _act_open_project(self) -> None:
        self.nav_list.setCurrentRow(_NAV["projects"])
        self.statusBar().showMessage(
            "Pick a project, then 'Open project folder'."
        )

    def _act_export_video(self) -> None:
        self.nav_list.setCurrentRow(_NAV["studio"])
        source = self.vm.preview_source()
        if source.get("exists"):
            self.preview_panel.open_last()
            return
        for row in self.vm.refresh_projects(limit=1):
            output = row.get("last_render_output_path")
            if output and Path(str(output)).is_file():
                QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(Path(str(output)).parent))
                )
                return
        self.statusBar().showMessage("No finished render to reveal yet.")

    # ------------------------------------------------------------------
    # File ▸ Export submenu (partial-workflow exports, spec 35-39)
    # ------------------------------------------------------------------
    def _act_export(self, kind: str) -> None:
        from ui.dialogs.app_dialogs import ExportJobDialog

        if self._worker is not None and self._worker.isRunning():
            self.statusBar().showMessage("A job is already running.")
            return
        dialog = ExportJobDialog(self.vm, kind, self)
        if dialog.exec() != ExportJobDialog.DialogCode.Accepted:
            return
        job = dialog.job or {}
        if kind == "export_thumbnails":
            ok, msg, payload = self.vm.export_thumbnail_jobs(
                job.get("project_id"), job.get("out_dir"))
            if not ok:
                self.show_notification("warning", msg)
                return
            self._run_thumbnails(payload)
            return
        jobs = {
            "export_audio_only":
                lambda: self.vm.export_audio_only(
                    job.get("script", ""), job.get("out", "")),
            "export_audio_mix":
                lambda: self.vm.export_audio_mix(
                    job.get("narration", ""), job.get("music", ""),
                    job.get("sfx", ""), job.get("out", "")),
            "burn_subtitles":
                lambda: self.vm.burn_subtitles(
                    job.get("video", ""), job.get("srt", ""),
                    job.get("out", "")),
            "export_storyboard_pdf":
                lambda: self.vm.export_storyboard_pdf(
                    job.get("project_id"), job.get("out", "")),
        }
        fn = jobs.get(kind)
        if fn is None:
            return
        self.progress_panel.set_running(True)
        self._log(f"[Export] starting {kind}…")
        self._worker = _RenderWorker(self.vm.engine, job=fn)
        self._worker.finished_with_result.connect(
            self._on_export_finished)
        self._worker.start()

    def _on_export_finished(self, result: Any) -> None:
        self.progress_panel.set_running(False)
        self._worker = None
        if isinstance(result, tuple):
            ok = bool(result[0])
            message = str(result[1]) if len(result) > 1 else ""
            payload = result[2] if len(result) > 2 else {}
        elif isinstance(result, dict):
            ok = bool(result.get("success"))
            message = str(result.get("message")
                          or result.get("error") or "Export finished.")
            payload = result
        else:
            ok, message, payload = False, str(result), {}
        if isinstance(payload, dict) and payload.get("cmd"):
            self._log(f"[Export] $ {payload['cmd']}")  # RULE 4
        self.show_notification("success" if ok else "error",
                               message[:160])

    def _run_thumbnails(self, payload: Dict[str, Any]) -> None:
        """Thumb scaling needs QPixmap — GUI thread, fast, local."""
        from PyQt6.QtGui import QPixmap

        jobs = list(payload.get("jobs") or [])
        out_dir = Path(str(payload.get("out_dir") or ""))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.show_notification(
                "error", f"Cannot create output folder: {exc}")
            return
        done = 0
        for job in jobs:
            pix = QPixmap(str(job.get("src") or ""))
            if pix.isNull():
                continue
            scaled = pix.scaled(
                320, 180, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            if scaled.save(str(job.get("dst")), "JPG", 90):
                done += 1
        self.show_notification(
            "success" if done else "warning",
            f"Thumbnails exported: {done}/{len(jobs)} → {out_dir}")

    def _act_save_project(self) -> None:
        text = ("Projects auto-save through the pipeline — "
                "nothing to save.")
        self.statusBar().showMessage(text)
        self.show_notification("info", text)

    def _act_pause_render(self) -> None:
        self.statusBar().showMessage(
            "Pause is not supported by engine v1 (cancel keeps DB state)."
        )

    def _act_toggle_preview(self) -> None:
        self.nav_list.setCurrentRow(_NAV["studio"])
        self.preview_panel.toggle()

    def _act_import_files(self) -> None:
        self.nav_list.setCurrentRow(_NAV["studio"])
        self.import_panel.open_file_dialog()

    def _act_open_settings(self) -> None:
        self.nav_list.setCurrentRow(_NAV["settings"])

    # -- Menu: View / Project / Render extras / Tools (spec File 04) ---
    def _act_refresh(self) -> None:
        self._refresh_projects()
        self.timeline_panel.reload_projects()
        self._refresh_statusbar_fields()
        self.statusBar().showMessage("Projects refreshed.")

    # -- Menu: View / Project / Render extras / Tools (spec File 04) ---
    def _act_pre_render_report(self) -> None:
        dialog = PreRenderReportDialog(
            self.vm,
            {
                "script_path": self.script_edit.text().strip(),
                "images_folder": self.images_edit.text().strip(),
                "project_folder": self.project_edit.text().strip(),
                "title": self.title_edit.text().strip(),
            },
            self,
        )
        dialog.exec()
        if dialog.start_requested:
            self._on_render_clicked()

    # -- Menu: File extras ------------------------------------------------
    def _act_import_zip(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Import project ZIP", "", "ZIP archives (*.zip)"
        )
        if not path:
            return
        result = self.vm.import_zip(path)
        if not result.get("success"):
            self.show_notification("error", str(result.get("error")))
            return
        self.script_edit.setText(str(result.get("script_path") or ""))
        if result.get("images_folder"):
            self.images_edit.setText(str(result["images_folder"]))
        self.project_edit.setText(str(result.get("project_folder") or ""))
        self.title_edit.setText(str(result.get("title") or ""))
        self.nav_list.setCurrentRow(_NAV["render"])
        message = (
            f"ZIP imported — {result.get('copied', 0)} file(s) staged."
        )
        self.statusBar().showMessage(message)
        self.show_notification("success", message)

    def _act_backup(self) -> None:
        ok, message = self.vm.backup_now()
        self.show_notification("success" if ok else "error", message)
        self.statusBar().showMessage(message)

    # -- Menu: Edit (scene ops — same seam as the timeline context menu) --
    def _scene_context(self) -> tuple:
        project_id = self.timeline_panel.current_project_id()
        number = self.timeline_panel.selected_scene
        self.nav_list.setCurrentRow(_NAV["studio"])
        return project_id, number

    def _refresh_after_scene_op(self, message: str, ok: bool) -> None:
        self.statusBar().showMessage(message)
        self.timeline_panel.refresh()
        self.preview_panel.reload_storyboard()
        if ok:
            self.show_notification("success", message)

    def _act_copy_frame(self) -> None:
        self.nav_list.setCurrentRow(_NAV["studio"])
        self.preview_panel.copy_frame()

    def _act_undo(self) -> None:
        ok, message = self.vm.undo()
        self.statusBar().showMessage(message)
        self.timeline_panel.refresh()
        self.preview_panel.reload_storyboard()
        self._update_undo_actions()

    def _act_redo(self) -> None:
        ok, message = self.vm.redo()
        self.statusBar().showMessage(message)
        self.timeline_panel.refresh()
        self.preview_panel.reload_storyboard()
        self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        undo = self._actions.get("undo")
        redo = self._actions.get("redo")
        if undo is not None:
            undo.setEnabled(bool(self.vm.undo_label()))
            label = self.vm.undo_label()
            undo.setText(f"&Undo {label}" if label else "&Undo Scene Op")
        if redo is not None:
            redo.setEnabled(bool(self.vm.redo_label()))
            label = self.vm.redo_label()
            redo.setText(f"&Redo {label}" if label else "&Redo Scene Op")

    def _act_select_all(self) -> None:
        self.nav_list.setCurrentRow(_NAV["studio"])
        cards = self.timeline_panel.cards
        if not cards:
            self.statusBar().showMessage("No scenes to select.")
            return
        self.timeline_panel.select_scene(cards[-1].scene_number)
        for card in self.timeline_panel.cards:
            card.set_selected(True)
        self.statusBar().showMessage(
            f"{len(cards)} scene(s) selected (last one is the op target)."
        )

    def _act_copy_scene(self) -> None:
        project_id, number = self._scene_context()
        if number is None:
            self.statusBar().showMessage("Select a scene card first.")
            return
        ok, message = self.vm.copy_scene(project_id, number)
        self.statusBar().showMessage(message)

    def _act_paste_scene(self) -> None:
        project_id, number = self._scene_context()
        ok, message = self.vm.paste_scene(project_id, number or 0)
        self._refresh_after_scene_op(message, ok)
        self._update_undo_actions()

    def _act_delete_scene(self) -> None:
        project_id, number = self._scene_context()
        if number is None:
            self.statusBar().showMessage("Select a scene card first.")
            return
        ok, message = self.vm.delete_scene(project_id, number)
        self._refresh_after_scene_op(message, ok)
        self._update_undo_actions()

    # -- Menu: View extras -------------------------------------------------
    def _act_toggle_fullscreen(self) -> None:
        """View → Toggle Fullscreen (F11): honest Qt fullscreen swap."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _act_toggle_inspector(self) -> None:
        """View → Show Inspector: v3.0 dock (draggable/floatable)."""
        self.inspector_dock.setVisible(
            not self.inspector_dock.isVisible())

    def _act_toggle_toolbar(self) -> None:
        self.toolbar.setVisible(not self.toolbar.isVisible())

    def _act_toggle_statusbar(self) -> None:
        self.statusBar().setVisible(not self.statusBar().isVisible())

    # -- Menu: Project extras -----------------------------------------------
    def _act_channel_profiles(self) -> None:
        dialog = ChannelProfileDialog(
            self.vm, on_changed=self._reload_profile_combo, parent=self
        )
        dialog.exec()

    def _reload_profile_combo(self) -> None:
        current = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("(channel default)", None)
        for profile in self.vm.channel_profiles():
            self.profile_combo.addItem(profile["label"], profile["id"])
        index = max(0, self.profile_combo.findData(current))
        self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        if hasattr(self, "toolbar_profile"):
            self._reload_toolbar_profile()

    def _act_quality_check(self) -> None:
        project_id = self.timeline_panel.current_project_id()
        dialog = QualityCheckDialog(self.vm, project_id, self)
        dialog.exec()

    # -- Menu: Render extras -----------------------------------------------
    def _act_quick_preview(self) -> None:
        """F5: jump straight to the latest render in the player."""
        self.nav_list.setCurrentRow(_NAV["studio"])
        self.preview_panel.open_last()

    def _act_resume(self) -> None:
        candidates = self.vm.recovery_candidates()
        if not candidates:
            self.statusBar().showMessage(
                "No interrupted renders to resume."
            )
            return
        dialog = RecoveryDialog(self.vm, candidates, self)
        dialog.exec()
        if dialog.resume_id:
            self._resume_project_render(str(dialog.resume_id))

    def _act_batch_page(self) -> None:
        self.nav_list.setCurrentRow(_NAV["batch"])
        self.batch_panel.refresh()

    def _act_render_settings(self) -> None:
        self.nav_list.setCurrentRow(_NAV["render"])
        self.statusBar().showMessage(
            "Render settings live on the Render form; grade+export "
            "defaults on the Grade page."
        )

    # -- Menu: Tools -------------------------------------------------------
    def _act_voice_store(self) -> None:
        self.nav_list.setCurrentRow(_NAV["voices"])

    def _act_voice_clone(self) -> None:
        dialog = VoiceCloneDialog(self.vm, self)
        dialog.exec()
        if dialog.added:
            self.show_notification("success", "Voice clone queued.")

    def _act_engine_manager(self) -> None:
        dialog = EngineInstallDialog(self.vm, self)
        dialog.exec()

    def _act_setup_wizard(self) -> None:
        """Tools → Setup Wizard… — first-run wizard, re-run any time
        (deep-dive fix #12)."""
        from ui.dialogs.app_dialogs import FirstRunWizard

        FirstRunWizard(self.vm, self).exec()

    def _act_key_generator(self) -> None:
        dialog = KeyGeneratorDialog(self.vm, self)
        dialog.exec()

    def _act_user_guide(self) -> None:
        from ui.dialogs.app_dialogs import UserGuideDialog

        dialog = UserGuideDialog(self)
        dialog.exec()

    def _act_open_logs(self) -> None:
        folder = "logs"
        try:
            if self.vm.container is not None:
                folder = str(
                    self.vm.container.get("config").get("log_folder", "logs")
                )
        except Exception:  # noqa: BLE001 - fall back to ./logs
            folder = "logs"
        path = Path(folder)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_plugins(self) -> None:
        lines: list = []
        status_fn = getattr(self.vm.engine, "get_plugin_status", None)
        if callable(status_fn):
            try:
                reply = status_fn()
                data = (reply or {}).get("data") or {}
                plugins = data.get("plugins")
                if isinstance(plugins, list):
                    for entry in plugins:
                        if not isinstance(entry, dict):
                            lines.append(str(entry))
                            continue
                        name = str(entry.get("name") or "?")
                        state = str(entry.get("status") or "")
                        lines.append(f"{name} — {state}".rstrip(" —"))
            except Exception:  # noqa: BLE001 - advisory dialog
                lines = []
        if not lines:
            count = self.vm.engines_status().get("plugins_loaded", 0)
            if count:
                lines = [f"{count} plugin(s) loaded and healthy."]
            else:
                lines = [
                    "No plugins loaded.",
                    "Drop a BasePlugin subclass into plugins/ to add one.",
                ]
        QMessageBox.information(self, "Plugins", "\n".join(lines))

    def _show_engines(self) -> None:
        QMessageBox.information(
            self, "Engines", self._engines_text(self.vm.engines_status())
        )

    def _show_about(self) -> None:
        QMessageBox.about(self, "About Autopilot", self.vm.about_text())

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self, "Keyboard shortcuts", self.vm.shortcuts_text()
        )

    @staticmethod
    def _path_row(button_text: str, slot: Any) -> tuple:
        edit = QLineEdit()
        button = QPushButton(button_text)
        button.clicked.connect(slot)
        return edit, button

    @staticmethod
    def _wrap(edit: QLineEdit, button: QPushButton) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        row.addWidget(button)
        return holder

    def _browse_script(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Choose script", "",
            "Scripts (*.txt *.pdf *.docx);;All files (*)",
        )
        if path:
            self.script_edit.setText(path)

    def _browse_images(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose images folder")
        if path:
            self.images_edit.setText(path)

    def _browse_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose project folder")
        if path:
            self.project_edit.setText(path)

    # ------------------------------------------------------------------
    # Render control
    # ------------------------------------------------------------------
    def _on_render_clicked(self) -> None:
        if not self.vm.engine_ready():
            self._log("ERROR: engine not ready — restart the app.", error=True)
            return
        ok, message = self.vm.validate_render_inputs(
            self.script_edit.text().strip(),
            self.images_edit.text().strip(),
            self.project_edit.text().strip(),
        )
        if not ok:
            self._log(f"ERROR: {message}", error=True)
            self.statusBar().showMessage(message)
            return
        request = self.vm.build_render_request(
            script_path=self.script_edit.text().strip(),
            images_folder=self.images_edit.text().strip(),
            project_folder=self.project_edit.text().strip(),
            title=self.title_edit.text().strip() or None,
            export_preset=self.preset_combo.currentData(),
            channel_profile_id=self.profile_combo.currentData(),
            quality_gate=self.quality_gate_check.isChecked(),
        )
        self._reset_stages()
        self.progress_bar.setValue(0)
        self._log(f"{_APPEND} starting: {request['title']}")
        self.render_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_panel.set_running(True)
        self._worker = _RenderWorker(self.vm.engine, request)
        self._worker.finished_with_result.connect(self._on_render_finished)
        self._worker.start()

    def _on_cancel_clicked(self) -> None:
        try:
            self.vm.engine.cancel_pipeline()
            self._log(f"{_APPEND} cancel requested…")
        except Exception as exc:  # noqa: BLE001
            self._log(f"ERROR: cancel failed: {exc}", error=True)

    def _on_render_finished(self, result: Dict[str, Any]) -> None:
        self.render_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_panel.set_running(False)
        if result.get("success"):
            output = (result.get("data") or {}).get("output_file_path")
            self.statusBar().showMessage("Render complete")
            self._log(f"{_APPEND} SUCCESS: {output}")
            self.show_notification("success", "Render complete ✓")
            self._show_render_complete(result)
        else:
            error = result.get("error") or "unknown error"
            self.statusBar().showMessage("Render failed")
            self._log(f"{_APPEND} FAILED: {error}", error=True)
            self.show_notification("error", f"Render failed: {error}")
        self._refresh_projects()
        self.timeline_panel.reload_projects()
        self._refresh_statusbar_fields()
        source = self.vm.preview_source()
        if source.get("exists"):
            self.preview_panel.open_source(
                str(source["path"]), str(source.get("title") or "")
            )

    # ------------------------------------------------------------------
    # Live pipeline view (GUI thread — runs via queued signal)
    # ------------------------------------------------------------------
    def _reset_stages(self) -> None:
        self.stage_list.clear()
        self._stage_rows = {}
        for name in self.vm.stage_names():
            item = QListWidgetItem(f"{_STATE_MARK['pending']}  {name}")
            self.stage_list.addItem(item)
            self._stage_rows[name] = item
        self.progress_panel.reset_stages()

    def _on_pipeline_event(self, record: Dict[str, Any]) -> None:
        stage = record.get("stage")
        marks = {
            "pipeline.stage_started": "running",
            "pipeline.stage_completed": "done",
            "pipeline.failed": "failed",
        }
        mark = marks.get(record["event"])
        if mark and stage in self._stage_rows:
            self._stage_rows[stage].setText(f"{_STATE_MARK[mark]}  {stage}")
        if record.get("percent") is not None:
            self.progress_bar.setValue(int(float(record["percent"]) * 10))
        self._log(record["text"], error=(record.get("level") == "error"))
        self.progress_panel.on_record(record)

    def _log(self, line: str, error: bool = False) -> None:
        self.log_pane.appendPlainText(("!! " if error else "") + str(line))

    # ------------------------------------------------------------------
    # Menus / dialogs / data
    # ------------------------------------------------------------------
    def _show_license(self) -> None:
        summary = self.vm.license_summary()
        QMessageBox.information(
            self, "License status",
            f"Status: {summary['status']}\n{summary['message']}",
        )

    def _show_modules(self) -> None:
        count = self.vm.module_count()
        QMessageBox.information(
            self, "Modules", f"{count} engine module(s) loaded and ready."
        )

    def _refresh_projects(self) -> None:
        self.projects_list.clear()
        rows = self.vm.refresh_projects()
        for row in rows:
            title = row.get("title") or row.get("id")
            item = QListWidgetItem(f"{title}  —  {row.get('status')}")
            item.setData(
                Qt.ItemDataRole.UserRole, row.get("project_folder_path")
            )
            self.projects_list.addItem(item)
        if not rows:  # empty state (deep-dive fix #6)
            item = QListWidgetItem(
                "🗂  No projects yet\nPress Ctrl+N to create "
                "your first documentary.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.projects_list.addItem(item)

    def _show_render_complete(self, result: Dict[str, Any]) -> None:
        def _play(path: str) -> None:
            self.nav_list.setCurrentRow(_NAV["studio"])
            self.preview_panel.open_source(path)

        dialog = RenderCompleteDialog(self.vm, result, on_play=_play,
                                      parent=self)
        dialog.exec()
        self._refresh_statusbar_fields()

    # ------------------------------------------------------------------
    # Toasts (spec §17): top-right slide-in, 4s auto-dismiss
    # ------------------------------------------------------------------
    def show_notification(self, level: str, text: str) -> None:
        from PyQt6.QtWidgets import QGraphicsOpacityEffect

        model = notification_model(level, text)
        toast = QLabel(f"{model['icon']}  {model['text']}", self)
        toast.setObjectName(f"toast_{model['level']}")
        toast.setStyleSheet(
            "QLabel { background: #26262f; color: #e8e8ec; border: 1px"
            " solid #e0a458; border-radius: 6px; padding: 8px 14px; }"
        )
        toast.adjustSize()
        offset = 12 + sum(t.height() + 8 for t in self._toasts)
        toast.move(self.width() - toast.width() - 16, -toast.height())
        toast.show()
        self._toasts.append(toast)
        # slide-in: animate down to the top-right corner
        self._slide_step = 0

        def _slide(target=toast, y=offset) -> None:
            current = target.y()
            if current < y:
                target.move(self.width() - target.width() - 16,
                            min(y, current + 14))
                QTimer.singleShot(16, lambda: _slide(target, y))
            else:
                effect = QGraphicsOpacityEffect(target)
                target.setGraphicsEffect(effect)
                effect.setOpacity(0.95)

        _slide(toast, offset)
        QTimer.singleShot(
            int(model["timeout_ms"]),
            lambda: self._dismiss_toast(toast),
        )

    def _dismiss_toast(self, toast: QLabel) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        toast.hide()
        toast.deleteLater()
        # re-stack remaining toasts upward
        offset = 12
        for item in self._toasts:
            item.move(self.width() - item.width() - 16, offset)
            offset += item.height() + 8

    # ------------------------------------------------------------------
    # Boot recovery + first-run wizard (spec workflows)
    # ------------------------------------------------------------------
    def post_boot_checks(self) -> None:
        """Run just after the window appears (called by launch())."""
        self._refresh_statusbar_fields()
        # License Screen (ui_specification §2): only when no active /
        # trial license is on file — never nags licensed users.
        if self.vm.license_screen_needed():
            LicenseScreenDialog(self.vm, self).exec()
            self._refresh_statusbar_fields()
        from ui.dialogs.app_dialogs import FirstRunWizard

        first_run = self.vm.first_run_model()
        if first_run.get("needs_wizard"):
            FirstRunWizard(self.vm, self).exec()
        candidates = self.vm.recovery_candidates()
        if not candidates:
            return
        dialog = RecoveryDialog(self.vm, candidates, self)
        dialog.exec()
        if dialog.resume_id:
            self._resume_project_render(str(dialog.resume_id))

    def _resume_project_render(self, project_id: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.statusBar().showMessage("A render is already running.")
            return
        if not self.vm.engine_ready():
            self._log("ERROR: engine not ready — cannot resume.", error=True)
            return
        engine = self.vm.engine
        self._reset_stages()
        self.progress_bar.setValue(0)
        self._log(f"{_APPEND} resuming interrupted render: {project_id}")
        self.render_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_panel.set_running(True)
        self.nav_list.setCurrentRow(_NAV["render"])
        self._worker = _RenderWorker(
            engine,
            job=lambda: engine.run_project_pipeline(project_id),
        )
        self._worker.finished_with_result.connect(self._on_render_finished)
        self._worker.start()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        try:
            self.vm.window_state_save(
                bytes(self.saveGeometry().toBase64()).decode("ascii"),
                bytes(self.saveState().toBase64()).decode("ascii"),
            )
        except Exception:  # noqa: BLE001 - persistence is best-effort
            pass
        self._unsubscribe()
        if self._worker is not None and self._worker.isRunning():
            try:
                self.vm.engine.cancel_pipeline()
            except Exception:  # noqa: BLE001
                pass
            self._worker.wait(5000)
        super().closeEvent(event)


def launch(ctx: Dict[str, Any]) -> int:
    """Create the QApplication, splash, themed window, run the loop."""
    from ui.theme import apply_theme  # lazy: needs a running Qt env

    viewmodel = UiViewModel(ctx)
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName("Autopilot")
    app.setOrganizationName("Autopilot")
    # Fusion style FIRST: the native Windows style ignores large parts
    # of the stylesheet, which is why the dark theme looked light —
    # Fusion renders every QSS rule (deep-dive fix #1).
    app.setStyle("Fusion")
    # Montserrat everywhere (ui_specification fonts); Qt substitutes
    # the platform default when the family is not installed.
    from PyQt6.QtGui import QFont

    app.setFont(QFont("Montserrat", 10))
    apply_theme(app, viewmodel.current_theme())
    splash = AutopilotSplash(viewmodel.splash_model())
    steps = viewmodel.splash_model()["steps"]
    if owns_app:
        splash.show()
        for index in range(max(len(steps) - 1, 0)):
            splash.show_step(index)
    window = MainWindow(viewmodel)
    if owns_app:
        splash.show_step(len(steps) - 1)
    window.show()
    splash.finish(window)
    if owns_app:
        QTimer.singleShot(0, window.post_boot_checks)
    if owns_app:
        return int(app.exec() == 0)
    return 0  # embedded in an existing app (tests); do not own the loop


__all__ = ["AutopilotSplash", "MainWindow", "UiViewModel", "launch"]
