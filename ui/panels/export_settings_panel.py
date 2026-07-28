"""Export settings panel (v3.0 master spec — Export).

Resolution, FPS, codec, CRF, encoder preset, audio profile, export
folder and naming pattern — the default export profile for the app.
Re-encode exports (like File ▸ Export → Burn Subtitles) consume it
immediately via the view-model. This file only paints widgets.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.panels.compact_field import field_row, section_label, wrap_scrollable
from ui.viewmodel import UiViewModel


class ExportSettingsPanel(QWidget):
    """v3.0 control panel #3 — the app's default export profile."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Export Settings")
        heading.setObjectName("panelTitle")
        header.addWidget(heading, 1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        header.addWidget(self.summary_label)
        layout.addLayout(header)

        # ROOT-CAUSE FIX (v3.2.8): scrollable content area — see
        # compact_field.wrap_scrollable for why.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(16)
        video_card = QFrame()
        video_card.setObjectName("card")
        video_layout = QVBoxLayout(video_card)
        video_layout.addWidget(section_label("Video"))
        self.resolution_combo = QComboBox()
        self.resolution_combo.setToolTip(
            "Output frame size — Custom enables width/height")
        self.resolution_combo.currentIndexChanged.connect(
            self._refresh_summary)

        self.fps_combo = QComboBox()
        self.fps_combo.setToolTip("Frames per second")
        self.fps_combo.currentIndexChanged.connect(
            self._refresh_summary)

        self.codec_combo = QComboBox()
        self.codec_combo.setToolTip("Video codec for re-encode exports")
        self.codec_combo.currentIndexChanged.connect(
            self._refresh_summary)
        # UI REDESIGN (v3.2.9): was 5 separate full-width rows.
        video_layout.addWidget(field_row(
            ("Resolution:", self.resolution_combo),
            ("FPS:", self.fps_combo),
            ("Codec:", self.codec_combo),
        ))

        size_row = QHBoxLayout()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 7680)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 4320)
        for spin in (self.width_spin, self.height_spin):
            spin.valueChanged.connect(self._refresh_summary)
        size_row.addWidget(QLabel("Custom size:"))
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_spin)
        size_row.addStretch(1)
        video_layout.addLayout(size_row)

        # 3.0.7: spinbox instead of a slider (round-6 review) — the
        # tiny right-edge slider number was unreadable; the typed
        # value is precise and shows the full 0-51 codec domain.
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(20)
        self.crf_spin.setToolTip(
            "Quality (0 = lossless, 51 = smallest files; "
            "14-35 recommended)")
        self.crf_spin.valueChanged.connect(self._refresh_summary)

        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip(
            "Encoder speed vs compression trade-off")
        video_layout.addWidget(field_row(
            ("CRF:", self.crf_spin),
            ("Preset:", self.preset_combo),
        ))
        content_layout.addWidget(video_card)

        audio_card = QFrame()
        audio_card.setObjectName("card")
        audio_layout = QVBoxLayout(audio_card)
        audio_layout.addWidget(section_label("Audio"))
        self.audio_codec_combo = QComboBox()
        self.bitrate_combo = QComboBox()
        self.rate_combo = QComboBox()
        self.channels_combo = QComboBox()
        # UI REDESIGN (v3.2.9): was 4 separate full-width rows.
        audio_layout.addWidget(field_row(
            ("Audio codec:", self.audio_codec_combo),
            ("Bitrate:", self.bitrate_combo),
            ("Sample rate:", self.rate_combo),
            ("Channels:", self.channels_combo),
        ))
        content_layout.addWidget(audio_card)

        output_card = QFrame()
        output_card.setObjectName("card")
        out_form = QFormLayout(output_card)
        out_form.addRow(section_label("Output"))
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("(project exports folder)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        out_form.addRow("Export folder:", folder_row)

        self.naming_edit = QLineEdit()
        self.naming_edit.setToolTip(
            "Tokens: {project} {date} {scene}")
        out_form.addRow("Naming pattern:", self.naming_edit)

        self.open_folder_check = QCheckBox(
            "Open the folder after export")
        out_form.addRow("After export:", self.open_folder_check)
        self.play_after_check = QCheckBox(
            "Play the result after export")
        out_form.addRow("", self.play_after_check)
        content_layout.addWidget(output_card)

        save = QPushButton("Save export settings")
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
            "Saved to app settings as the default export profile. "
            "Re-encode exports (File ▸ Export → Burn Subtitles) use "
            "the codec, CRF and preset right away; the engine's own "
            "render stage keeps its tuned defaults."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        content_layout.addWidget(note)
        content_layout.addStretch(1)
        wrap_scrollable(layout, content)
        self.reload()

    # ------------------------------------------------------------------
    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def _fill(
        self, combo: QComboBox, values: Any, current: str
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        for value in values:
            if isinstance(value, dict):
                combo.addItem(value["label"], value["id"])
            else:
                combo.addItem(str(value), str(value))
        index = combo.findData(current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def reload(self) -> None:
        model = self.vm.export_settings_model()
        self._fill(self.resolution_combo, model["resolutions"],
                   str(model["export_resolution"]))
        self._fill(self.fps_combo, model["fps_values"],
                   str(model["export_fps"]))
        self._fill(self.codec_combo, model["codecs"],
                   str(model["export_codec"]))
        self._fill(self.preset_combo, model["presets"],
                   str(model["export_preset"]))
        self._fill(self.audio_codec_combo, model["audio_codecs"],
                   str(model["export_audio_codec"]))
        self._fill(self.bitrate_combo, model["bitrates"],
                   str(model["export_audio_bitrate"]))
        self._fill(self.rate_combo, model["sample_rates"],
                   str(model["export_sample_rate"]))
        self._fill(self.channels_combo, model["channels"],
                   str(model["export_channels"]))
        try:
            self.width_spin.setValue(int(model["export_width"]))
            self.height_spin.setValue(int(model["export_height"]))
            self.crf_spin.setValue(int(model["export_crf"]))
        except (TypeError, ValueError):
            self.crf_spin.setValue(20)
        self.folder_edit.setText(str(model["export_folder"] or ""))
        self.naming_edit.setText(str(model["export_naming"] or ""))
        self.open_folder_check.setChecked(
            bool(model["export_open_folder"]))
        self.play_after_check.setChecked(
            bool(model["export_play_after"]))
        self._refresh_summary()

    def _state(self) -> Dict[str, Any]:
        return {
            "export_resolution":
                str(self.resolution_combo.currentData()),
            "export_width": self.width_spin.value(),
            "export_height": self.height_spin.value(),
            "export_fps": str(self.fps_combo.currentData()),
            "export_codec": str(self.codec_combo.currentData()),
            "export_crf": self.crf_spin.value(),
            "export_preset": str(self.preset_combo.currentData()),
            "export_audio_codec":
                str(self.audio_codec_combo.currentData()),
            "export_audio_bitrate":
                str(self.bitrate_combo.currentData()),
            "export_sample_rate": str(self.rate_combo.currentData()),
            "export_channels": str(self.channels_combo.currentData()),
            "export_folder": self.folder_edit.text().strip(),
            "export_naming": self.naming_edit.text().strip(),
            "export_open_folder":
                self.open_folder_check.isChecked(),
            "export_play_after": self.play_after_check.isChecked(),
        }

    def _on_revert(self) -> None:
        answer = QMessageBox.question(
            self, "Revert export settings",
            "Revert to the last saved values?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.reload()
        self._status("Reverted to the last saved export settings.")

    def _refresh_summary(self, *_args: Any) -> None:
        custom = str(self.resolution_combo.currentData()) == "Custom"
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        self.summary_label.setText(
            self.vm.export_summary_text(self._state()))

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose export folder")
        if path:
            self.folder_edit.setText(path)

    def _save(self) -> None:
        _ok, message = self.vm.save_export_settings(self._state())
        self._status(message)
        self._refresh_summary()
