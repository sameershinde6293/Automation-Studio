"""Audio panel (ui_specification.txt Section 11).

Narration volume (+preview), background music file + volume,
auto-ducking controls (on/off now; depth/ceiling/attack/release
saved for the mixer), SFX volume and master output level. Volumes
and the music path write the REAL projects columns the audio mix
stage consumes; ducking writes app settings keys.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ui.panels.compact_field import section_label, wrap_scrollable
from ui.panels.waveform_widget import WaveformWidget
from ui.viewmodel import UiViewModel


class AudioPanel(QWidget):
    """Per-project audio design saved to real DB columns/settings."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
        preview_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._preview_callback = preview_callback
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Audio")
        heading.setObjectName("panelTitle")
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(200)  # 3.0.9
        self.project_combo.currentIndexChanged.connect(self.reload_settings)
        header.addWidget(heading, 1)
        header.addWidget(self.project_combo, 2)
        layout.addLayout(header)

        # ROOT-CAUSE FIX (v3.2.8): scrollable content area — see
        # compact_field.wrap_scrollable for why.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(16)

        levels_card = QFrame()
        levels_card.setObjectName("card")
        form = QFormLayout(levels_card)
        form.addRow(section_label("Narration & Music"))
        self.narration_slider, self.narration_label = self._slider_row()
        narration_row = QHBoxLayout()
        narration_row.addWidget(self.narration_slider)
        narration_row.addWidget(self.narration_label)
        self.mute_narration = QCheckBox("Mute")
        self.mute_narration.setToolTip("Mute narration in the next mix")
        narration_row.addWidget(self.mute_narration)
        self.narration_preview = QPushButton("Preview")
        self.narration_preview.clicked.connect(self._preview_narration)
        narration_row.addWidget(self.narration_preview)
        narration_row.addStretch(1)
        form.addRow("Narration volume:", narration_row)

        self.music_edit = QLineEdit()
        self.music_edit.setMinimumWidth(300)  # 3.0.9
        self.music_edit.setPlaceholderText("(no background music)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_music)
        music_row = QHBoxLayout()
        music_row.addWidget(self.music_edit, 1)
        music_row.addWidget(browse)
        form.addRow("Music file:", music_row)
        self.music_slider, self.music_label = self._slider_row()
        m_row = QHBoxLayout()
        m_row.addWidget(self.music_slider)
        m_row.addWidget(self.music_label)
        self.mute_music = QCheckBox("Mute")
        self.mute_music.setToolTip("Mute music in the next mix")
        m_row.addWidget(self.mute_music)
        m_row.addStretch(1)
        form.addRow("Music volume:", m_row)
        content_layout.addWidget(levels_card)

        sfx_ducking_card = QFrame()
        sfx_ducking_card.setObjectName("card")
        sfx_col = QVBoxLayout(sfx_ducking_card)
        sfx_col.addWidget(section_label("SFX & Ducking"))
        sfx_form = QFormLayout()
        self.sfx_slider, self.sfx_label = self._slider_row()
        s_row = QHBoxLayout()
        s_row.addWidget(self.sfx_slider)
        s_row.addWidget(self.sfx_label)
        self.mute_sfx = QCheckBox("Mute")
        self.mute_sfx.setToolTip("Mute SFX in the next mix")
        s_row.addWidget(self.mute_sfx)
        s_row.addStretch(1)
        sfx_form.addRow("SFX volume:", s_row)
        self.ducking_check = QCheckBox("Auto-duck music under narration")
        sfx_form.addRow("Ducking:", self.ducking_check)
        sfx_col.addLayout(sfx_form)

        duck_grid = QGridLayout()
        duck_grid.setHorizontalSpacing(20)
        duck_grid.setVerticalSpacing(10)
        self.duck_sliders: Dict[str, QSlider] = {}
        self.duck_labels: Dict[str, QLabel] = {}
        for index, (key, label) in enumerate((
            ("ducking_depth", "Duck depth"),
            ("ducking_ceiling", "Duck ceiling"),
            ("ducking_attack", "Attack"),
            ("ducking_release", "Release"),
        )):
            slider, value_label = self._slider_row()
            self.duck_sliders[key] = slider
            self.duck_labels[key] = value_label
            field = QWidget()
            field_col = QVBoxLayout(field)
            field_col.setContentsMargins(0, 0, 0, 0)
            field_col.setSpacing(4)
            field_header = QHBoxLayout()
            name_lbl = QLabel(label)
            name_lbl.setObjectName("fieldLabel")
            field_header.addWidget(name_lbl, 1)
            field_header.addWidget(value_label)
            field_col.addLayout(field_header)
            field_col.addWidget(slider)
            duck_grid.addWidget(field, index // 2, index % 2)
        sfx_col.addLayout(duck_grid)
        content_layout.addWidget(sfx_ducking_card)

        fades_master_card = QFrame()
        fades_master_card.setObjectName("card")
        fm_form = QFormLayout(fades_master_card)
        fm_form.addRow(section_label("Fades & Master"))
        # Fades (ui fix #21): applied to the music bed at render.
        self.fade_spins: Dict[str, QDoubleSpinBox] = {}
        for key, label, default in (
            ("fade_in_seconds", "Music fade-in", 1.5),
            ("fade_out_seconds", "Music fade-out", 2.0),
        ):
            spin = QDoubleSpinBox()
            spin.setMinimumWidth(100)  # 3.0.9
            spin.setRange(0.0, 30.0)
            spin.setSingleStep(0.5)
            spin.setSuffix(" s")
            spin.setValue(default)
            spin.setToolTip(f"{label} applied to the music bed "
                            "at the next render")
            self.fade_spins[key] = spin
            fm_form.addRow(f"{label}:", spin)

        self.master_slider, self.master_label = self._slider_row()
        master_row = QHBoxLayout()
        master_row.addWidget(self.master_slider)
        master_row.addWidget(self.master_label)
        master_row.addStretch(1)
        fm_form.addRow("Master output:", master_row)
        content_layout.addWidget(fades_master_card)

        # v3.0 #18: painted peaks from the mixed narration WAV.
        wave_heading = QLabel("Narration waveform")
        wave_heading.setObjectName("h2")
        content_layout.addWidget(wave_heading)
        self.waveform = WaveformWidget(self)
        content_layout.addWidget(self.waveform)
        self.waveform.seekRequested.connect(self._seek_requested)
        wave_load = QPushButton("↻  Load narration waveform")
        wave_load.setToolTip(
            "Read the mixed narration WAV and draw its peaks "
            "(available after the audio mix stage)")
        wave_load.clicked.connect(self._load_waveform)
        content_layout.addWidget(wave_load)

        save = QPushButton("Save audio settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        content_layout.addWidget(save)
        revert = QPushButton("↺ Revert")
        revert.setObjectName("revertBtn")
        revert.setToolTip(
            "Discard unsaved edits and reload the saved values")
        revert.clicked.connect(self._on_revert)
        content_layout.addWidget(revert)
        note = QLabel(
            "Volumes + music path write the project's real columns "
            "(mixed at next render). Ducking on/off applies today; the "
            "fine-tuning sliders and fade times are saved for the "
            "mixer pipeline."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        content_layout.addWidget(note)
        content_layout.addStretch(1)
        wrap_scrollable(layout, content)
        self.reload_projects()

    # ------------------------------------------------------------------
    @staticmethod
    def _slider_row() -> tuple:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 200)
        slider.setValue(100)
        # UI REDESIGN (v3.2.7): was unbounded — every row.addWidget(
        # slider, 1) stretched it across the whole panel width. Capped
        # it here fixes every slider built through this helper (all 8
        # in this panel) without touching the value/label contract that
        # reload_settings()/_state() depend on elsewhere in this file.
        # BUGFIX (v3.2.10): 220px was too aggressive a cap — real
        # screenshots showed a large dead empty gap after the slider on
        # a normal-width panel. Raised so the slider actually uses a
        # reasonable share of the row instead of sitting in a small
        # corner of it.
        slider.setMaximumWidth(420)
        label = QLabel("100%")
        # 3.0.7: bold readable value, fixed room (sliderValue QSS)
        label.setObjectName("sliderValue")
        label.setMinimumWidth(60)
        label.setStyleSheet("font-weight: bold; font-size: 13px;")
        # 3.0.9: value readable at a glance
        slider.valueChanged.connect(
            lambda v, lbl=label: lbl.setText(f"{v}%")
        )
        return slider, label

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
        self.reload_settings()

    def reload_settings(self) -> None:
        model = self.vm.audio_settings(self.current_project_id())
        self.narration_slider.setValue(int(model["narration_volume"]))
        self.music_slider.setValue(int(model["music_volume"]))
        self.sfx_slider.setValue(int(model["sfx_volume"]))
        self.music_edit.setText(str(model["music_file_path"] or ""))
        self.ducking_check.setChecked(bool(model["ducking_enabled"]))
        self.mute_narration.setChecked(bool(model["mute_narration"]))
        self.mute_music.setChecked(bool(model["mute_music"]))
        self.mute_sfx.setChecked(bool(model["mute_sfx"]))
        for key, slider in self.duck_sliders.items():
            try:
                slider.setValue(int(model.get(key) or 50))
            except (TypeError, ValueError):
                slider.setValue(50)
        for key, spin in self.fade_spins.items():
            try:
                spin.setValue(float(model.get(key) or 0.0))
            except (TypeError, ValueError):
                spin.setValue(0.0)
        # Review fix (3.0.2): master volume round-trips — it was
        # written on save but never read back, resetting silently.
        try:
            master = float(model.get("master_volume") or 1.0)
        except (TypeError, ValueError):
            master = 1.0
        self.master_slider.setValue(
            max(0, min(200, int(round(master * 100)))))

    def _browse_music(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Choose background music", "",
            "Audio (*.mp3 *.wav);;All files (*)",
        )
        if path:
            self.music_edit.setText(path)

    def _preview_narration(self) -> None:
        model = self.vm.waveform_model(self.current_project_id())
        path = model.get("path")
        if path and self._preview_callback is not None:
            self._preview_callback(str(path))
            return
        self._status(
            "No narration mix to preview yet — it exists after the "
            "audio mix stage runs."
        )

    def _on_revert(self) -> None:
        answer = QMessageBox.question(
            self, "Revert audio settings",
            "Revert to the last saved values for this project?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.reload_settings()
        self._status("Reverted to the last saved audio settings.")

    def _seek_requested(self, seconds: float) -> None:
        try:
            total = max(0.0, float(seconds))
        except (TypeError, ValueError):
            total = 0.0
        stamp = f"{int(total // 60)}:{int(total % 60):02d}"
        self._status(
            f"Seek requested at {stamp} — scrubbing wires up with "
            "the playback update.")

    def _load_waveform(self) -> None:
        model = self.vm.waveform_model(self.current_project_id())
        path = model.get("path")
        if not path:
            self.waveform.set_message(
                "Narration mix appears after the audio mix "
                "stage runs.")
            return
        ok, message, peaks = self.vm.waveform_peaks(str(path))
        if ok:
            self.waveform.set_peaks(peaks)
            duration_fn = getattr(self.vm, "audio_file_duration", None)
            if callable(duration_fn):
                _ok_d, _msg_d, seconds = duration_fn(str(path))
                self.waveform.set_duration(seconds)
        else:
            self.waveform.set_message(message)
        self._status(message)

    def _save(self) -> None:
        project_id = self.current_project_id()
        if not project_id:
            self._status("Pick a project first.")
            return
        values: Dict[str, Any] = {
            "narration_volume": self.narration_slider.value(),
            "music_volume": self.music_slider.value(),
            "sfx_volume": self.sfx_slider.value(),
            "music_file_path": self.music_edit.text().strip(),
            "ducking_enabled": self.ducking_check.isChecked(),
            "master_volume": self.master_slider.value() / 100.0,
            "mute_narration": self.mute_narration.isChecked(),
            "mute_music": self.mute_music.isChecked(),
            "mute_sfx": self.mute_sfx.isChecked(),
        }
        for key, slider in self.duck_sliders.items():
            values[key] = slider.value()
        for key, spin in self.fade_spins.items():
            values[key] = spin.value()
        ok, message = self.vm.save_audio_settings(project_id, values)
        self._status(message)
