"""Preview panel with TABS (ui_specification.txt Section 8).

Tab 1 [Preview]: video player with play/pause/seek/rate/volume +
    copy-frame, and a scene info bar under the player that shows
    which scene is on screen (from DB timings).
Tab 2 [Storyboard]: thumbnail grid of ALL scenes of the project;
    clicking a card selects it and opens the Scene Details tab.
Tab 3 [Scene Details]: full info rows for the selected scene.

PyQt6.QtMultimedia stays defensive (fallback pane when absent).
Space toggles play/pause only while this panel owns focus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui.viewmodel import PLAYBACK_RATES, UiViewModel

try:  # multimedia backend — absent on some bare dev machines
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QVideoWidget

    MULTIMEDIA_AVAILABLE = True
except ImportError:  # honest degradation, see _build_fallback
    QAudioOutput = QMediaPlayer = QVideoWidget = None  # type: ignore
    MULTIMEDIA_AVAILABLE = False


class _StoryboardCard(QLabel):
    """One clickable thumbnail cell in the Storyboard grid."""

    def __init__(self, card: Dict[str, Any], on_pick: Callable[[int], None]) -> None:
        super().__init__()
        self._number = int(card.get("number") or 0)
        self._on_pick = on_pick
        from PyQt6.QtGui import QPixmap

        thumb = card.get("thumb_path")
        pixmap = (
            QPixmap(str(thumb))
            if thumb and Path(str(thumb)).is_file()
            else QPixmap()
        )
        title = card.get("title") or f"Scene {self._number}"
        caption = (
            f"#{self._number:02d} {title}\n{card.get('duration', 0):.0f}s"
            f" · {card.get('status', '')}"
        )
        if pixmap.isNull():
            self.setText(f"🖼\n{caption}")
        else:
            self.setText(caption)
            self.setPixmap(
                pixmap.scaled(
                    150, 84, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.setObjectName("sceneCard")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(150, 110)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt
        if self._on_pick is not None:
            self._on_pick(self._number)
        super().mousePressEvent(event)


class PreviewPanel(QWidget):
    """Tabbed preview: player / storyboard / scene details."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._player: Optional[Any] = None
        self._audio: Optional[Any] = None
        self._duration_ms = 0
        self._loaded_path: Optional[str] = None
        self._scenes: List[Dict[str, Any]] = []
        self._project_id: Optional[str] = None
        self._selected_scene = 1

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Preview")
        heading.setObjectName("panelTitle")
        self.story_project_combo = QComboBox()
        self.story_project_combo.currentIndexChanged.connect(
            self.reload_storyboard
        )
        header.addWidget(heading, 1)
        header.addWidget(self.story_project_combo, 2)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_preview_tab(), "Preview")
        self.tabs.addTab(self._build_storyboard_tab(), "Storyboard")
        self.tabs.addTab(self._build_details_tab(), "Scene Details")
        layout.addWidget(self.tabs, 1)

        space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        space.activated.connect(self.toggle)
        self.reload_projects()

    # ==================================================================
    # Tab 1 — Preview (player + transport + scene info bar)
    # ==================================================================
    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        if MULTIMEDIA_AVAILABLE:
            self._build_player(layout)
        else:
            self._build_fallback(layout)
        # Scene info bar (spec §8): what scene is on screen right now.
        self.scene_bar = QLabel("Scene info: load a project render.")
        self.scene_bar.setObjectName("muted")
        self.scene_bar.setWordWrap(True)
        layout.addWidget(self.scene_bar)
        self.file_label = QLabel(
            "🎞  No media loaded — drop files into the import "
            "zones on the left, or open a project.")
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)
        self._build_transport(layout)
        return tab

    def _build_player(self, layout: QVBoxLayout) -> None:
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(220)
        layout.addWidget(self.video_widget, 1)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self.video_widget)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.errorOccurred.connect(
            lambda _err, msg="": self._status(
                msg or "Playback error (codec missing?)."
            )
        )

    def _build_fallback(self, layout: QVBoxLayout) -> None:
        notice = QLabel(
            "In-app playback unavailable: PyQt6 multimedia backend "
            "missing.\nVideos still open in your system player."
        )
        notice.setObjectName("muted")
        notice.setWordWrap(True)
        layout.addWidget(notice, 1)

    def _build_transport(self, layout: QVBoxLayout) -> None:
        transport = QHBoxLayout()
        self.play_button = QPushButton("►")
        self.play_button.setObjectName("primary")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle)
        self.stop_button = QPushButton("■")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderMoved.connect(self._seek_percent)
        self.time_label = QLabel("0:00 / 0:00")
        self.rate_combo = QComboBox()
        for rate in PLAYBACK_RATES:
            self.rate_combo.addItem(f"{rate:g}×", rate)
        self.rate_combo.setCurrentIndex(1)  # 1.0×
        self.rate_combo.currentIndexChanged.connect(self._apply_rate)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setMaximumWidth(90)
        self.volume_slider.valueChanged.connect(self._apply_volume)
        self.frame_button = QPushButton("Copy frame")
        self.frame_button.clicked.connect(self.copy_frame)
        self.last_button = QPushButton("Open last render")
        self.last_button.clicked.connect(self.open_last)
        transport.addWidget(self.play_button)
        transport.addWidget(self.stop_button)
        transport.addWidget(self.seek_slider, 1)
        transport.addWidget(self.time_label)
        transport.addWidget(self.rate_combo)
        transport.addWidget(self.volume_slider)
        transport.addWidget(self.frame_button)
        transport.addWidget(self.last_button)
        layout.addLayout(transport)
        if not MULTIMEDIA_AVAILABLE:
            for widget in (
                self.play_button, self.stop_button, self.seek_slider,
                self.rate_combo, self.volume_slider, self.frame_button,
            ):
                widget.setEnabled(False)

    # ==================================================================
    # Tab 2 — Storyboard (thumbnail grid)
    # ==================================================================
    def _build_storyboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.story_summary = QLabel("Pick a project to see its scenes.")
        self.story_summary.setObjectName("muted")
        layout.addWidget(self.story_summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.story_host = QWidget()
        self.story_grid = QGridLayout(self.story_host)
        self.story_grid.setSpacing(8)
        scroll.setWidget(self.story_host)
        layout.addWidget(scroll, 1)
        return tab

    def reload_projects(self) -> None:
        current = self.story_project_combo.currentData()
        self.story_project_combo.blockSignals(True)
        self.story_project_combo.clear()
        for row in self.vm.timeline_projects():
            self.story_project_combo.addItem(row["label"], row["id"])
        if self.story_project_combo.count():
            index = next(
                (i for i in range(self.story_project_combo.count())
                 if self.story_project_combo.itemData(i) == current),
                0,
            )
            self.story_project_combo.setCurrentIndex(index)
        self.story_project_combo.blockSignals(False)
        self.reload_storyboard()

    def reload_storyboard(self) -> None:
        while self.story_grid.count():
            item = self.story_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        project_id = self.story_project_combo.currentData()
        self._project_id = str(project_id) if project_id else None
        model = self.vm.storyboard_model(self._project_id or "")
        self._scenes = []
        if not model.get("found"):
            self.story_summary.setText(
                "Pick a project — scene thumbnails appear after render."
            )
            model2 = self.vm.timeline_model(self._project_id or "")
            self._scenes = list(model2.get("scenes") or [])
            self._refresh_details()
            return
        full = self.vm.timeline_model(self._project_id or "")
        self._scenes = list(full.get("scenes") or [])
        self.story_summary.setText(
            f"{model['count']} scene(s) — click one for details."
        )
        for index, card in enumerate(model["cards"]):
            cell = _StoryboardCard(card, self._on_story_pick)
            self.story_grid.addWidget(cell, index // 3, index % 3)
        self._refresh_details()

    def _on_story_pick(self, scene_number: int) -> None:
        self._selected_scene = int(scene_number)
        self._refresh_details()
        self.tabs.setCurrentIndex(2)  # Scene Details

    # ==================================================================
    # Tab 3 — Scene Details
    # ==================================================================
    def _build_details_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.details_title = QLabel("Scene Details")
        self.details_title.setObjectName("panelTitle")
        layout.addWidget(self.details_title)
        self.details_labels: List[QLabel] = []
        # Review fix (3.0.3): details model yields 7 rows total —
        # rows[0] is the title, leaving exactly 6 labels (was one
        # permanently-blank row).
        for _unused in range(6):
            row = QLabel("")
            row.setWordWrap(True)
            self.details_labels.append(row)
            layout.addWidget(row)
        self.details_lines = QLabel("")
        self.details_lines.setObjectName("muted")
        self.details_lines.setWordWrap(True)
        layout.addWidget(self.details_lines)
        layout.addStretch(1)
        return tab

    def _refresh_details(self) -> None:
        model = self.vm.scene_details_model(
            self._project_id or "", self._selected_scene
        )
        if not model.get("found"):
            self.details_title.setText("Scene Details — nothing selected")
            for label in self.details_labels:
                label.setText("")
            self.details_lines.setText(
                "Select a scene in the Storyboard tab."
            )
            return
        self.details_title.setText(
            f"Scene Details — {model['rows'][0][1]}"
        )
        for label, (name, value) in zip(
            self.details_labels, model["rows"][1:]
        ):
            label.setText(f"{name}:  {value}")
        snippets = [
            f"{line['character']}: {line['text'][:90]}"
            for line in model.get("lines", [])[:4]
        ]
        self.details_lines.setText("\n".join(snippets))

    # ==================================================================
    # Loading / transport (unchanged engine-facing surface)
    # ==================================================================
    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def open_source(self, path: str, title: str = "") -> bool:
        path = str(path or "")
        if not path or not Path(path).is_file():
            self._status(f"Preview file missing: {path or '(none)'}")
            return False
        self._loaded_path = path
        label = f"{title} — {path}" if title else path
        self.file_label.setText(label)
        self.reload_projects()
        if MULTIMEDIA_AVAILABLE:
            self.stop()
            self._player.setSource(QUrl.fromLocalFile(path))
            self._apply_rate()
            self._apply_volume()
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        self._status(f"Preview: {label}")
        return True

    def open_last(self) -> None:
        source = self.vm.preview_source()
        if not source.get("path") or not source.get("exists"):
            self._status("No finished render to preview yet.")
            return
        self.tabs.setCurrentIndex(0)
        self.open_source(str(source["path"]), str(source.get("title") or ""))

    def browse_and_open(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Open video", "", "Video (*.mp4 *.mov *.mkv *.webm);;All files (*)"
        )
        if path:
            self.open_source(path)

    def open_externally(self) -> None:
        if self._loaded_path:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(self._loaded_path)
            )

    def toggle(self) -> None:
        if not MULTIMEDIA_AVAILABLE or self._player is None:
            self._status("Playback unavailable (multimedia missing).")
            return
        if not self._loaded_path:
            self.open_last()
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def stop(self) -> None:
        if MULTIMEDIA_AVAILABLE and self._player is not None:
            self._player.stop()

    def _seek_percent(self, value: int) -> None:
        if self._player is not None and self._duration_ms > 0:
            self._player.setPosition(int(self._duration_ms * value / 1000))

    def _apply_rate(self) -> None:
        if self._player is not None:
            self._player.setPlaybackRate(
                float(self.rate_combo.currentData() or 1.0)
            )

    def _apply_volume(self) -> None:
        if self._audio is not None:
            self._audio.setVolume(self.volume_slider.value() / 100.0)

    def _on_duration(self, duration_ms: int) -> None:
        self._duration_ms = int(duration_ms or 0)

    def _on_position(self, position_ms: int) -> None:
        state = self.vm.transport_state(
            position_ms / 1000.0, self._duration_ms / 1000.0,
            playing=self._is_playing(),
        )
        self.time_label.setText(state["position_text"])
        self.play_button.setText(state["play_label"])
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(int(state["percent"] * 10))
        scene = self.vm.scene_at_position(
            self._scenes, position_ms / 1000.0
        )
        if scene:
            title = scene.get("title") or f"Scene {scene['number']}"
            self.scene_bar.setText(
                f"On screen: Scene {scene['number']} · {title}"
                f" · starts {scene.get('start_text', '0:00')}"
            )

    def _is_playing(self) -> bool:
        return bool(
            MULTIMEDIA_AVAILABLE
            and self._player is not None
            and self._player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        )

    def copy_frame(self) -> None:
        """Grab the current video frame to the clipboard (Ctrl+Shift+C)."""
        if not MULTIMEDIA_AVAILABLE:
            self._status("Copy frame unavailable (multimedia missing).")
            return
        from PyQt6.QtGui import QGuiApplication

        sink = self.video_widget.videoSink() if self.video_widget else None
        frame = sink.videoFrame() if sink is not None else None
        if frame is None or not frame.isValid():
            self._status("No frame to copy yet — start playback first.")
            return
        image = frame.toImage()
        if image.isNull():
            self._status("Could not convert the current frame.")
            return
        QGuiApplication.clipboard().setImage(image)
        self._status("Frame copied to clipboard.")
