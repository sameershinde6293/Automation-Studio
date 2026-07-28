"""Timeline panel: VISUAL scene cards + markers + waveform (§9).

- Scene cards with REAL image thumbnails (proxy/image path; painted
  placeholder only when the file is genuinely absent).
- Chapter markers drawn as vertical lines on a header strip
  (chapter_title/is-chapter scenes, else every scene start).
- Narration waveform strip (PCM .wav peaks via the view-model;
  honest placeholder when the mix doesn't exist yet).
- Right-click context menu on scenes: copy/paste/delete/details,
  wired to the same view-model ops as the Edit menu (undoable).
- Drag a card vertically to reorder scenes (DB renumber, undoable).

Editing stays scene-structure only; text edits remain the pipeline's
job for v1 — the header hint keeps saying so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.theme import ACCENT, PANEL_BG, TEXT_MUTED
from ui.viewmodel import UiViewModel, scene_card_lines

THUMB_W, THUMB_H = 112, 63  # 16:9 scene thumbnails
CARD_H = 84  # approx card + spacing height for drag math


def _placeholder_pixmap() -> QPixmap:
    """Grey 16:9 'no image' tile (only when the file truly is gone)."""
    pixmap = QPixmap(THUMB_W, THUMB_H)
    pixmap.fill(QColor(PANEL_BG))
    painter = QPainter(pixmap)
    painter.setPen(QColor(TEXT_MUTED))
    font = painter.font()
    font.setPointSize(11)
    painter.setFont(font)
    painter.drawText(
        pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), "no image"
    )
    painter.end()
    return pixmap


class MarkerStrip(QWidget):
    """Chapter markers as vertical lines + tiny labels (§9)."""

    def __init__(self) -> None:
        super().__init__()
        self._markers: List[Dict[str, Any]] = []
        self.setFixedHeight(26)

    def set_markers(self, markers: List[Dict[str, Any]]) -> None:
        self._markers = list(markers or [])
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(PANEL_BG))
        width = max(1, self.width() - 12)
        painter.setPen(QPen(QColor(ACCENT), 2))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        for marker in self._markers:
            x = 6 + int(width * float(marker.get("percent") or 0) / 100.0)
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.drawLine(x, 2, x, self.height() - 2)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(
                x + 3, self.height() - 6,
                str(marker.get("title") or "")[:18],
            )
        if not self._markers:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(
                self.rect(), int(Qt.AlignmentFlag.AlignCenter),
                "chapter markers appear after a render",
            )
        painter.end()


class WaveformStrip(QWidget):
    """Narration waveform: painted min/max peaks (§9)."""

    def __init__(self) -> None:
        super().__init__()
        self._peaks: List[float] = []
        self._note = "narration waveform appears after the audio mix"
        self.setFixedHeight(48)

    def set_wave(self, model: Dict[str, Any]) -> None:
        self._peaks = list(model.get("peaks") or [])
        self._note = str(model.get("note") or "")
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(PANEL_BG))
        mid = self.height() // 2
        if self._peaks:
            count = len(self._peaks)
            width = max(1, self.width() - 8)
            step = max(1, width // count)
            painter.setPen(QPen(QColor(ACCENT), 1))
            for index, peak in enumerate(self._peaks):
                x = 4 + index * step
                half = max(1, int((self.height() - 8) * peak / 2))
                painter.drawLine(x, mid - half, x, mid + half)
        else:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(
                self.rect(), int(Qt.AlignmentFlag.AlignCenter), self._note
            )
        painter.end()


class SceneCard(QFrame):
    """One scene: thumbnail + title/meta/detail; click to select."""

    def __init__(self, scene: Dict[str, Any]) -> None:
        super().__init__()
        self.scene_number = int(scene.get("number") or 0)
        self.setObjectName("sceneCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setSpacing(10)
        self.thumb = QLabel()
        self.thumb.setObjectName("sceneThumb")
        self.thumb.setFixedSize(THUMB_W, THUMB_H)
        thumb_path = scene.get("thumb_path")
        pixmap = (
            QPixmap(str(thumb_path))
            if thumb_path and Path(str(thumb_path)).is_file()
            else QPixmap()
        )
        if pixmap.isNull():
            self.thumb.setPixmap(_placeholder_pixmap())
        else:
            self.thumb.setPixmap(
                pixmap.scaled(
                    THUMB_W, THUMB_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        row.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignTop)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_line, meta_line, detail_line = scene_card_lines(scene)
        self.title_label = QLabel(title_line)
        self.title_label.setStyleSheet(f"color: {ACCENT}; font-weight: 600;")
        self.meta_label = QLabel(meta_line)
        self.detail_label = QLabel(detail_line)
        self.detail_label.setObjectName("muted")
        self.detail_label.setWordWrap(True)
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.meta_label)
        text_col.addWidget(self.detail_label)
        text_col.addStretch(1)
        row.addLayout(text_col, 1)

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet(
            f"QFrame#sceneCard {{ border: 2px solid {ACCENT}; }}"
            if selected else ""
        )


class TimelinePanel(QWidget):
    """Markers + waveform + selectable/reorderable scene cards."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
        on_scene_changed: Optional[Callable[[], None]] = None,
        cta: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._on_scene_changed = on_scene_changed
        self._cta = cta
        self.cards: List[SceneCard] = []
        self.selected_scene: Optional[int] = None
        self._drag_from: Optional[int] = None
        # Optional shell hook (scene number / None) — the Inspector
        # panel uses it to mirror the current selection.
        self.on_scene_selected: Optional[Any] = None

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Timeline (scene ops: right-click / drag)")
        heading.setObjectName("panelTitle")
        self.project_combo = QComboBox()
        self.project_combo.currentIndexChanged.connect(self.refresh)
        self.refresh_button = QPushButton("↻")
        self.refresh_button.setToolTip("Reload projects")
        self.refresh_button.clicked.connect(self.reload_projects)
        header.addWidget(heading, 1)
        header.addWidget(self.project_combo, 2)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        layout.addWidget(self.summary_label)
        self.marker_strip = MarkerStrip()
        layout.addWidget(self.marker_strip)
        self.waveform_strip = WaveformStrip()
        layout.addWidget(self.waveform_strip)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setSpacing(6)
        self.cards_layout.setContentsMargins(2, 2, 2, 2)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.cards_host)
        layout.addWidget(self.scroll, 1)
        self.reload_projects()

    # ------------------------------------------------------------------
    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def current_project_id(self) -> str:
        data = self.project_combo.currentData()
        return str(data) if data else ""

    def _clear_cards(self) -> None:
        while self.cards_layout.count() > 1:  # keep the trailing stretch
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards = []

    def reload_projects(self) -> None:
        current = self.project_combo.currentData()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        rows = self.vm.timeline_projects()
        for row in rows:
            self.project_combo.addItem(row["label"], row["id"])
        if rows:
            index = next(
                (i for i, row in enumerate(rows) if row["id"] == current),
                0,
            )
            self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)
        self.refresh()

    def refresh(self) -> None:
        self._clear_cards()
        project_id = self.current_project_id()
        model = self.vm.timeline_model(project_id)
        self.summary_label.setText(self.vm.timeline_summary_text(model))
        markers = self.vm.chapter_markers(project_id)
        self.marker_strip.set_markers(markers.get("markers") or [])
        self.waveform_strip.set_wave(self.vm.waveform_model(project_id))
        if not model.get("found"):
            # Rich empty state (deep-dive fix #6): icon + text + CTA.
            holder = QWidget()
            col = QVBoxLayout(holder)
            icon = QLabel("🎬")
            icon.setObjectName("emptyIcon")
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            text = QLabel("No scenes yet")
            text.setObjectName("emptyText")
            text.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint = QLabel(
                "Pick a project above — scene cards appear here "
                "after a render, with thumbnails and chapters.")
            hint.setObjectName("emptyHint")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            col.addWidget(icon)
            col.addWidget(text)
            col.addWidget(hint)
            if callable(self._cta):
                go = QPushButton("▶  Go to Render page")
                go.setToolTip("Open the Render page to start (F9)")
                go.clicked.connect(self._cta)
                col.addWidget(
                    go, 0, Qt.AlignmentFlag.AlignCenter)
            self.cards_layout.insertWidget(0, holder)
            return
        for index, scene in enumerate(model.get("scenes", [])):
            card = SceneCard(scene)
            card.mousePressEvent = (  # noqa: N802 - per-card binding
                lambda event, n=scene["number"]:
                self._card_pressed(event, n)
            )
            card.mouseReleaseEvent = (  # noqa: N802 - drop target math
                lambda event, c=card: self._card_released(event, c)
            )
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda pos, n=scene["number"], c=card:
                self._show_scene_menu(c.mapToGlobal(pos), n)
            )
            self.cards.append(card)
            self.cards_layout.insertWidget(index, card)
        self._apply_selection()

    # ------------------------------------------------------------------
    # Selection + drag reorder (mouse-driven, applies via view-model)
    # ------------------------------------------------------------------
    def select_scene(self, number: Optional[int]) -> None:
        self.selected_scene = None if number is None else int(number)
        self._apply_selection()
        if callable(self.on_scene_selected):
            self.on_scene_selected(self.selected_scene)

    def _apply_selection(self) -> None:
        for card in self.cards:
            card.set_selected(card.scene_number == self.selected_scene)

    def _card_pressed(self, event: Any, number: int) -> None:
        self.select_scene(number)
        if (
            hasattr(event, "button")
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_from = number

    def _card_released(self, event: Any, card: SceneCard) -> None:
        """Drop: release over ANY card resolves the drag (cards, not
        the panel, receive mouse events)."""
        if self._drag_from is None or not self.cards:
            self._drag_from = None
            return
        scene_number = self._drag_from
        self._drag_from = None
        point = (
            event.position().toPoint()
            if hasattr(event, "position") else QPoint(0, 0)
        )
        host_point = self.cards_host.mapFrom(card, point)
        index = max(
            0, min(int(host_point.y() // CARD_H), len(self.cards) - 1)
        )
        project_id = self.current_project_id()
        ok, message = self.vm.reorder_scene(project_id, scene_number, index)
        self._status(message)
        self.refresh()
        self.select_scene(index + 1 if ok else scene_number)
        self._notify_change()

    # ------------------------------------------------------------------
    # Right-click scene menu (same ops as the Edit menu)
    # ------------------------------------------------------------------
    def _show_scene_menu(self, global_pos: QPoint, number: int) -> None:
        self.select_scene(number)
        menu = QMenu(self)
        menu.addAction(f"Copy Scene {number}",
                       lambda: self._scene_op("copy"))
        menu.addAction("Paste Scene After This",
                       lambda: self._scene_op("paste"))
        menu.addSeparator()
        menu.addAction(f"Delete Scene {number}",
                       lambda: self._scene_op("delete"))
        menu.addSeparator()
        menu.addAction("Scene Details…",
                       lambda: self._status(
                           "Scene details live in Preview → "
                           "Scene Details tab."
                       ))
        menu.exec(global_pos)

    def _scene_op(self, op: str) -> None:
        project_id = self.current_project_id()
        number = self.selected_scene
        if not project_id:
            self._status("Pick a project first.")
            return
        if op == "paste":
            ok, message = self.vm.paste_scene(project_id, number or 0)
        elif number is None:
            self._status("Select a scene first.")
            return
        elif op == "copy":
            ok, message = self.vm.copy_scene(project_id, number)
        else:
            ok, message = self.vm.delete_scene(project_id, number)
        self._status(message)
        self.refresh()
        self._notify_change()

    def _notify_change(self) -> None:
        if self._on_scene_changed is not None:
            self._on_scene_changed()
