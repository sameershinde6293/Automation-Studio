"""Subtitle designer panel (v3.0 master spec — Subtitle Designer).

Font, size, weight, colours, outline, shadow, background, position,
margins and animation — one style, applied everywhere subtitles are
burned (File ▸ Export → Burn Subtitles consumes it via the
view-model's ASS force_style builder). This file only paints widgets.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.panels.compact_field import (
    build_slider_field,
    field_row,
    section_label,
    wrap_scrollable,
)
from ui.viewmodel import UiViewModel


class SubtitleStylePanel(QWidget):
    """v3.0 control panel #7 — one subtitle style for every burn."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._colors: Dict[str, str] = {}

        layout = QVBoxLayout(self)
        heading = QLabel("Subtitle Designer")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)

        self.preview_label = QLabel("")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(90)
        layout.addWidget(self.preview_label)

        # ROOT-CAUSE FIX (v3.2.8): this panel has ~15 form rows and NO
        # scroll area — confirmed from real screenshots that this causes
        # Qt to forcibly compress rows into overlapping, unreadable text
        # when the window is shorter than the content needs. This was a
        # pre-existing issue (this panel wasn't touched by the earlier
        # redesign pass) — see compact_field.wrap_scrollable.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)

        # UI REDESIGN (v3.2.10): was one QFormLayout with every field on
        # its own full-width row — the exact "long panel" pattern
        # reported for Scenes/Voice/Export, fixed the same way here:
        # short related fields grouped 3-per-line inside labeled cards.
        typography_card = QFrame()
        typography_card.setObjectName("card")
        typography_layout = QVBoxLayout(typography_card)
        typography_layout.addWidget(section_label("Typography"))
        self.font_combo = QComboBox()
        self.font_combo.setToolTip("Font family burned into subtitles")
        self.font_combo.currentTextChanged.connect(self._refresh_preview)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(12, 120)
        self.size_spin.valueChanged.connect(self._refresh_preview)
        self.weight_combo = QComboBox()
        self.weight_combo.currentIndexChanged.connect(self._refresh_preview)
        typography_layout.addWidget(field_row(
            ("Font:", self.font_combo),
            ("Size:", self.size_spin),
            ("Weight:", self.weight_combo),
        ))
        content_layout.addWidget(typography_card)

        colors_card = QFrame()
        colors_card.setObjectName("card")
        colors_layout = QVBoxLayout(colors_card)
        colors_layout.addWidget(section_label("Colors"))
        self.color_buttons: Dict[str, Any] = {}
        color_fields = []
        for key, label in (
            ("subtitle_color", "Text colour"),
            ("subtitle_outline_color", "Outline colour"),
            ("subtitle_back_color", "Background colour"),
        ):
            button = QPushButton("")
            button.setToolTip(f"{label} — click to pick")
            button.clicked.connect(
                lambda _c=False, k=key: self._pick_color(k))
            self.color_buttons[key] = button
            color_fields.append((f"{label}:", button))
        colors_layout.addWidget(field_row(*color_fields))
        content_layout.addWidget(colors_card)

        outline_card = QFrame()
        outline_card.setObjectName("card")
        outline_layout = QVBoxLayout(outline_card)
        outline_layout.addWidget(section_label("Outline & Background"))
        self.outline_spin = QSpinBox()
        self.outline_spin.setRange(0, 8)
        self.outline_spin.setToolTip("Outline thickness in pixels")
        self.shadow_spin = QSpinBox()
        self.shadow_spin.setRange(0, 6)
        outline_layout.addWidget(field_row(
            ("Outline:", self.outline_spin),
            ("Shadow:", self.shadow_spin),
        ))
        self.background_check = QCheckBox("Opaque box behind the text")
        self.background_check.toggled.connect(self._refresh_preview)
        outline_layout.addWidget(self.background_check)
        opacity_wrapper, self.opacity_slider, self.opacity_label = (
            build_slider_field("Box opacity", 0, 100, 50)
        )
        self.opacity_label.setText("50%")
        self.opacity_slider.valueChanged.connect(
            lambda v: self.opacity_label.setText(f"{v}%"))
        outline_layout.addWidget(opacity_wrapper)
        content_layout.addWidget(outline_card)

        position_card = QFrame()
        position_card.setObjectName("card")
        position_layout = QVBoxLayout(position_card)
        position_layout.addWidget(section_label("Position & Motion"))
        self.position_combo = QComboBox()
        self.position_combo.setToolTip("Where subtitles sit on the frame")
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 400)
        self.margin_spin.setSuffix(" px")
        self.margin_spin.setToolTip("Vertical margin from the chosen edge")
        self.animation_combo = QComboBox()
        self.animation_combo.setToolTip(
            "Entrance animation (stored for the subtitle engine)")
        position_layout.addWidget(field_row(
            ("Position:", self.position_combo),
            ("Margin:", self.margin_spin),
            ("Animation:", self.animation_combo),
        ))
        self.highlight_check = QCheckBox(
            "Highlight the spoken word (karaoke)")
        position_layout.addWidget(self.highlight_check)
        self.apply_burn_check = QCheckBox(
            "Apply this style when burning subtitles")
        position_layout.addWidget(self.apply_burn_check)
        content_layout.addWidget(position_card)

        save = QPushButton("Save subtitle style")
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
            "Font/size/colour/outline/position ride every burn via ASS "
            "force_style. Animation and karaoke are stored for the "
            "subtitle engine — burns keep the static style today."
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

    @staticmethod
    def _text_on(background: str) -> str:
        try:
            red = int(background[1:3], 16)
            green = int(background[3:5], 16)
            blue = int(background[5:7], 16)
        except (ValueError, IndexError):
            return "#FFFFFF"
        luma = 0.299 * red + 0.587 * green + 0.114 * blue
        return "#1a1a2e" if luma > 140 else "#FFFFFF"

    def _paint_swatch(self, key: str) -> None:
        button = self.color_buttons[key]
        colour = self._colors[key]
        # Review fix (3.0.1): plain swatch, no hex text on the button —
        # the hex lives in the colour picker dialog only.
        # 3.0.9: swatch AND readable hex text (auto contrast).
        button.setText(colour.upper())
        button.setFixedSize(92, 24)
        button.setStyleSheet(
            f"background:{colour}; color:{self._text_on(colour)};"
            " border:1px solid #3c3c58; border-radius:4px;"
            " font-weight:bold;")

    def _pick_color(self, key: str) -> None:
        colour = QColorDialog.getColor(
            QColor(self._colors.get(key) or "#FFFFFF"), self,
            "Pick colour")
        if colour.isValid():
            self._colors[key] = colour.name().upper()
            self._paint_swatch(key)
            self._refresh_preview()

    def reload(self) -> None:
        model = self.vm.subtitle_style_model()
        try:
            families = list(QFontDatabase.families())
        except Exception:  # noqa: BLE001
            families = []
        fonts = [f for f in model["fonts"] if f in families] \
            or list(model["fonts"])
        current_font = str(model["subtitle_font"])
        if current_font not in fonts:
            fonts.append(current_font)
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        self.font_combo.addItems(fonts)
        self.font_combo.setCurrentText(current_font)
        self.font_combo.blockSignals(False)

        self.weight_combo.clear()
        self.weight_combo.addItems(model["weights"])
        self.weight_combo.setCurrentText(
            str(model["subtitle_weight"]))
        self.position_combo.clear()
        self.position_combo.addItems(model["positions"])
        self.position_combo.setCurrentText(
            str(model["subtitle_position"]))
        self.animation_combo.clear()
        self.animation_combo.addItems(model["animations"])
        self.animation_combo.setCurrentText(
            str(model["subtitle_animation"]))

        self.size_spin.setValue(int(model["subtitle_size"] or 54))
        self.outline_spin.setValue(
            int(model["subtitle_outline"] or 3))
        self.shadow_spin.setValue(int(model["subtitle_shadow"] or 1))
        self.margin_spin.setValue(
            int(model["subtitle_margin_v"] or 40))
        self.opacity_slider.setValue(
            int(model["subtitle_back_opacity"] or 50))
        self.background_check.setChecked(
            bool(model["subtitle_background"]))
        self.highlight_check.setChecked(
            bool(model["subtitle_word_highlight"]))
        self.apply_burn_check.setChecked(
            bool(model["subtitle_apply_burn"]))
        self._colors = {
            "subtitle_color": str(model["subtitle_color"]),
            "subtitle_outline_color":
                str(model["subtitle_outline_color"]),
            "subtitle_back_color": str(model["subtitle_back_color"]),
        }
        for key in self.color_buttons:
            self._paint_swatch(key)
        self._preview_text = str(model["preview_text"])
        self._refresh_preview()

    def _state(self) -> Dict[str, Any]:
        return {
            "subtitle_font": self.font_combo.currentText(),
            "subtitle_size": self.size_spin.value(),
            "subtitle_weight": self.weight_combo.currentText(),
            "subtitle_color": self._colors.get(
                "subtitle_color", "#FFFFFF"),
            "subtitle_outline_color": self._colors.get(
                "subtitle_outline_color", "#000000"),
            "subtitle_outline": self.outline_spin.value(),
            "subtitle_shadow": self.shadow_spin.value(),
            "subtitle_background":
                self.background_check.isChecked(),
            "subtitle_back_color": self._colors.get(
                "subtitle_back_color", "#000000"),
            "subtitle_back_opacity": self.opacity_slider.value(),
            "subtitle_position": self.position_combo.currentText(),
            "subtitle_margin_v": self.margin_spin.value(),
            "subtitle_word_highlight":
                self.highlight_check.isChecked(),
            "subtitle_animation": self.animation_combo.currentText(),
            "subtitle_apply_burn": self.apply_burn_check.isChecked(),
        }

    def _on_revert(self) -> None:
        answer = QMessageBox.question(
            self, "Revert subtitle style",
            "Revert to the last saved values?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.reload()
        self._status("Reverted to the last saved subtitle style.")

    def _refresh_preview(self, *_args: Any) -> None:
        state = getattr(self, "_state", lambda: {})()
        if not state:
            return
        colour = state.get("subtitle_color") or "#FFFFFF"
        back = (
            state.get("subtitle_back_color")
            if state.get("subtitle_background") else "#1a1a2e")
        size = max(14, min(28, int(state.get("subtitle_size", 54))
                           // 2))
        weight = (QFont.Weight.Bold
                  if state.get("subtitle_weight") == "Bold"
                  else QFont.Weight.Normal)
        font = QFont(state.get("subtitle_font") or "Montserrat", size)
        font.setWeight(weight)
        self.preview_label.setFont(font)
        self.preview_label.setStyleSheet(
            f"QLabel {{ color:{colour}; background:{back};"
            " border:1px solid #3c3c58; border-radius:6px; }}")
        self.preview_label.setText(
            getattr(self, "_preview_text", "")
            or "Autopilot subtitles look like this.")

    def _save(self) -> None:
        _ok, message = self.vm.save_subtitle_style(self._state())
        self._status(message)
        self._refresh_preview()
