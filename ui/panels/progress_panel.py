"""Render Progress panel (ui_specification.txt Section 12).

Collapsible bottom strip visible on every page: header shows the
current stage + percentage and collapses the body; body = progress
bar, per-stage indicator chips (pending/running/done/failed), live
log output, and Pause (honest-disabled) / Cancel buttons. Events
are pushed in from the shell's normalized pipeline records.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import ACCENT, DANGER, SUCCESS, TEXT_MUTED
from ui.viewmodel import UiViewModel

_MARK = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}
_COLOR = {
    "pending": TEXT_MUTED, "running": ACCENT,
    "done": SUCCESS, "failed": DANGER,
}


class ProgressPanel(QFrame):
    """Bottom render-progress strip (collapsible)."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._on_cancel = on_cancel
        self.setObjectName("progressPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 2)
        header = QHBoxLayout()
        self.toggle_button = QPushButton("▾ Render Progress")
        self.toggle_button.setFlat(True)
        self.toggle_button.clicked.connect(self.toggle_body)
        self.stage_label = QLabel("idle")
        self.stage_label.setObjectName("muted")
        self.percent_label = QLabel("")
        header.addWidget(self.toggle_button)
        header.addWidget(self.stage_label, 1)
        header.addWidget(self.percent_label)
        outer.addLayout(header)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        body.addWidget(self.progress_bar)
        # Review fix (3.0.1): the 18-name chip row clipped off-screen
        # and duplicated the Render tab's vertical stage list. One
        # compact line now reports the current stage — the full list
        # stays on the Render page (single source of truth).
        self._stage_order = list(self.vm.stage_names())
        self._stage_state = {
            name: "pending" for name in self._stage_order
        }
        self.stage_status = QLabel("")
        self.stage_status.setStyleSheet(f"color: {TEXT_MUTED};")
        self.stage_status.setToolTip(
            "Current pipeline stage — the full 18-stage list with "
            "per-stage marks lives on the Render tab")
        body.addWidget(self.stage_status)
        self.log_pane = QPlainTextEdit()
        self.log_pane.setReadOnly(True)
        self.log_pane.setMaximumHeight(110)
        body.addWidget(self.log_pane)
        buttons = QHBoxLayout()
        self.pause_button = QPushButton("❚❚ Pause")
        self.pause_button.setEnabled(False)  # honest: engine v1
        self.pause_button.setToolTip(
            "Pause is not supported by engine v1"
        )
        self.cancel_button = QPushButton("■ Cancel")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setToolTip(
            "Stop the running render (Esc)")
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.cancel_button)
        body.addLayout(buttons)
        outer.addWidget(self.body)

    # ------------------------------------------------------------------
    def toggle_body(self) -> None:
        self.body.setVisible(not self.body.isVisible())
        self.toggle_button.setText(
            ("▾" if self.body.isVisible() else "▸") + " Render Progress"
        )

    def set_running(self, running: bool) -> None:
        self.cancel_button.setEnabled(running)
        if running and not self.body.isVisible():
            self.toggle_body()

    def reset_stages(self) -> None:
        for name in self._stage_state:
            self._stage_state[name] = "pending"
        self.stage_status.setText("")
        self.stage_label.setText("idle")
        self.percent_label.setText("")
        self.progress_bar.setValue(0)

    def on_record(self, record: Dict[str, Any]) -> None:
        """Apply one normalized pipeline record (shell-routed)."""
        stage = record.get("stage")
        marks = {
            "pipeline.stage_started": "running",
            "pipeline.stage_completed": "done",
            "pipeline.failed": "failed",
        }
        mark = marks.get(record["event"])
        if mark and stage in self._stage_state:
            self._stage_state[stage] = mark
            index = self._stage_order.index(stage) + 1
            self.stage_status.setText(
                f"{_MARK[mark]} Stage {index}/"
                f"{len(self._stage_order)} · "
                f"{str(stage).replace('_', ' ')} — {mark}"
            )
            self.stage_status.setStyleSheet(
                f"color: {_COLOR[mark]};")
        if record["event"] == "pipeline.stage_started":
            self.stage_label.setText(str(record["text"]))
        if record["event"] in ("pipeline.completed", "pipeline.failed"):
            self.stage_label.setText(str(record["text"]))
        if record.get("percent") is not None:
            percent = float(record["percent"])
            self.progress_bar.setValue(int(percent * 10))
            self.percent_label.setText(f"{percent:.0f}%")
        self.log_pane.appendPlainText(str(record["text"]))

    def _cancel(self) -> None:
        if self._on_cancel is not None:
            self._on_cancel()
