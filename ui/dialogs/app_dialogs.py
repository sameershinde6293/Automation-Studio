"""Application dialogs (full-UI Batch 3 + spec File 04 additions).

NewProjectDialog, RecoveryDialog, PreRenderReportDialog and
RenderCompleteDialog are purely presentational: the UiViewModel
dialog mixin computes defaults, validation, recovery candidates,
pre-flight checks, file facts, chapters and Drive readiness so every
rule is headless-tested. Dialogs never touch the engine directly —
results flow back to the shell via plain dicts/callbacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.viewmodel import UiViewModel, slugify_title

_LEVEL_ICON = {"ok": "✓", "warn": "⚠", "error": "✗", "info": "ℹ"}


from ui.theme import (  # noqa: E402
    ACCENT, DANGER, SUCCESS, TEXT_MUTED)


class NewProjectDialog(QDialog):
    """Collect title + folder (+ optional script/images prefill)."""

    def __init__(
        self, viewmodel: UiViewModel, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.setWindowTitle("New Project")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        heading = QLabel("Start a new documentary project")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("The Dark History of …")
        self.title_edit.textChanged.connect(self._suggest_folder)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("projects/my-documentary")
        browse_folder = QPushButton("Browse…")
        browse_folder.clicked.connect(self._browse_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse_folder)
        self.script_edit = QLineEdit()
        self.script_edit.setPlaceholderText("(optional) script file")
        browse_script = QPushButton("Browse…")
        browse_script.clicked.connect(self._browse_script)
        script_row = QHBoxLayout()
        script_row.addWidget(self.script_edit, 1)
        script_row.addWidget(browse_script)
        self.images_edit = QLineEdit()
        self.images_edit.setPlaceholderText("(optional) images folder")
        browse_images = QPushButton("Browse…")
        browse_images.clicked.connect(self._browse_images)
        images_row = QHBoxLayout()
        images_row.addWidget(self.images_edit, 1)
        images_row.addWidget(browse_images)
        form.addRow("Title:", self.title_edit)
        form.addRow("Project folder:", folder_row)
        form.addRow("Script:", script_row)
        form.addRow("Images:", images_row)
        layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setObjectName("muted")
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Create project"
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._folder_touched = False
        self.folder_edit.textEdited.connect(self._mark_folder_touched)
        self.result_payload: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    def _mark_folder_touched(self) -> None:
        self._folder_touched = True

    def _suggest_folder(self) -> None:
        if not self._folder_touched:
            self.folder_edit.setText(
                self.vm.new_project_defaults(self.title_edit.text())["folder"]
            )

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose project folder"
        )
        if path:
            self._folder_touched = True
            self.folder_edit.setText(path)

    def _browse_script(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Choose script", "",
            "Scripts (*.txt *.pdf *.docx *.md);;All files (*)",
        )
        if path:
            self.script_edit.setText(path)

    def _browse_images(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose images folder")
        if path:
            self.images_edit.setText(path)

    def _accept(self) -> None:
        title = self.title_edit.text().strip()
        folder = self.folder_edit.text().strip()
        ok, message = self.vm.validate_new_project(title, folder)
        if not ok:
            self.error_label.setText(message)
            return
        self.result_payload = {
            "title": title,
            "project_folder": folder,
            "script_path": self.script_edit.text().strip() or None,
            "images_folder": self.images_edit.text().strip() or None,
            "slug": slugify_title(title),
        }
        self.accept()


class RecoveryDialog(QDialog):
    """Offer resume/discard for interrupted renders found at boot."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        candidates: List[Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.setWindowTitle("Render recovery")
        self.setMinimumWidth(560)
        self.resume_id: Optional[str] = None
        layout = QVBoxLayout(self)
        heading = QLabel(
            f"{len(candidates)} interrupted render(s) can be resumed"
        )
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        note = QLabel(
            "Autopilot found renders that stopped before finishing "
            "(crash, power cut or cancel). Resume one now, or discard "
            "markers you no longer need."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(
                f"{candidate['title']} — stage {candidate['stage']}"
                f" ({candidate['percent']:.0f}%,"
                f" {candidate['error_count']} errors,"
                f" {candidate['updated_at']})"
            )
            item.setData(Qt.ItemDataRole.UserRole, candidate["project_id"])
            self.list.addItem(item)
        if candidates:
            self.list.setCurrentRow(0)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.resume_button = QPushButton("Resume render")
        self.resume_button.setObjectName("primary")
        self.resume_button.clicked.connect(self._resume)
        self.discard_button = QPushButton("Discard marker")
        self.discard_button.setObjectName("danger")
        self.discard_button.clicked.connect(self._discard)
        later = QPushButton("Decide later")
        later.clicked.connect(self.reject)
        row.addWidget(self.resume_button)
        row.addWidget(self.discard_button)
        row.addStretch(1)
        row.addWidget(later)
        layout.addLayout(row)

    # ------------------------------------------------------------------
    def _selected_id(self) -> Optional[str]:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _resume(self) -> None:
        self.resume_id = self._selected_id()
        if self.resume_id is None:
            self.error_hint("Select an interrupted render first.")
            return
        self.accept()

    def _discard(self) -> None:
        project_id = self._selected_id()
        if project_id is None:
            self.error_hint("Select a marker to discard.")
            return
        ok, message = self.vm.discard_recovery(project_id)
        if not ok:
            self.error_hint(message)
            return
        row = self.list.currentRow()
        self.list.takeItem(row)
        if self.list.count() == 0:
            self.resume_id = None
            self.reject()

    def error_hint(self, message: str) -> None:
        QMessageBox.information(self, "Render recovery", message)


_MATCH_PRESENTATION = {
    "exact": ("✓", SUCCESS, "Exact"),
    "fuzzy": ("≈", ACCENT, "Fuzzy"),
    "no_match": ("✗", DANGER, "No match"),
}


class PreRenderReportDialog(QDialog):
    """Quality checks + estimates BEFORE a render starts (File 04).

    Lists every pre-flight check with an honest ✓/⚠/✗/ℹ marker,
    a duration/scene estimate, and only enables "Start Render" when
    the view-model says the render can actually start.
    """

    def __init__(
        self,
        viewmodel: UiViewModel,
        inputs: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.model = viewmodel.pre_render_report_model(
            str(inputs.get("script_path") or ""),
            str(inputs.get("images_folder") or ""),
            str(inputs.get("project_folder") or ""),
            str(inputs.get("title") or ""),
        )
        self.start_requested = False
        self.setWindowTitle("Pre-render report")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel(
            f"Pre-render report — {self.model['title'] or 'untitled'}"
        )
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.rows_list = QListWidget()
        for row in self.model["rows"]:
            item = QListWidgetItem(
                f"{_LEVEL_ICON.get(row['level'], 'ℹ')}  {row['label']}:"
                f"  {row['value']}"
            )
            self.rows_list.addItem(item)
        layout.addWidget(self.rows_list, 1)
        # Image matching preview (3.0.4 review #1): run the fuzzy
        # match against the chosen images folder and colour each
        # scene row green/yellow/red before burn time is spent.
        self.match_list: Optional[QListWidget] = None
        self.match_note: Optional[QLabel] = None
        match_model = {}
        matcher_fn = getattr(self.vm, "pre_render_match_report", None)
        if callable(matcher_fn):
            try:
                match_model = matcher_fn(
                    images_folder=str(inputs.get("images_folder") or "")
                ) or {}
            except Exception:  # noqa: BLE001 - preview is advisory
                match_model = {}
        if match_model.get("rows"):
            match_head = QLabel("Image matching")
            match_head.setObjectName("panelTitle")
            layout.addWidget(match_head)
            self.match_list = QListWidget()
            for match_row in match_model["rows"]:
                icon, colour, label_text = _MATCH_PRESENTATION.get(
                    str(match_row.get("status")),
                    ("•", TEXT_MUTED, "?"))
                image = match_row.get("image") or "—"
                try:
                    confidence = int(round(
                        float(match_row.get("confidence") or 0.0) * 100))
                except (TypeError, ValueError):
                    confidence = 0
                item = QListWidgetItem(
                    f"{icon}  Scene {match_row.get('scene')}  |  "
                    f"{image}  |  {label_text}  |  {confidence}%")
                item.setForeground(QColor(colour))
                self.match_list.addItem(item)
            self.match_list.setMaximumHeight(130)
            layout.addWidget(self.match_list)
            match_summary = QLabel(
                str(match_model.get("summary_text") or ""))
            match_summary.setObjectName("muted")
            layout.addWidget(match_summary)
        else:
            self.match_note = QLabel(
                "Image matching: "
                + str(match_model.get("note")
                      or "unavailable for this project."))
            self.match_note.setObjectName("muted")
            self.match_note.setWordWrap(True)
            layout.addWidget(self.match_note)
        self.estimate_label = QLabel(f"Estimate: {self.model['estimate_text']}")
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)
        self.summary_label = QLabel(self.model["summary_text"])
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.start_button = buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.start_button.setText("▶ Start Render")
        self.start_button.setObjectName("primary")
        self.start_button.setEnabled(bool(self.model["ready"]))
        if not self.model["ready"]:
            self.start_button.setToolTip(
                "Fix the ✗ rows above, then re-run this report."
            )
        buttons.accepted.connect(self._start)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _start(self) -> None:
        self.start_requested = True
        self.accept()


class RenderCompleteDialog(QDialog):
    """After a render: thumbnail, file facts, chapters, actions.

    Shows the generated thumbnail (or a scene image), output path /
    size / duration, YouTube-ready chapter markers with a copy button,
    and Play / Open-folder / Drive-upload actions (spec File 04).
    """

    def __init__(
        self,
        viewmodel: UiViewModel,
        result: Dict[str, Any],
        on_play: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self._on_play = on_play
        self.model = viewmodel.render_complete_model(result)
        self.setWindowTitle("Render complete")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel("✓ Your documentary is ready")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)

        top = QHBoxLayout()
        self.thumb_label = QLabel()
        self.thumb_label.setObjectName("sceneThumb")
        self.thumb_label.setFixedSize(160, 90)
        thumb = self.model.get("thumbnail_path")
        pixmap = QPixmap(str(thumb)) if thumb else QPixmap()
        if pixmap.isNull():
            self.thumb_label.setText("no thumbnail")
            self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            self.thumb_label.setPixmap(
                pixmap.scaled(
                    160,
                    90,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        top.addWidget(self.thumb_label, 0, Qt.AlignmentFlag.AlignTop)
        details = self.model["output"] or "(no output path reported)"
        facts = []
        if self.model["size_text"]:
            facts.append(f"Size: {self.model['size_text']}")
        if self.model["duration_text"]:
            facts.append(f"Duration: {self.model['duration_text']}")
        if facts:
            details += "\n" + "   ·   ".join(facts)
        label = QLabel(details)
        label.setWordWrap(True)
        top.addWidget(label, 1)
        layout.addLayout(top)

        chapters_text = str(self.model.get("chapters_text") or "")
        if chapters_text:
            chapters_head = QLabel("YouTube chapters")
            chapters_head.setObjectName("panelTitle")
            layout.addWidget(chapters_head)
            self.chapters_edit = QPlainTextEdit()
            self.chapters_edit.setReadOnly(True)
            self.chapters_edit.setPlainText(chapters_text)
            self.chapters_edit.setMaximumHeight(110)
            layout.addWidget(self.chapters_edit)

        # 3.0.4 review #2: expandable inline warnings instead of a
        # bare count that pointed at the log pane.
        warnings = []
        lister = getattr(self.vm, "render_warnings_list", None)
        if callable(lister):
            try:
                _ok, _msg, listed = lister()
                warnings = [str(item) for item in (listed or [])]
            except (TypeError, ValueError):
                warnings = []
        if not warnings:
            warnings = [
                str(item) for item in (self.model.get("warnings") or [])
            ]
        self.warn_toggle: Optional[QPushButton] = None
        self.warnings_list: Optional[QListWidget] = None
        if warnings:
            count = len(warnings)
            self.warn_toggle = QPushButton(f"▸ Show {count} warning(s)")
            self.warn_toggle.setFlat(True)
            self.warn_toggle.setToolTip(
                "Expand the list inline — full details also live in "
                "the log pane.")
            self.warn_toggle.clicked.connect(self._toggle_warnings)
            layout.addWidget(self.warn_toggle)
            self.warnings_list = QListWidget()
            for warning in warnings:
                warn_item = QListWidgetItem(f"⚠  {warning}")
                warn_item.setForeground(QColor(ACCENT))
                self.warnings_list.addItem(warn_item)
            self.warnings_list.setMaximumHeight(110)
            self.warnings_list.setVisible(False)
            layout.addWidget(self.warnings_list)
        row = QHBoxLayout()
        self.play_button = QPushButton("▶ Play video")
        self.play_button.setObjectName("primary")
        self.play_button.setEnabled(bool(self.model["exists"]))
        self.play_button.clicked.connect(self._play)
        self.reveal_button = QPushButton("📁 Open folder")
        self.reveal_button.setEnabled(bool(self.model["exists"]))
        self.reveal_button.clicked.connect(self._reveal)
        self.copy_chapters_button = QPushButton("⧉ Copy chapters")
        self.copy_chapters_button.setEnabled(bool(chapters_text))
        self.copy_chapters_button.clicked.connect(self._copy_chapters)
        self.upload_button = QPushButton("⬆ Upload to Drive")
        drive_ready = bool(self.model["drive_ready"])
        self.upload_button.setEnabled(drive_ready)
        if not drive_ready:
            self.upload_button.setToolTip(
                self.model["drive_status_text"]
                or "Google Drive backup not configured"
            )
        self.upload_button.clicked.connect(self._upload)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.play_button)
        row.addWidget(self.reveal_button)
        row.addWidget(self.copy_chapters_button)
        row.addWidget(self.upload_button)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    # ------------------------------------------------------------------
    def _toggle_warnings(self) -> None:
        if self.warnings_list is None or self.warn_toggle is None:
            return
        show = self.warnings_list.isHidden()
        self.warnings_list.setVisible(show)
        count = self.warnings_list.count()
        arrow = "▾ Hide" if show else "▸ Show"
        self.warn_toggle.setText(f"{arrow} {count} warning(s)")

    def _play(self) -> None:
        if self._on_play is not None and self.model["exists"]:
            self._on_play(self.model["output"])
        self.accept()

    def _reveal(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        if self.model["exists"]:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(self.model["output"]).parent))
            )

    def _copy_chapters(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(self.model.get("chapters_text") or ""))
        self.copy_chapters_button.setText("✓ Copied")

    def _upload(self) -> None:
        ok, message = self.vm.upload_render_to_drive(self.model["output"])
        if ok:
            QMessageBox.information(self, "Google Drive backup", message)
        else:
            QMessageBox.warning(self, "Google Drive backup", message)


