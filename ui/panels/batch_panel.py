"""Batch panel (ui_specification.txt Section 14).

Queue list with priority + status indicators, Add current project /
Remove queued, priority ↑/↓, and Start/Stop. Start hands the queue
to the shell, which renders items SEQUENTIALLY with the same engine
+ worker as single renders (no second engine). Statuses come from
the batch_queue table itself — honest at every step.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.viewmodel import UiViewModel

_STATUS_MARK = {
    "queued": "○", "running": "▶", "completed": "✓", "failed": "✗",
    "cancelled": "■", "paused": "❚❚",
}


class BatchPanel(QWidget):
    """Queue + controls; the shell runs the actual renders."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
        add_current_provider: Optional[Callable[[], dict]] = None,
        on_start: Optional[Callable[[List[str]], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._add_current_provider = add_current_provider
        self._on_start = on_start
        self._on_stop = on_stop

        layout = QVBoxLayout(self)
        heading = QLabel("Batch Queue")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["", "Title", "Type", "Channel", "Priority", "Status", "Added", "Error"]
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.tree, 1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        layout.addWidget(self.summary_label)

        row = QHBoxLayout()
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 9)
        self.priority_spin.setValue(5)
        self.priority_spin.setPrefix("P")
        # FEATURE (v3.2.14): let a queued item be a full render OR just
        # narration (TTS + mix, no video) — much faster to queue several
        # scripts overnight for audio review before committing to full
        # video renders.
        self.job_type_combo = QComboBox()
        self.job_type_combo.addItem("Full render", "full")
        self.job_type_combo.addItem("Audio only", "audio_only")
        self.job_type_combo.setToolTip(
            "Audio only: narration + music mix, skips all video "
            "rendering — much faster, good for reviewing scripts")
        self.add_button = QPushButton("Add current project")
        self.add_button.clicked.connect(self._add_current)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        self.up_button = QPushButton("↑ Earlier")
        self.up_button.setEnabled(False)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("↓ Later")
        self.down_button.setEnabled(False)
        self.down_button.clicked.connect(lambda: self._move(1))
        self.start_button = QPushButton("▶ Start queue")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("■ Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        refresh = QPushButton("↻")
        refresh.setToolTip("Reload queue")
        refresh.clicked.connect(self.refresh)
        for widget in (
            QLabel("Priority:"), self.priority_spin,
            QLabel("Type:"), self.job_type_combo, self.add_button,
            self.remove_button, self.up_button, self.down_button,
        ):
            row.addWidget(widget)
        row.addStretch(1)
        for widget in (self.start_button, self.stop_button, refresh):
            row.addWidget(widget)
        layout.addLayout(row)
        self.refresh()

    # ------------------------------------------------------------------
    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def _selected_id(self) -> Optional[str]:
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return str(data) if data else None

    def _selection_changed(self) -> None:
        has = self._selected_id() is not None
        self.remove_button.setEnabled(has)
        self.up_button.setEnabled(has)
        self.down_button.setEnabled(has)

    def refresh(self) -> None:
        # Review fix (3.0.3): keep the user's selection across the
        # rebuild — reordering a long queue must not cost double
        # clicks.
        current = self.tree.currentItem()
        selected_id = (
            current.data(0, Qt.ItemDataRole.UserRole)
            if current is not None else None
        )
        self.tree.clear()
        model = self.vm.batch_model()
        for row in model["rows"]:
            mark = _STATUS_MARK.get(row["status"], "?")
            item = QTreeWidgetItem(
                [
                    mark,
                    row["title"],
                    "Audio only" if row.get("job_type") == "audio_only" else "Full render",
                    row.get("channel") or "—",
                    f"P{row['priority']}",
                    row["status"],
                    row["added_at"],
                    row["error"][:60],
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, row["id"])
            self.tree.addTopLevelItem(item)
        if model["rows"]:
            self.summary_label.setText(model["summary_text"])
        else:  # empty state (deep-dive fix #6)
            self.summary_label.setText(
                "🗂  Queue is empty — 'Add current project' queues "
                "your next overnight batch.")
        self.start_button.setEnabled(model["queued"] > 0)
        if selected_id is not None:
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                if item is not None and item.data(
                        0, Qt.ItemDataRole.UserRole) == selected_id:
                    self.tree.setCurrentItem(item)
                    break
        self._selection_changed()

    def queued_items(self) -> List[dict]:
        return [
            row
            for row in self.vm.batch_model()["rows"]
            if row["status"] == "queued"
        ]

    # ------------------------------------------------------------------
    def _add_current(self) -> None:
        payload = (
            self._add_current_provider()
            if self._add_current_provider is not None else {}
        ) or {}
        ok, message = self.vm.batch_add(
            str(payload.get("script_path") or ""),
            str(payload.get("images_folder") or ""),
            str(payload.get("project_folder") or ""),
            str(payload.get("title") or ""),
            self.priority_spin.value(),
            channel=str(payload.get("channel") or ""),
            job_type=str(self.job_type_combo.currentData() or "full"),
        )
        self._status(message)
        self.refresh()

    def _remove_selected(self) -> None:
        batch_id = self._selected_id()
        if batch_id is None:
            return
        ok, message = self.vm.batch_remove(batch_id)
        self._status(message)
        self.refresh()

    def _move(self, delta: int) -> None:
        batch_id = self._selected_id()
        if batch_id is None:
            return
        ok, message = self.vm.batch_move(batch_id, delta)
        self._status(message)
        self.refresh()

    def _start(self) -> None:
        ids = [row["id"] for row in self.queued_items()]
        if not ids:
            self._status("Nothing queued.")
            return
        if self._on_start is not None:
            self._on_start(ids)
            self.stop_button.setEnabled(True)

    def _stop(self) -> None:
        if self._on_stop is not None:
            self._on_stop()
        self.stop_button.setEnabled(False)
        self.refresh()

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
