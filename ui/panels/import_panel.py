"""Import panel: separate drop zones per kind (spec File 04).

Three clearly-labelled zones — Script (TXT · JSON · CSV · DOCX ·
PDF), Images (JPG · JPEG · PNG) and Audio (MP3 · WAV) — so the user
can SEE every supported format (JSON support is printed on the
script zone, not hidden in docs). Files dropped on ANY zone are
classified by extension and staged into <project>/imports/<kind>/;
the zone layout is guidance, the classifier is the truth. On a
successful stage the panel calls ``on_staged(script, images)`` so
the shell can pre-fill the render form.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import ACCENT
from ui.viewmodel import UiViewModel


def _fmt_size(size_bytes: int) -> str:
    size = float(size_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


_STATUS_BADGE = {
    "ready": "✓", "missing": "✗", "duplicate": "=", "unsupported": "?",
}


class DropZone(QLabel):
    """Dashed drop target for ONE import kind (formats printed on it)."""

    files_dropped = pyqtSignal(list)

    def __init__(self, zone: Dict[str, Any]) -> None:
        self.kind = str(zone.get("kind") or "")
        self.title = str(zone.get("title") or "Drop files")
        self.formats_text = str(zone.get("formats") or "")
        super().__init__()
        # UI REDESIGN (v3.2.7): was one flat plain-text block (title,
        # formats and hint all the same size/color) — no way to tell
        # at a glance what's the zone's NAME vs its supporting detail.
        # Rich text gives the title real visual weight; QLabel still
        # supports word wrap with rich text enabled.
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setText(
            f"<div style='font-size:13px;font-weight:700;color:{ACCENT};'>"
            f"{self.title}</div>"
            f"<div style='font-size:16px;'>⇩</div>"
            f"<div style='font-size:11px;'>drop here — {self.formats_text}</div>"
            + (
                f"<div style='font-size:10px;opacity:0.8;'>{zone.get('hint')}</div>"
                if zone.get("hint") else ""
            )
        )
        self.setObjectName("dropZone")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        # BUGFIX (v3.2.7): word wrap was never enabled — with 5 zones
        # sharing one row (Script/Images/Music/Voice/Video), each zone
        # is too narrow for its title on one line, so it silently
        # clipped instead of wrapping ("Scrip", "Imag", "Musi" — matches
        # exactly what was reported). Word wrap fixes it directly;
        # minimum height raised slightly so wrapped text has room.
        self.setWordWrap(True)
        self.setMinimumHeight(100)
        self.setMinimumWidth(140)

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802 - Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:  # noqa: N802 - Qt
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()


class ImportPanel(QWidget):
    """Zones + queue + stage; the view-model does the thinking."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        on_staged: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
        project_folder_provider: Optional[Callable[[], str]] = None,
        status_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._on_staged = on_staged
        self._project_folder_provider = project_folder_provider
        self._status_sink = status_sink
        self._plan: List[Dict[str, Any]] = []

        layout = QVBoxLayout(self)
        heading = QLabel("Import")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.drop_zones: List[DropZone] = []
        zone_row = QHBoxLayout()
        zone_row.setSpacing(8)
        for zone in self.vm.import_zones():
            drop = DropZone(zone)
            drop.files_dropped.connect(self.add_paths)
            self.drop_zones.append(drop)
            zone_row.addWidget(drop, 1)
        layout.addLayout(zone_row)
        # Back-compat: the first (script) zone under the old name.
        self.drop_zone = self.drop_zones[0]
        self.queue_list = QListWidget()
        layout.addWidget(self.queue_list, 1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add Files…")
        self.add_button.clicked.connect(self.open_file_dialog)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_queue)
        self.stage_button = QPushButton("Stage into project")
        self.stage_button.setObjectName("primary")
        self.stage_button.clicked.connect(self.stage_now)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self.stage_button)
        layout.addLayout(buttons)
        self._refresh_view()

    # ------------------------------------------------------------------
    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def add_paths(self, paths: List[str]) -> None:
        fresh = self.vm.import_plan([str(p) for p in paths])
        known = {row["path"] for row in self._plan}
        merged = [row for row in self._plan]
        for row in fresh:
            merged.append(
                dict(row, status="duplicate")
                if row["path"] in known
                else row
            )
            known.add(row["path"])
        self._plan = merged
        self._refresh_view()

    def open_file_dialog(self) -> None:
        paths, _unused = QFileDialog.getOpenFileNames(
            self,
            "Import files",
            "",
            "All supported (*.txt *.json *.csv *.docx *.pdf"
            " *.jpg *.jpeg *.png *.mp3 *.wav);;"
            "Scripts (*.txt *.json *.csv *.docx *.pdf);;"
            "Images (*.jpg *.jpeg *.png);;"
            "Audio (*.mp3 *.wav);;"
            "All files (*)",
        )
        if paths:
            self.add_paths(paths)

    def clear_queue(self) -> None:
        self._plan = []
        self._refresh_view()
        self._status("Import queue cleared.")

    def stage_now(self) -> None:
        if not any(row["status"] == "ready" for row in self._plan):
            self._status("Nothing ready to stage.")
            return
        folder = (
            self._project_folder_provider() or ""
            if self._project_folder_provider is not None
            else ""
        )
        if not folder:
            QMessageBox.information(
                self,
                "Project folder needed",
                "Set a project folder on the Render page first — staged "
                "files land in <project>/imports/.",
            )
            return
        result = self.vm.apply_import(self._plan, folder)
        if not result.get("copied"):
            QMessageBox.warning(
                self, "Import", str(result.get("error") or "Nothing staged.")
            )
            return
        errors = result.get("errors") or []
        message = (
            f"Staged {result['copied']} file(s) into {result['staged_dir']}"
            + (f" — {len(errors)} error(s)." if errors else ".")
        )
        self._status(message)
        self.clear_queue()
        if self._on_staged is not None:
            self._on_staged(
                result.get("script_path"), result.get("images_folder")
            )

    def _refresh_view(self) -> None:
        self.queue_list.clear()
        for index, row in enumerate(self._plan):
            badge = _STATUS_BADGE.get(row["status"], "?")
            text = (
                f"{badge} [{row['kind']}] {row['name']}"
                f"  ({_fmt_size(row['size_bytes'])})"
                + (f" — {row['status']}" if row["status"] != "ready" else "")
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.queue_list.addItem(item)
        self.summary_label.setText(
            self.vm.import_summary(self._plan)["text"]
        )
