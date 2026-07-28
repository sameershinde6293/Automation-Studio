"""Waveform peak display (v3.0 master spec — Audio Waveform).

Bar-style peak meter fed by the view-model's stdlib ``wave`` reader
(``UiV3Mixin.waveform_peaks``). Pure paint layer: give it a list of
0.0-1.0 peaks or an honest message.

3.0.4 review #4 upgrades: click-to-seek (emits ``seekRequested`` in
seconds), a playhead line, a time axis under the bars, and a
near-clipping legend explaining the bright bars.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget

from ui.theme import ACCENT, ACCENT_SOFT, PANEL_BG, TEXT_MUTED

PLAYHEAD_COLOR = "#E94560"  # bright accent per the review spec
_AXIS_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 180, 300, 600, 900)


class WaveformWidget(QWidget):
    """Painted peak meter for the Audio page (v3.0 #18).

    3.0.4: interactive. Clicking seeks (``seekRequested(seconds)``,
    also aliased ``seek_requested``), ``set_playhead`` draws a
    playable-position line, and the bottom axis shows time markers
    once ``set_duration`` reports the media length.
    """

    seekRequested = pyqtSignal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._peaks: List[float] = []
        self._message = "Load a narration mix to see its waveform."
        self._duration = 0.0
        self._playhead = -1.0  # 0.0-1.0 fraction; negative hides it
        self._footer = 14      # px reserved under the bars for the axis
        self.seek_requested = self.seekRequested  # spec-named alias
        self.setMinimumHeight(110)
        self.setToolTip(
            "Narration waveform — click to seek; bright bars are "
            "near-clipping peaks (≥0.85)")

    # -- data ----------------------------------------------------------
    def set_peaks(self, peaks: List[float]) -> None:
        cleaned: List[float] = []
        for peak in peaks:
            try:
                cleaned.append(max(0.0, min(1.0, float(peak))))
            except (TypeError, ValueError):
                continue
        self._peaks = cleaned
        self._message = ""
        if not cleaned:
            self._playhead = -1.0
        self.update()

    def set_message(self, text: str) -> None:
        self._peaks = []
        self._message = str(text or "")
        self._playhead = -1.0
        self.update()

    def set_duration(self, seconds: float) -> None:
        """Total media length — enables the time axis and real seeks."""
        try:
            self._duration = max(0.0, float(seconds))
        except (TypeError, ValueError):
            self._duration = 0.0
        self.update()

    def set_playhead(self, fraction: float) -> None:
        """Move the playhead (0.0-1.0 of the clip); out of range hides."""
        try:
            value = float(fraction)
        except (TypeError, ValueError):
            value = -1.0
        self._playhead = value if 0.0 <= value <= 1.0 else -1.0
        self.update()

    # -- interaction ---------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Click-to-seek: emit the time under the cursor (3.0.4 #4)."""
        if (event.button() == Qt.MouseButton.LeftButton
                and self._peaks and self.width() > 0):
            fraction = min(
                1.0, max(0.0, event.position().x() / self.width()))
            self._playhead = fraction
            self.update()
            self.seekRequested.emit(fraction * self._duration)
        super().mousePressEvent(event)

    # -- painting ------------------------------------------------------
    def _axis_interval(self) -> int:
        target = max(1, self.width() // 90)
        for step in _AXIS_STEPS:
            if self._duration / step <= target:
                return step
        return _AXIS_STEPS[-1]

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(PANEL_BG))
        if not self._peaks:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                self._message)
            painter.end()
            return
        bar_area = max(10, self.height() - self._footer)
        count = len(self._peaks)
        width = max(1, self.width() // count)
        middle = bar_area // 2
        painter.setPen(Qt.PenStyle.NoPen)
        for index, peak in enumerate(self._peaks):
            painter.setBrush(
                QColor(ACCENT) if peak >= 0.85
                else QColor(ACCENT_SOFT))
            half = max(2, int(peak * (bar_area // 2 - 4)))
            painter.drawRect(
                index * width + 1, middle - half,
                max(1, width - 2), half * 2)

        small = painter.font()
        small.setPointSizeF(7.5)
        painter.setFont(small)

        if self._duration > 0:  # time axis labels (3.0.4 #4)
            painter.setPen(QColor(TEXT_MUTED))
            interval = self._axis_interval()
            marks = int(self._duration // interval) + 1
            for mark in range(marks):
                moment = mark * interval
                x_pos = int(
                    moment / self._duration * (self.width() - 1))
                painter.drawLine(x_pos, bar_area, x_pos, bar_area + 3)
                label = f"{moment // 60}:{moment % 60:02d}"
                painter.drawText(
                    x_pos - 20, bar_area + 3, 40, self._footer - 3,
                    int(Qt.AlignmentFlag.AlignHCenter
                        | Qt.AlignmentFlag.AlignVCenter),
                    label)

        # Legend: what the bright bars mean (3.0.4 #4)
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(
            self.rect().adjusted(4, 2, -4, 0),
            int(Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignTop),
            "Bright bars = near-clipping (≥0.85)")

        if 0.0 <= self._playhead <= 1.0:  # playhead (3.0.4 #4)
            pen = QPen(QColor(PLAYHEAD_COLOR))
            pen.setWidth(2)
            painter.setPen(pen)
            x_pos = int(self._playhead * (self.width() - 1))
            painter.drawLine(x_pos, 0, x_pos, bar_area)
        painter.end()