class VoiceCloneDialog(QDialog):
    """Tools -> Voice Clone (spec §15): queue a clone from a WAV/MP3."""

    def __init__(
        self, viewmodel: UiViewModel, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.setWindowTitle("Voice Clone")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel("Clone a voice from a reference sample")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.existing = QListWidget()
        for clone in self.vm.voice_clone_model()["clones"]:
            state = "ready" if clone["ready"] else "awaiting engine"
            self.existing.addItem(
                f"{clone['name']}  ·  {clone['engine']}  ·  {state}"
            )
        layout.addWidget(self.existing)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("My Narration Voice")
        form.addRow("Voice name:", self.name_edit)
        self.sample_edit = QLineEdit()
        self.sample_edit.setPlaceholderText("reference .wav / .mp3")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        sample_row = QHBoxLayout()
        sample_row.addWidget(self.sample_edit, 1)
        sample_row.addWidget(browse)
        form.addRow("Sample:", sample_row)
        layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setObjectName("muted")
        layout.addWidget(self.error_label)
        note = QLabel(
            "Clones are stored as QUEUED: training runs when an "
            "XTTS-class engine is installed (Engine Manager)."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        # Review fix (3.0.3): consent is legally required before a
        # voice may be queued for cloning (impersonation liability).
        self.consent_check = QCheckBox(
            "I confirm I have the right to clone this voice and, "
            "if it is not my own, that I have the speaker's consent."
        )
        self.consent_check.setToolTip(
            "Required — cloned voices can be misused for "
            "impersonation; only clone with permission."
        )
        layout.addWidget(self.consent_check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Queue clone"
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        create = buttons.button(QDialogButtonBox.StandardButton.Ok)
        create.setEnabled(False)  # gated by the consent checkbox
        self.consent_check.toggled.connect(create.setEnabled)
        layout.addWidget(buttons)
        self.added = False

    def _browse(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Choose reference sample", "",
            "Audio (*.wav *.mp3);;All files (*)",
        )
        if path:
            self.sample_edit.setText(path)

    def _accept(self) -> None:
        if not self.consent_check.isChecked():
            self.error_label.setText(
                "Please confirm consent before cloning."
            )
            return
        ok, message = self.vm.add_voice_clone(
            self.name_edit.text(), self.sample_edit.text()
        )
        if not ok:
            self.error_label.setText(message)
            return
        self.added = True
        self.accept()


class EngineInstallDialog(QDialog):
    """Tools -> Engine Manager (spec §15): what is installed, where."""

    def __init__(
        self, viewmodel: UiViewModel, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.setWindowTitle("Engine Manager")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        heading = QLabel("Offline engines")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.rows_list = QListWidget()
        layout.addWidget(self.rows_list, 1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        row = QHBoxLayout()
        refresh = QPushButton("↻ Re-check")
        refresh.clicked.connect(self._reload)
        open_folder = QPushButton("📁 Open engines folder")
        open_folder.clicked.connect(self._open_engines)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(refresh)
        row.addWidget(open_folder)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        note = QLabel(
            "Install = copy the engine binaries into engines\\ (see "
            "AFTER_INSTALL.txt); Autopilot never downloads anything."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self._reload()

    def _reload(self) -> None:
        model = self.vm.engine_install_model()
        self.rows_list.clear()
        for row in model["rows"]:
            mark = "✓" if row["found"] else "✗"
            self.rows_list.addItem(
                f"{mark}  {row['name']}  —  {row['path'] or 'not found'}"
                f"  (needed for: {row['needed_for']})"
            )
        self.summary_label.setText(model["summary"])

    def _open_engines(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        folder = Path("engines")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(folder.resolve()))
        )


class KeyGeneratorDialog(QDialog):
    """Tools -> My License / Machine ID: honest read-only HWID helper."""

    def __init__(
        self, viewmodel: UiViewModel, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.model = viewmodel.key_generator_model()
        self.setWindowTitle("My License / Machine ID")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        heading = QLabel("Machine ID for license issuance")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.hwid_edit = QLineEdit(self.model["hwid"])
        self.hwid_edit.setReadOnly(True)
        layout.addWidget(self.hwid_edit)
        self.status_label = QLabel(
            f"License status: {self.model['license_status']}"
        )
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)
        note = QLabel(self.model["message"])
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.copy_button = QPushButton("⧉ Copy machine ID")
        self.copy_button.setEnabled(bool(self.model["hwid"]))
        self.copy_button.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.model["hwid"])
        self.copy_button.setText("✓ Copied")


class ChannelProfileDialog(QDialog):
    """Project -> Channel Profile Manager (workflow_spec)."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        on_changed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self._on_changed = on_changed
        self.setWindowTitle("Channel Profile Manager")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        heading = QLabel("Channel profiles")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show_details)
        layout.addWidget(self.list, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(120)
        layout.addWidget(self.details)
        row = QHBoxLayout()
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self._duplicate)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete)
        self.default_button = QPushButton("Set as default")
        self.default_button.setObjectName("primary")
        self.default_button.clicked.connect(self._set_default)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.duplicate_button)
        row.addWidget(self.delete_button)
        row.addWidget(self.default_button)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        self._reload()

    def _reload(self) -> None:
        self.list.clear()
        for row in self.vm.channel_profile_rows():
            item = QListWidgetItem(row["name"])
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _selected(self) -> Dict[str, Any]:
        item = self.list.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        return dict(data) if isinstance(data, dict) else {}

    def _show_details(self) -> None:
        self.details.setPlainText(self._selected().get("details", ""))

    def _act(self, fn: Callable[[str], Any]) -> None:
        profile_id = self._selected().get("id")
        if not profile_id:
            return
        ok, message = fn(str(profile_id))
        if not ok:
            QMessageBox.warning(self, "Channel profiles", message)
            return
        self._reload()
        if self._on_changed is not None:
            self._on_changed()
        QMessageBox.information(self, "Channel profiles", message)

    def _duplicate(self) -> None:
        self._act(self.vm.channel_profile_duplicate)

    def _delete(self) -> None:
        self._act(self.vm.channel_profile_delete)

    def _set_default(self) -> None:
        self._act(self.vm.channel_profile_set_default)


class QualityCheckDialog(QDialog):
    """Project -> Quality Check results (quality_checker seam)."""

    def __init__(
        self, viewmodel: UiViewModel, project_id: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.model = viewmodel.quality_run(project_id)
        self.setWindowTitle("Quality Check")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel(f"Quality check — {project_id or 'project'}")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.rows_list = QListWidget()
        icon = {"pass": "✓", "ok": "✓", "warn": "⚠", "fail": "✗",
                "error": "✗"}
        for row in self.model["rows"]:
            mark = icon.get(str(row["status"]).lower(), "ℹ")
            self.rows_list.addItem(
                f"{mark}  {row['name']}"
                + (f" — {row['detail']}" if row.get("detail") else "")
            )
        layout.addWidget(self.rows_list, 1)
        summary = QLabel(self.model["summary"])
        summary.setObjectName("muted")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class FirstRunWizard(QDialog):
    """workflow_spec: first-time setup when engines are missing."""

    def __init__(
        self, viewmodel: UiViewModel, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.model = viewmodel.first_run_model()
        self.setWindowTitle("Welcome to Autopilot")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel("First-time setup — TTS & video engines")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        rows = QListWidget()
        for row in self.model["rows"]:
            mark = "✓" if row["found"] else "✗"
            rows.addItem(
                f"{mark}  {row['name']}  —  {row['path'] or 'missing'}"
                f"  (needed for: {row['needed_for']})"
            )
        layout.addWidget(rows)
        note = QLabel(
            "Autopilot is 100% offline: engines ship as binaries you "
            "copy into the engines\\ folder (no downloads, no "
            "accounts). The app runs every non-render feature without "
            "them and tells you exactly what is missing."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        summary = QLabel(self.model["summary"])
        summary.setObjectName("muted")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Got it — don't show again"
        )
        buttons.accepted.connect(self._finish)
        layout.addWidget(buttons)

    def _finish(self) -> None:
        self.vm.mark_first_run_done()
        self.accept()


class UserGuideDialog(QDialog):
    """Help -> User Guide: shipped docs rendered as plain text."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("User Guide")
        self.setMinimumSize(640, 520)
        layout = QVBoxLayout(self)
        heading = QLabel("Autopilot — User Guide")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        body = ""
        for candidate in (
            Path("docs/USER_GUIDE.md"), Path("README.md"),
        ):
            if candidate.is_file():
                try:
                    body = candidate.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    break
                except OSError:
                    body = ""
        if not body:
            body = (
                "Quick start:\n"
                "1. New Project (Ctrl+N) or Import Files (Ctrl+I).\n"
                "2. Pick script + images on the Render page.\n"
                "3. Optional: Pre-Render Report (Ctrl+Shift+R).\n"
                "4. Start Render (F9). Quick Preview is F5.\n"
                "5. Studio page: import zones, tabbed preview, visual "
                "timeline with chapter markers and waveform.\n"
                "6. Grade/Audio pages polish the look + mix; Voices "
                "manages TTS voices; Batch queues many renders."
            )
        text.setPlainText(body)
        layout.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class LicenseScreenDialog(QDialog):
    """Boot-time License Screen (ui_specification §3).

    Shows the machine HWID with a Copy button so the user can request
    a key, a key entry + Activate wired to ``vm.activate_license``,
    current status, and an honest "continue in trial mode" escape.
    Purely presentational — all facts come from
    ``vm.license_screen_model()``.
    """

    def __init__(
        self, vm: UiViewModel, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.vm = vm
        model = vm.license_screen_model()
        self.setWindowTitle("Autopilot — License")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        title = QLabel("🔑  License Activation")
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        status = QLabel(str(model.get("message") or ""))
        status.setWordWrap(True)
        layout.addWidget(status)
        layout.addWidget(QLabel("Your machine ID (HWID):"))
        hwid_row = QHBoxLayout()
        self.hwid_edit = QLineEdit(str(model.get("hwid") or ""))
        self.hwid_edit.setReadOnly(True)
        self.hwid_edit.setToolTip(
            "Send this ID to your vendor to receive a license key")
        copy_btn = QPushButton("Copy")
        copy_btn.setToolTip("Copy the HWID to the clipboard")
        copy_btn.clicked.connect(self._copy_hwid)
        hwid_row.addWidget(self.hwid_edit, 1)
        hwid_row.addWidget(copy_btn)
        layout.addLayout(hwid_row)
        layout.addWidget(QLabel("License key:"))
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("AUTOPILOT-XXXX-XXXX-XXXX")
        self.key_edit.setToolTip(
            "Paste the key issued for THIS machine ID")
        layout.addWidget(self.key_edit)
        buttons = QHBoxLayout()
        activate = QPushButton("Activate")
        activate.setObjectName("primary")
        activate.setToolTip("Validate and store the license key")
        activate.clicked.connect(self._activate)
        later = QPushButton("Continue in trial mode")
        later.setToolTip("You can activate later from Help → License")
        later.clicked.connect(self.accept)
        buttons.addWidget(activate)
        buttons.addWidget(later)
        layout.addLayout(buttons)
        note = QLabel(
            "Keys are bound to this machine's HWID. Tools → My "
            "License / Machine ID prints the same ID for your records."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

    def _copy_hwid(self) -> None:
        QApplication.clipboard().setText(self.hwid_edit.text())

    def _activate(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.information(
                self, "License", "Paste a license key first.")
            return
        ok, message = self.vm.activate_license(key)
        title = "License" if ok else "Activation failed"
        (QMessageBox.information if ok
         else QMessageBox.warning)(self, title, str(message))
        if ok:
            self.accept()


_EXPORT_FIELDS = {
    "export_audio_only": (
        ("text", "script", "Script to narrate", "", True),
        ("save", "out", "Output WAV", "WAV audio (*.wav)", True),
    ),
    "export_audio_mix": (
        ("open", "narration", "Narration file",
         "Audio (*.wav *.mp3)", True),
        ("open", "music", "Music file (optional)",
         "Audio (*.wav *.mp3)", False),
        ("open", "sfx", "SFX file (optional)",
         "Audio (*.wav *.mp3)", False),
        ("save", "out", "Output file",
         "WAV audio (*.wav);;MP3 audio (*.mp3)", True),
    ),
    "burn_subtitles": (
        ("open", "video", "Video file",
         "Video (*.mp4 *.mkv *.mov)", True),
        ("open", "srt", "Subtitle file", "Subtitles (*.srt)", True),
        ("save", "out", "Output video", "MP4 video (*.mp4)", True),
    ),
    "export_thumbnails": (
        ("project", "project_id", "Project", "", True),
        ("dir", "out_dir", "Output folder", "", True),
    ),
    "export_storyboard_pdf": (
        ("project", "project_id", "Project", "", True),
        ("save", "out", "Output PDF", "PDF document (*.pdf)", True),
    ),
}


class ExportJobDialog(QDialog):
    """One parameterized form for every File ▸ Export job (35-39).

    Field specs live in _EXPORT_FIELDS; collected values are handed
    back as ``self.job`` — the shell calls the view-model brains
    (headless-tested) so the dialog never touches FFmpeg/engine.
    """

    def __init__(
        self, vm: UiViewModel, kind: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.vm = vm
        self.kind = kind
        self.job: Optional[Dict[str, Any]] = None
        model = next(
            m for m in vm.export_menu_model() if m["id"] == kind)
        self.setWindowTitle(model["label"].replace("&", ""))
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        desc = QLabel(model["desc"])
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        self._fields: Dict[str, Any] = {}
        self._required: Dict[str, bool] = {}
        form = QFormLayout()
        for mode, key, label, pattern, required in (
                _EXPORT_FIELDS[kind]):
            self._required[key] = required
            if mode == "project":
                combo = QComboBox()
                try:
                    rows = self.vm.refresh_projects(limit=50)
                except Exception:  # noqa: BLE001
                    rows = []
                for row in rows:
                    combo.addItem(
                        str(row.get("title") or row.get("id")),
                        row.get("id"))
                if combo.count() == 0:
                    combo.addItem("(no projects yet)", "")
                combo.setToolTip("Project whose scenes are exported")
                form.addRow(f"{label}:", combo)
                self._fields[key] = combo
                continue
            if mode == "text":
                edit = QPlainTextEdit()
                edit.setPlaceholderText(
                    "Paste the narration script here…")
                edit.setMinimumHeight(90)
                form.addRow(f"{label}:", edit)
                self._fields[key] = edit
                continue
            line = QLineEdit()
            row = QHBoxLayout()
            browse = QPushButton("Browse…")
            browse.setToolTip(f"Choose {label.lower()}")
            browse.clicked.connect(
                lambda _c=False, m=mode, e=line, p=pattern:
                self._browse(m, e, p))
            row.addWidget(line, 1)
            row.addWidget(browse)
            holder = QWidget()
            holder.setLayout(row)
            form.addRow(f"{label}:", holder)
            self._fields[key] = line
        layout.addLayout(form)
        run = QPushButton("▶ Run Export")
        run.setObjectName("primary")
        run.setToolTip("Runs in the background — watch the progress "
                       "panel below the window")
        run.clicked.connect(self._accept)
        layout.addWidget(run, 0, Qt.AlignmentFlag.AlignRight)

    def _browse(self, mode: str, edit: QLineEdit,
                pattern: str) -> None:
        if mode == "open":
            path, _flt = QFileDialog.getOpenFileName(
                self, "Choose file", "", pattern)
        elif mode == "save":
            path, _flt = QFileDialog.getSaveFileName(
                self, "Choose output", "", pattern)
        else:
            path = QFileDialog.getExistingDirectory(
                self, "Choose output folder")
        if path:
            edit.setText(path)

    def _accept(self) -> None:
        job: Dict[str, Any] = {}
        for key, widget in self._fields.items():
            if isinstance(widget, QComboBox):
                value = widget.currentData() or ""
            elif isinstance(widget, QPlainTextEdit):
                value = widget.toPlainText()
            else:
                value = widget.text()
            value = str(value).strip()
            if self._required.get(key) and not value:
                QMessageBox.information(
                    self, "Export", f"Fill in: {key}")
                return
            job[key] = value
        self.job = job
        self.accept()
