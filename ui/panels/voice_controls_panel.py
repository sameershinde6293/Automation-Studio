"""Voice controls panel (v3.0 master spec — Voice Controls).

Per-profile voice shaping: engine/voice pickers, speed/pitch/volume
sliders, emotion + reverb presets, breathing, staged pauses,
pronunciation dictionary, voice lock, presets and one-click Preview.
Every value lives in the Qt-free view-model (UiV3Mixin); this file
only paints widgets.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.panels.compact_field import build_slider_field, field_row, section_label, wrap_scrollable
from ui.viewmodel import UiViewModel


class VoiceControlsPanel(QWidget):
    """v3.0 control panel #1 — full voice shaping per profile."""

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
        heading = QLabel("Voice Controls")
        heading.setObjectName("panelTitle")
        header.addWidget(heading, 1)
        self.preview_button = QPushButton("▶  Preview voice")
        self.preview_button.setToolTip(
            "Speak the sample line with this profile (uses the TTS "
            "engine seam when one is available)")
        self.preview_button.clicked.connect(self._preview)
        header.addWidget(self.preview_button)
        layout.addLayout(header)

        # ROOT-CAUSE FIX (v3.2.8): everything below the header now
        # lives in a scrollable content area — see
        # compact_field.wrap_scrollable for why (overlapping text
        # when the window is shorter than the content).
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(16)

        # --- Voice selection card -----------------------------------
        voice_card = QFrame()
        voice_card.setObjectName("card")
        voice_layout = QVBoxLayout(voice_card)
        voice_layout.addWidget(section_label("Voice"))
        self.engine_combo = QComboBox()
        self.engine_combo.setToolTip(
            "TTS engine — 'auto' lets the project profile decide")

        self.voice_combo = QComboBox()
        self.voice_combo.setEditable(True)
        self.voice_combo.setToolTip(
            "Voice name — pick an installed voice or type one")

        self.emotion_combo = QComboBox()
        self.emotion_combo.setToolTip(
            "Emotion preset handed to the TTS engine")
        # UI REDESIGN (v3.2.9): was 3 separate full-width rows.
        voice_layout.addWidget(field_row(
            ("Engine:", self.engine_combo),
            ("Voice:", self.voice_combo),
            ("Emotion:", self.emotion_combo),
        ))
        content_layout.addWidget(voice_card)

        # --- Presets row (its own strip, not buried in the form) -----
        preset_card = QFrame()
        preset_card.setObjectName("card")
        preset_col = QVBoxLayout(preset_card)
        preset_col.addWidget(section_label("Presets"))
        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(200)
        self.preset_combo.setToolTip("Saved voice presets")
        apply_btn = QPushButton("Apply")
        apply_btn.setToolTip("Load the selected preset")
        apply_btn.clicked.connect(self._apply_preset)
        save_btn = QPushButton("Save as…")
        save_btn.setToolTip("Save the current settings as a preset")
        save_btn.clicked.connect(self._save_preset)
        delete_btn = QPushButton("Delete")
        delete_btn.setObjectName("danger")
        delete_btn.setToolTip("Delete the selected preset")
        delete_btn.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(apply_btn)
        preset_row.addWidget(save_btn)
        preset_row.addWidget(delete_btn)
        preset_col.addLayout(preset_row)
        content_layout.addWidget(preset_card)

        # --- Sound shaping card: reverb/breathing + a compact grid ---
        # of sliders instead of five full-width rows.
        shaping_card = QFrame()
        shaping_card.setObjectName("card")
        shaping_col = QVBoxLayout(shaping_card)
        shaping_col.addWidget(section_label("Sound shaping"))

        toggles_row = QHBoxLayout()
        self.reverb_combo = QComboBox()
        self.reverb_combo.setMinimumWidth(160)
        self.reverb_combo.setToolTip(
            "Room simulation applied to the voice")
        toggles_row.addWidget(QLabel("Reverb:"))
        toggles_row.addWidget(self.reverb_combo)
        self.breathing_check = QCheckBox(
            "Add natural breaths between sentences")
        self.breathing_check.setToolTip(
            "Breathing on/off — level set by the slider below")
        toggles_row.addWidget(self.breathing_check)
        toggles_row.addStretch(1)
        shaping_col.addLayout(toggles_row)

        slider_grid = QGridLayout()
        slider_grid.setHorizontalSpacing(20)
        slider_grid.setVerticalSpacing(14)
        self.sliders: Dict[str, Tuple[QSlider, QLabel, str]] = {}
        columns = 2
        for index, (key, label, lo, hi, default) in enumerate((
            ("voice_speed", "Speed", 50, 200, 100),
            ("voice_pitch_st", "Pitch", -6, 6, 0),
            ("voice_volume", "Volume", 0, 100, 100),
            ("voice_reverb_amount", "Reverb amount", 0, 100, 40),
            ("voice_breath_volume", "Breath volume", 0, 100, 30),
        )):
            wrapper, slider, value_label = build_slider_field(
                label, lo, hi, default)
            slider.valueChanged.connect(
                lambda value, lbl=value_label, k=key:
                lbl.setText(self._slider_text(k, value))
            )
            slider_grid.addWidget(wrapper, index // columns, index % columns)
            self.sliders[key] = (slider, value_label, "")
        shaping_col.addLayout(slider_grid)
        content_layout.addWidget(shaping_card)

        # --- Timing card: pause lengths in a compact grid -------------
        timing_card = QFrame()
        timing_card.setObjectName("card")
        timing_col = QVBoxLayout(timing_card)
        timing_col.addWidget(section_label("Pause timing"))
        timing_grid = QGridLayout()
        timing_grid.setHorizontalSpacing(20)
        timing_grid.setVerticalSpacing(10)
        self.pause_spins: Dict[str, QSpinBox] = {}
        for index, (key, label) in enumerate((
            ("voice_pause_comma_ms", "Comma pause"),
            ("voice_pause_sentence_ms", "Sentence pause"),
            ("voice_pause_paragraph_ms", "Paragraph pause"),
            ("voice_pause_chapter_ms", "Chapter pause"),
        )):
            field = QWidget()
            field_col = QVBoxLayout(field)
            field_col.setContentsMargins(0, 0, 0, 0)
            field_col.setSpacing(4)
            field_label = QLabel(label)
            field_label.setObjectName("fieldLabel")
            field_col.addWidget(field_label)
            spin = QSpinBox()
            spin.setRange(0, 5000)
            spin.setSingleStep(50)
            spin.setSuffix(" ms")
            spin.setMinimumWidth(120)
            spin.setToolTip(f"{label} inserted by the TTS stage")
            field_col.addWidget(spin)
            self.pause_spins[key] = spin
            timing_grid.addWidget(field, index // 2, index % 2)
        timing_col.addLayout(timing_grid)
        content_layout.addWidget(timing_card)

        # --- Advanced card: pronunciation, lock, preview line ---------
        advanced_card = QFrame()
        advanced_card.setObjectName("card")
        form = QFormLayout(advanced_card)
        form.addRow(section_label("Advanced"))
        self.pronunciation_edit = QLineEdit()
        self.pronunciation_edit.setPlaceholderText(
            "(no pronunciation dictionary)")
        self.pronunciation_edit.setToolTip(
            "Custom word → phoneme dictionary used at narration time")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_pronunciation)
        pro_row = QHBoxLayout()
        pro_row.addWidget(self.pronunciation_edit, 1)
        pro_row.addWidget(browse)
        manage_btn = QPushButton("Manage…")
        manage_btn.setToolTip(
            "Add/edit/delete pronunciation entries, or load a starter "
            "preset (Bible names, common acronyms)")
        manage_btn.clicked.connect(self._manage_pronunciation)
        pro_row.addWidget(manage_btn)
        form.addRow("Pronunciation:", pro_row)

        self.lock_check = QCheckBox(
            "Voice lock — keep this voice across all scenes")
        form.addRow("Lock:", self.lock_check)

        self.sample_edit = QLineEdit()
        self.sample_edit.setToolTip("Line spoken by Preview voice")
        form.addRow("Preview line:", self.sample_edit)
        content_layout.addWidget(advanced_card)

        save = QPushButton("Save voice profile")
        save.setObjectName("primary")
        save.setToolTip("Persist the whole profile to app settings")
        save.clicked.connect(self._save)
        content_layout.addWidget(save)
        revert = QPushButton("↺ Revert")
        revert.setObjectName("revertBtn")
        revert.setToolTip(
            "Discard unsaved edits and reload the saved profile")
        revert.clicked.connect(self._on_revert)
        content_layout.addWidget(revert)
        note = QLabel(
            "Stored as app settings and handed to the TTS stage on "
            "the next preview or narration pass. Engines that ignore "
            "a parameter simply skip it — every other value still "
            "applies."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        content_layout.addWidget(note)
        content_layout.addStretch(1)
        wrap_scrollable(layout, content)
        self.reload()

    # ------------------------------------------------------------------
    @staticmethod
    def _slider_text(key: str, value: int) -> str:
        if key == "voice_speed":
            return f"{value / 100:.2f}x"
        if key == "voice_pitch_st":
            return f"{value:+d} st"
        return f"{value}%"

    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def reload(self) -> None:
        model = self.vm.voice_controls_model()
        self.engine_combo.blockSignals(True)
        self.engine_combo.clear()
        self.engine_combo.addItems(model["engines"])
        index = self.engine_combo.findText(
            str(model["voice_engine"] or "auto"))
        self.engine_combo.setCurrentIndex(max(0, index))
        self.engine_combo.blockSignals(False)

        self.voice_combo.clear()
        voices = self._installed_voice_names()
        if voices:
            self.voice_combo.addItems(voices)
        self.voice_combo.setCurrentText(str(model["voice_name"] or ""))

        self.emotion_combo.clear()
        self.emotion_combo.addItems(model["emotions"])
        self.emotion_combo.setCurrentText(str(model["voice_emotion"]))
        self.reverb_combo.clear()
        self.reverb_combo.addItems(model["reverbs"])
        self.reverb_combo.setCurrentText(str(model["voice_reverb"]))

        values = {
            "voice_speed": int(model.get("speed_percent") or 100),
            "voice_pitch_st": int(model.get("voice_pitch_st") or 0),
            "voice_volume": int(model.get("voice_volume") or 100),
            "voice_reverb_amount":
                int(model.get("voice_reverb_amount") or 40),
            "voice_breath_volume":
                int(model.get("voice_breath_volume") or 30),
        }
        for key, (slider, label, _sfx) in self.sliders.items():
            slider.setValue(values[key])
            label.setText(self._slider_text(key, values[key]))
        self.breathing_check.setChecked(bool(model["voice_breathing"]))
        for key, spin in self.pause_spins.items():
            try:
                spin.setValue(int(model.get(key) or 0))
            except (TypeError, ValueError):
                spin.setValue(0)
        self.pronunciation_edit.setText(
            str(model["voice_pronunciation"] or ""))
        self.lock_check.setChecked(bool(model["voice_lock"]))
        self.sample_edit.setText(str(model["sample_text"]))
        self._reload_presets()

    def _on_revert(self) -> None:
        answer = QMessageBox.question(
            self, "Revert voice profile",
            "Revert to the last saved values?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.reload()
        self._status("Reverted to the last saved voice profile.")

    def _installed_voice_names(self) -> List[str]:
        try:
            rows = self.vm.voice_store_model().get("voices") or []
        except Exception:  # noqa: BLE001
            return []
        return [
            str(r.get("name")) for r in rows
            if r.get("name") and r.get("installed")
        ]

    def _reload_presets(self) -> None:
        self.preset_combo.clear()
        self.preset_combo.addItem("(choose a preset)")
        for name in self.vm.voice_controls_model()["presets"]:
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentIndex(0)

    def _state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {
            "voice_engine": self.engine_combo.currentText(),
            "voice_name": self.voice_combo.currentText().strip(),
            "voice_speed":
                self.sliders["voice_speed"][0].value() / 100,
            "voice_pitch_st":
                self.sliders["voice_pitch_st"][0].value(),
            "voice_volume": self.sliders["voice_volume"][0].value(),
            "voice_emotion": self.emotion_combo.currentText(),
            "voice_reverb": self.reverb_combo.currentText(),
            "voice_reverb_amount":
                self.sliders["voice_reverb_amount"][0].value(),
            "voice_breathing": self.breathing_check.isChecked(),
            "voice_breath_volume":
                self.sliders["voice_breath_volume"][0].value(),
            "voice_pronunciation":
                self.pronunciation_edit.text().strip(),
            "voice_lock": self.lock_check.isChecked(),
        }
        for key, spin in self.pause_spins.items():
            state[key] = spin.value()
        return state

    def _save(self) -> None:
        _ok, message = self.vm.save_voice_controls(self._state())
        self._status(message)

    def _apply_preset(self) -> None:
        if self.preset_combo.currentIndex() <= 0:
            self._status("Pick a preset first.")
            return
        name = self.preset_combo.currentText()
        ok, message, state = self.vm.apply_voice_preset(name)
        if ok:
            self.vm.save_voice_controls(state)
            self.reload()
        self._status(message)

    def _save_preset(self) -> None:
        name, accepted = QInputDialog.getText(
            self, "Save voice preset", "Preset name:")
        if not accepted or not str(name).strip():
            return
        ok, message = self.vm.save_voice_preset(
            str(name).strip(), self._state())
        self._reload_presets()
        self._status(message)

    def _delete_preset(self) -> None:
        if self.preset_combo.currentIndex() <= 0:
            self._status("Pick a preset first.")
            return
        _ok, message = self.vm.delete_voice_preset(
            self.preset_combo.currentText())
        self._reload_presets()
        self._status(message)

    def _browse_pronunciation(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Pronunciation dictionary", "",
            "Pronunciation dictionaries (*.json);;All files (*)",
        )
        if path:
            self.pronunciation_edit.setText(path)

    def _manage_pronunciation(self) -> None:
        from ui.dialogs.pronunciation_manager_dialog import (
            PronunciationManagerDialog,
        )
        dialog = PronunciationManagerDialog(
            self.vm, self.pronunciation_edit.text().strip(), self)
        if dialog.exec() and dialog.result_path:
            self.pronunciation_edit.setText(dialog.result_path)

    def _preview(self) -> None:
        ok, message, payload = self.vm.preview_voice(
            self.sample_edit.text())
        path = str(payload.get("path") or "") if ok else ""
        if path and self._preview_callback is not None:
            self._preview_callback(path)
        self._status(message)
