"""Shared compact form-field widgets (UI redesign, v3.2.7).

Small, reusable building blocks so every panel gets the same compact,
readable field treatment instead of each panel inventing its own
full-width row. See UI_REDESIGN_NOTES.md for the design rationale.
"""

from __future__ import annotations

from typing import Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

DEFAULT_FIELD_MAX_WIDTH = 260


def build_slider_field(
    label_text: str,
    minimum: int,
    maximum: int,
    default: int,
    max_width: int = DEFAULT_FIELD_MAX_WIDTH,
) -> Tuple[QWidget, QSlider, QLabel]:
    """One compact slider field: label + value on top, thin slider below.

    Replaces the old pattern of a slider stretched across the full
    panel width with a value label glued to its right — that's what
    made sliders look oversized. Returns (wrapper, slider, value_label)
    so the caller can wire up valueChanged and keep its own references.

    BUGFIX (v3.2.10): previously capped the wrapper to max_width
    unconditionally — inside a QGridLayout cell wider than that (which
    is normal on a wide panel), this left a dead empty gap instead of
    the field actually filling its cell. No longer capped; the grid's
    own column sizing governs width now, same as any other grid cell.
    """
    wrapper = QWidget()
    col = QVBoxLayout(wrapper)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(4)
    header = QHBoxLayout()
    header.setContentsMargins(0, 0, 0, 0)
    name_label = QLabel(label_text)
    name_label.setObjectName("fieldLabel")
    value_label = QLabel(str(default))
    value_label.setObjectName("sliderValue")
    header.addWidget(name_label, 1)
    header.addWidget(value_label, 0, Qt.AlignmentFlag.AlignRight)
    col.addLayout(header)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(minimum, maximum)
    slider.setValue(default)
    slider.setToolTip(label_text)
    col.addWidget(slider)
    return wrapper, slider, value_label


def field_row(*label_and_widget_pairs) -> QWidget:
    """Lay out several (label, widget) pairs side by side in one row.

    UI REDESIGN (v3.2.9): several panels had one field per full-width
    row (e.g. Animation / Intensity / Duration each on their own line,
    each stretched edge-to-edge) — reads as needlessly long for a
    handful of short values. This groups related short fields onto one
    line instead.

    BUGFIX (v3.2.10): the first version of this helper capped every
    field to DEFAULT_FIELD_MAX_WIDTH (260px) — on a wide panel with
    only 2-4 fields in the row, that left a large dead empty gap after
    them instead of actually using the panel's width. Real screenshots
    confirmed this. Fields now stretch evenly to fill the row's actual
    available width (equal stretch factor, no cap) — same fix
    direction as the original problem (don't force an artificial
    size), just corrected to not overshoot in the other direction.
    """
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(20)
    for label_text, widget in label_and_widget_pairs:
        field = QWidget()
        col = QVBoxLayout(field)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        col.addWidget(label)
        col.addWidget(widget)
        layout.addWidget(field, 1)
    return row


def section_label(text: str) -> QLabel:
    """A small-caps section header, e.g. above a group of fields in a card."""
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    return label


def wrap_scrollable(outer_layout, content_widget) -> "QScrollArea":
    """Put `content_widget` in a QScrollArea and add that to `outer_layout`.

    ROOT-CAUSE FIX (v3.2.8): panels with many QFormLayout rows and no
    QScrollArea get forcibly compressed by Qt when the window is
    shorter than the content needs — rows overlap instead of clipping
    or scrolling. Confirmed from real screenshots: this affected a
    panel (Subtitles) that was never touched in the v3.2.7 redesign,
    proving it's a pre-existing structural issue, not something the
    redesign introduced — the redesign's extra card content just made
    an existing problem more visible. voice_panel.py already used this
    pattern correctly (and looked fine); every other multi-row panel
    was missing it.
    """
    from PyQt6.QtWidgets import QScrollArea

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content_widget)
    outer_layout.addWidget(scroll, 1)
    return scroll
