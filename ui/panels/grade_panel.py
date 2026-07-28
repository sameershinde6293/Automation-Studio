"""Color-grade panel (ui_specification.txt Section 10).

Review fix (3.0.1): this page now hosts ONLY the grading controls —
per-scene animation moved to the Scenes page, transitions to the
Transitions page and the export profile to the Export page (v3.0
sidebar). What remains writes REAL values the pipeline consumes:
grade preset -> projects.color_grade_preset; custom sliders (+LUT
+opacity) -> scenes.color_grade_override JSON.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ui.panels.compact_field import build_slider_field, section_label, wrap_scrollable
from ui.viewmodel import UiViewModel


class GradePanel(QWidget):
    """Color grading only — presets, custom sliders, LUT."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        header = QHBoxLayout()
        heading = QLabel("Color Grade")
        heading.setObjectName("panelTitle")
        self.project_combo = QComboBox()
        self.project_combo.currentIndexChanged.connect(
            self._project_changed)
        header.addWidget(heading, 1)
        header.addWidget(self.project_combo, 2)
        layout.addLayout(header)

        # ROOT-CAUSE FIX (v3.2.8): everything below the header now lives
        # in a scrollable content area — see compact_field.wrap_scrollable
        # for why. Without this, a window shorter than the content needs
        # gets its rows forcibly compressed by Qt instead of scrolling,
        # which is what produced the overlapping/unreadable text.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(16)
        content_layout.setContentsMargins(0, 0, 4, 0)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip(
            "Six studio looks; applied to the whole project at render")
        for preset in self.vm.color_presets():
            self.preset_combo.addItem(preset["label"], preset["id"])
        apply_preset = QPushButton("Apply preset to project")
        apply_preset.clicked.connect(self._apply_preset)
        preset_row.addWidget(QLabel("Preset:"))
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(apply_preset)
        content_layout.addLayout(preset_row)

        # Sliders card: a 2-column grid of compact, fixed-width fields
        # instead of one full-width column — this is the core fix for
        # the "sliders are very big" feedback.
        sliders_card = QFrame()
        sliders_card.setObjectName("card")
        grid = QGridLayout(sliders_card)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(14)
        section = section_label("Adjustments")
        grid.addWidget(section, 0, 0, 1, 2)
        self.sliders: Dict[str, QSlider] = {}
        self.slider_labels: Dict[str, QLabel] = {}
        specs = self.vm.grade_sliders()
        columns = 2
        for index, spec in enumerate(specs):
            wrapper, slider, value_label = build_slider_field(
                spec["label"], spec["min"], spec["max"], spec["default"]
            )
            slider.valueChanged.connect(
                lambda v, lbl=value_label: lbl.setText(str(v))
            )
            row = 1 + index // columns
            col = index % columns
            grid.addWidget(wrapper, row, col)
            self.sliders[spec["key"]] = slider
            self.slider_labels[spec["key"]] = value_label
        content_layout.addWidget(sliders_card)

        # LUT card: same compact-field treatment.
        lut_card = QFrame()
        lut_card.setObjectName("card")
        lut_layout = QVBoxLayout(lut_card)
        lut_section = section_label("LUT")
        lut_layout.addWidget(lut_section)
        lut_file_row = QHBoxLayout()
        self.lut_combo = QComboBox()
        self.lut_combo.setToolTip(
            ".cube LUT from config/luts — blended at LUT opacity")
        self.lut_combo.addItem("(none)", "")
        for name in self.vm.lut_files():
            self.lut_combo.addItem(name, name)
        lut_file_row.addWidget(QLabel("File:"))
        lut_file_row.addWidget(self.lut_combo, 1)
        lut_layout.addLayout(lut_file_row)
        lut_wrapper, self.lut_opacity, self.lut_opacity_label = (
            build_slider_field("Opacity", 0, 100, 80)
        )
        self.lut_opacity_label.setText("80%")
        self.lut_opacity.valueChanged.connect(
            lambda v: self.lut_opacity_label.setText(f"{v}%")
        )
        lut_layout.addWidget(lut_wrapper)
        content_layout.addWidget(lut_card)

        actions_row = QHBoxLayout()
        apply_all = QPushButton("Apply to All Scenes")
        apply_all.setObjectName("primary")
        apply_all.clicked.connect(self._apply_to_all)
        revert = QPushButton("↺ Revert")
        revert.setObjectName("revertBtn")
        revert.setToolTip(
            "Reset sliders, LUT and preset to the saved/default grade "
            "values")
        revert.clicked.connect(self._on_revert)
        actions_row.addWidget(apply_all, 1)
        actions_row.addWidget(revert)
        content_layout.addLayout(actions_row)

        note = QLabel(
            "Grade presets apply project-wide at render. Custom "
            "slider + LUT overrides are stored per scene and applied "
            "where the grade engine supports them. Motion lives on "
            "the Scenes page, cut styling on Transitions, and codec "
            "choices on Export."
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
                 if self.project_combo.itemData(i) == current),
                0,
            )
            self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)

    def _project_changed(self) -> None:
        pass  # grading flow needs no per-project widget reload

    def _slider_values(self) -> Dict[str, int]:
        return {
            key: slider.value() for key, slider in self.sliders.items()
        }

    def _on_revert(self) -> None:
        answer = QMessageBox.question(
            self, "Revert color grade",
            "Revert to the saved/default grade values?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            specs = self.vm.grade_sliders()
        except Exception:  # noqa: BLE001
            specs = []
        for spec in specs:
            slider = self.sliders.get(str(spec.get("key")))
            if slider is not None:
                try:
                    slider.setValue(int(spec.get("default") or 0))
                except (TypeError, ValueError):
                    slider.setValue(0)
        self.lut_combo.setCurrentIndex(0)
        self.lut_opacity.setValue(80)
        if self.preset_combo.count():
            self.preset_combo.setCurrentIndex(0)
        self._status("Reverted to the default grade values.")

    def _apply_preset(self) -> None:
        project_id = self.current_project_id()
        if not project_id:
            self._status("Pick a project first.")
            return
        ok, message = self.vm.apply_grade_preset(
            project_id, str(self.preset_combo.currentData() or "")
        )
        self._status(message)

    def _apply_to_all(self) -> None:
        project_id = self.current_project_id()
        if not project_id:
            self._status("Pick a project first.")
            return
        override = self.vm.grade_override(
            self._slider_values(),
            str(self.lut_combo.currentData() or ""),
            self.lut_opacity.value(),
        )
        ok, message = self.vm.apply_grade_to_all(project_id, override)
        self._status(message)
