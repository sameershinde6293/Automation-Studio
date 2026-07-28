"""Scene/image controls panel (v3.0 master spec — Animation).

Per-scene motion controls: animation type, intensity and duration,
written to the real scenes columns through the existing view-model
seams. Colour work stays on the Grade page — a jump button routes
there. This file only paints widgets.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.panels.compact_field import field_row, section_label, wrap_scrollable
from ui.viewmodel import UiViewModel


class SceneControlsPanel(QWidget):
    """v3.0 control panel #4 — animation, intensity, duration."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
        on_structure_changed: Optional[Callable[[], None]] = None,
        open_grade: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._on_structure_changed = on_structure_changed
        self._open_grade = open_grade
        self._scenes: Dict[int, Dict[str, Any]] = {}

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Scene Controls")
        heading.setObjectName("panelTitle")
        self.project_combo = QComboBox()
        self.project_combo.setToolTip("Project whose scenes get motion")
        self.project_combo.currentIndexChanged.connect(
            self.reload_scenes)
        header.addWidget(heading, 1)
        header.addWidget(self.project_combo, 2)
        layout.addLayout(header)

        self.scenes_list = QListWidget()
        self.scenes_list.setToolTip(
            "Pick a scene to edit its motion — right-click for "
            "scene actions")
        self.scenes_list.currentItemChanged.connect(
            self._load_selected)
        self.scenes_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.scenes_list.customContextMenuRequested.connect(
            self._context_menu)
        layout.addWidget(self.scenes_list, 1)

        # ROOT-CAUSE FIX (v3.2.8): scrollable content area for the
        # controls below the scene list — see
        # compact_field.wrap_scrollable for why. The scene list itself
        # stays outside (it has its own internal scrolling already).
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(16)
        motion_card = QFrame()
        motion_card.setObjectName("card")
        motion_layout = QVBoxLayout(motion_card)
        motion_layout.addWidget(section_label("Motion"))
        self.animation_combo = QComboBox()
        self.animation_combo.setToolTip(
            "Camera move painted over the still image")
        self.intensity_combo = QComboBox()
        self.intensity_combo.setToolTip(
            "How far the camera travels over the scene")
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.5, 120.0)
        self.duration_spin.setSingleStep(0.5)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setToolTip(
            "Scene length on the timeline")
        # UI REDESIGN (v3.2.9): was 3 separate full-width rows (one
        # field per line) — grouped onto one line since these are three
        # short, related values, not long text.
        motion_layout.addWidget(field_row(
            ("Animation:", self.animation_combo),
            ("Intensity:", self.intensity_combo),
            ("Duration:", self.duration_spin),
        ))
        content_layout.addWidget(motion_card)

        buttons = QHBoxLayout()
        apply_scene = QPushButton("Apply to Scene")
        apply_scene.setObjectName("primary")
        apply_scene.setToolTip(
            "Save animation, intensity and duration for the "
            "selected scene")
        apply_scene.clicked.connect(self._apply_scene)
        apply_all = QPushButton("Apply Motion to All")
        apply_all.setToolTip(
            "Apply animation + intensity to every scene "
            "(durations stay per-scene)")
        apply_all.clicked.connect(self._apply_all)
        grade_btn = QPushButton("🎨  Grade this scene…")
        grade_btn.setToolTip(
            "Jump to the Grade page for colour work")
        grade_btn.clicked.connect(self._grade)
        buttons.addWidget(apply_scene)
        buttons.addWidget(apply_all)
        buttons.addWidget(grade_btn)
        content_layout.addLayout(buttons)

        self.details_label = QLabel("")
        self.details_label.setObjectName("muted")
        self.details_label.setWordWrap(True)
        content_layout.addWidget(self.details_label)
        note = QLabel(
            "Writes animation_type / animation_intensity / duration "
            "on the scene rows the animation engine reads at render."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        content_layout.addWidget(note)
        content_layout.addStretch(1)
        wrap_scrollable(layout, content)
        self.reload_projects()

    # ------------------------------------------------------------------
    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def current_project_id(self) -> str:
        data = self.project_combo.currentData()
        return str(data) if data else ""

    def selected_number(self) -> Optional[int]:
        item = self.scenes_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def reload_projects(self) -> None:
        current = self.project_combo.currentData()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for row in self.vm.timeline_projects():
            self.project_combo.addItem(row["label"], row["id"])
        if self.project_combo.count():
            index = next(
                (i for i in range(self.project_combo.count())
                 if self.project_combo.itemData(i) == current), 0)
            self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)
        self.reload_scenes()

    def reload_scenes(self) -> None:
        model = self.vm.scene_controls_model(
            self.current_project_id())
        self._scenes = {
            int(s["number"]): s for s in model["scenes"]
            if s.get("number") is not None
        }
        self.animation_combo.clear()
        self.animation_combo.addItems(model["animations"])
        self.intensity_combo.clear()
        self.intensity_combo.addItems(model["intensities"])
        current = self.selected_number()
        self.scenes_list.blockSignals(True)
        self.scenes_list.clear()
        if not model["found"]:
            item = QListWidgetItem(model["empty_text"])
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.scenes_list.addItem(item)
        else:
            for scene in model["scenes"]:
                label = scene["title"] or f"Scene {scene['number']}"
                motion = scene["animation"] or "static"
                item = QListWidgetItem(
                    f"{scene['number']} · {label} — "
                    f"{motion}")
                item.setData(
                    Qt.ItemDataRole.UserRole, scene["number"])
                self.scenes_list.addItem(item)
        self.scenes_list.blockSignals(False)
        if current in self._scenes:
            matches = self.scenes_list.findItems(
                f"{current} ·", Qt.MatchFlag.MatchStartsWith)
            if matches:
                self.scenes_list.setCurrentItem(matches[0])
        self._load_selected()

    def _load_selected(self, *_args: Any) -> None:
        number = self.selected_number()
        scene = self._scenes.get(number) if number is not None else None
        if scene is None:
            self.details_label.setText(
                "Select a scene to edit its motion.")
            return
        index = self.animation_combo.findText(
            str(scene.get("animation") or ""))
        self.animation_combo.setCurrentIndex(max(0, index))
        index = self.intensity_combo.findText(
            str(scene.get("intensity") or ""))
        self.intensity_combo.setCurrentIndex(max(0, index))
        try:
            self.duration_spin.setValue(float(scene["duration"]))
        except (TypeError, ValueError):
            self.duration_spin.setValue(4.0)
        self.details_label.setText(
            f"Scene {scene['number']} · {scene['title'] or 'untitled'}"
            f" · transition: {scene['transition_in'] or 'none'}")

    def _apply_scene(self) -> None:
        number = self.selected_number()
        if number is None:
            self._status("Pick a scene first.")
            return
        project_id = self.current_project_id()
        ok, motion = self.vm.apply_scene_animation(
            project_id, number, self.animation_combo.currentText(),
            self.intensity_combo.currentText())
        ok2, timing = self.vm.apply_scene_duration(
            project_id, number, self.duration_spin.value())
        self._status(f"{motion} {timing}")
        if ok or ok2:
            self.reload_scenes()

    def _apply_all(self) -> None:
        _ok, message = self.vm.apply_scene_animation_all(
            self.current_project_id(),
            self.animation_combo.currentText(),
            self.intensity_combo.currentText())
        self._status(message)
        self.reload_scenes()

    def _grade(self) -> None:
        if self._open_grade is not None:
            self._open_grade()
            self._status(
                "Grade page — the scene is on the timeline there.")

    def _context_menu(self, pos: Any) -> None:
        item = self.scenes_list.itemAt(pos)
        menu = QMenu(self)
        copy_action = menu.addAction("📄  Copy scene")
        paste_action = menu.addAction("📥  Paste scene after")
        delete_action = menu.addAction("🗑  Delete scene")
        chosen = menu.exec(
            self.scenes_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        project_id = self.current_project_id()
        number = item.data(Qt.ItemDataRole.UserRole) if item else None
        if chosen is copy_action:
            ok, message = self.vm.copy_scene(project_id, number)
        elif chosen is paste_action:
            ok, message = self.vm.paste_scene(project_id, number or 0)
        else:
            ok, message = self.vm.delete_scene(project_id, number)
        self._status(message)
        if ok:
            self.reload_scenes()
            if self._on_structure_changed is not None:
                self._on_structure_changed()
