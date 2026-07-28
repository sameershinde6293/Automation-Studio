"""Transitions panel (v3.0 master spec — Transitions).

Pick a transition type + duration and apply it to the selected scenes
or to the whole project. Values land in the real scenes columns
(transition_in / transition_out / transition_duration) through the
view-model's existing seams — this file only paints widgets.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
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

from ui.panels.compact_field import section_label, wrap_scrollable
from ui.viewmodel import UiViewModel


class TransitionsPanel(QWidget):
    """v3.0 control panel #2 — type, duration, apply-to-all."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
        on_structure_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._on_structure_changed = on_structure_changed
        self._scenes: Dict[int, Dict[str, Any]] = {}

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Transitions")
        heading.setObjectName("panelTitle")
        self.project_combo = QComboBox()
        self.project_combo.setToolTip("Project whose scenes get dressed")
        self.project_combo.currentIndexChanged.connect(
            self.reload_scenes)
        header.addWidget(heading, 1)
        header.addWidget(self.project_combo, 2)
        layout.addLayout(header)

        self.scenes_list = QListWidget()
        self.scenes_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.scenes_list.setToolTip(
            "Scenes of the project — multi-select with Ctrl/Shift, "
            "right-click for scene actions")
        self.scenes_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.scenes_list.customContextMenuRequested.connect(
            self._context_menu)
        # Review fix (3.0.2): picking a scene loads its transition.
        self.scenes_list.currentItemChanged.connect(
            self._load_selected)
        layout.addWidget(self.scenes_list, 1)

        # ROOT-CAUSE FIX (v3.2.8): scrollable content area for the
        # controls below the scene list — see
        # compact_field.wrap_scrollable for why.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)

        controls_card = QFrame()
        controls_card.setObjectName("card")
        controls_col = QVBoxLayout(controls_card)
        controls_col.addWidget(section_label("Apply Transition"))
        controls = QHBoxLayout()
        self.type_combo = QComboBox()
        self.type_combo.setToolTip("Transition type applied to scenes")
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.1, 5.0)
        self.duration_spin.setSingleStep(0.1)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setToolTip("Transition duration")
        self.apply_selected = QPushButton("Apply to Selected")
        self.apply_selected.setObjectName("primary")
        self.apply_selected.clicked.connect(self._apply_selected)
        self.apply_all = QPushButton("Apply to All Scenes")
        self.apply_all.clicked.connect(self._apply_all)
        controls.addWidget(QLabel("Type:"))
        controls.addWidget(self.type_combo, 1)
        controls.addWidget(QLabel("Duration:"))
        controls.addWidget(self.duration_spin)
        controls.addWidget(self.apply_selected)
        controls.addWidget(self.apply_all)
        controls_col.addLayout(controls)
        content_layout.addWidget(controls_card)

        note = QLabel(
            "Writes the scene's transition_in / transition_out columns "
            "the transition engine reads at render. Favourites and "
            "presets come from config/transition_presets.json."
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
        self.reload_types()
        self.reload_scenes()

    def reload_types(self) -> None:
        self.type_combo.clear()
        for item in self.vm.transitions_model(
                self.current_project_id())["types"]:
            self.type_combo.addItem(item["label"], item["id"])

    def reload_scenes(self) -> None:
        model = self.vm.transitions_model(self.current_project_id())
        self._scenes = {
            int(s["number"]): s for s in model["scenes"]
            if s.get("number") is not None
        }
        # Review fix (3.0.3): selection survives every reload —
        # capture, rebuild with signals blocked, restore, resync.
        keep = self.selected_number()
        self.scenes_list.blockSignals(True)
        self.scenes_list.clear()
        if not model["found"]:
            item = QListWidgetItem(model["empty_text"])
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.scenes_list.addItem(item)
            self.scenes_list.blockSignals(False)
            return
        for scene in model["scenes"]:
            label = scene["title"] or f"Scene {scene['number']}"
            current = scene["transition_in"] or "none"
            item = QListWidgetItem(
                f"{scene['number']} · {label} — "
                f"{current} {scene['transition_duration']}s")
            item.setData(Qt.ItemDataRole.UserRole, scene["number"])
            self.scenes_list.addItem(item)
        self.scenes_list.blockSignals(False)
        if keep in self._scenes:
            matches = self.scenes_list.findItems(
                f"{keep} ·", Qt.MatchFlag.MatchStartsWith)
            if matches:
                self.scenes_list.setCurrentItem(matches[0])
        self._load_selected()

    def selected_number(self) -> Optional[int]:
        item = self.scenes_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _load_selected(self, *_args: Any) -> None:
        """Selected scene -> type/duration widgets (3.0.2 review)."""
        number = self.selected_number()
        scene = self._scenes.get(number) if number is not None else None
        if scene is None:
            return
        index = self.type_combo.findData(
            str(scene.get("transition_in") or ""))
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        try:
            self.duration_spin.setValue(
                float(scene.get("transition_duration") or 0.8))
        except (TypeError, ValueError):
            self.duration_spin.setValue(0.8)

    def _selected_numbers(self) -> List[int]:
        numbers: List[int] = []
        for item in self.scenes_list.selectedItems():
            value = item.data(Qt.ItemDataRole.UserRole)
            if value is not None:
                try:
                    numbers.append(int(value))
                except (TypeError, ValueError):
                    continue
        return numbers

    def _apply_selected(self) -> None:
        _ok, message = self.vm.apply_transition(
            self.current_project_id(), self._selected_numbers(),
            str(self.type_combo.currentData()),
            self.duration_spin.value())
        self._status(message)
        self.reload_scenes()

    def _apply_all(self) -> None:
        _ok, message = self.vm.apply_transition(
            self.current_project_id(), [],
            str(self.type_combo.currentData()),
            self.duration_spin.value(), apply_all=True)
        self._status(message)
        self.reload_scenes()

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
