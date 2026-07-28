"""Pronunciation dictionary manager dialog (v3.2.13).

Presentational only — all file I/O and validation happens in
UiViewModel (pronunciation_presets / load_pronunciation_entries /
save_pronunciation_entries), matching this app's dialog convention.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ui.viewmodel import UiViewModel


class PronunciationManagerDialog(QDialog):
    """Add/edit/delete pronunciation entries; import presets; save/export."""

    def __init__(self, viewmodel: UiViewModel, current_path: str, parent=None) -> None:
        super().__init__(parent)
        self.vm = viewmodel
        self.setWindowTitle("Pronunciation Dictionary")
        self.resize(640, 480)
        self._current_path = current_path

        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("File:"))
        self.path_edit = QLineEdit(current_path)
        self.path_edit.setPlaceholderText("(unsaved — choose Save As)")
        path_row.addWidget(self.path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Load preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("(choose a starter preset)", "")
        for preset in self.vm.pronunciation_presets():
            self.preset_combo.addItem(preset["label"], preset["path"])
        preset_row.addWidget(self.preset_combo, 1)
        load_preset_btn = QPushButton("Import preset (merge)")
        load_preset_btn.setToolTip(
            "Adds the preset's words to the current table — doesn't "
            "remove anything already there")
        load_preset_btn.clicked.connect(self._import_preset)
        preset_row.addWidget(load_preset_btn)
        layout.addLayout(preset_row)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Word (as written)", "Spoken as"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        row_buttons = QHBoxLayout()
        add_btn = QPushButton("+ Add word")
        add_btn.clicked.connect(self._add_row)
        remove_btn = QPushButton("Delete selected")
        remove_btn.setObjectName("danger")
        remove_btn.clicked.connect(self._remove_selected)
        row_buttons.addWidget(add_btn)
        row_buttons.addWidget(remove_btn)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox()
        save_as_btn = buttons.addButton(
            "Save As…", QDialogButtonBox.ButtonRole.ActionRole)
        save_as_btn.clicked.connect(self._save_as)
        save_btn = buttons.addButton(
            "Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save)
        cancel_btn = buttons.addButton(
            QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

        # Result the caller reads after exec() — the path that should
        # become the active voice_pronunciation setting, or None if the
        # user cancelled / never saved anything.
        self.result_path: Optional[str] = None

        if current_path:
            self._load_from(current_path)

    def _load_from(self, path: str) -> None:
        entries = self.vm.load_pronunciation_entries(path)
        self.table.setRowCount(0)
        for entry in entries:
            self._add_row(entry["word"], entry["pronunciation"])
        self.status_label.setText(f"{len(entries)} entries loaded from {path}")

    def _add_row(self, word: str = "", pronunciation: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(word))
        self.table.setItem(row, 1, QTableWidgetItem(pronunciation))

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _browse(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, "Pronunciation dictionary", "",
            "Pronunciation dictionaries (*.json);;All files (*)",
        )
        if path:
            self.path_edit.setText(path)
            self._load_from(path)

    def _import_preset(self) -> None:
        path = str(self.preset_combo.currentData() or "")
        if not path:
            return
        entries = self.vm.load_pronunciation_entries(path)
        existing_words = {
            self.table.item(r, 0).text().strip().lower()
            for r in range(self.table.rowCount())
            if self.table.item(r, 0)
        }
        added = 0
        for entry in entries:
            if entry["word"].strip().lower() in existing_words:
                continue  # don't clobber a word the user already customized
            self._add_row(entry["word"], entry["pronunciation"])
            added += 1
        self.status_label.setText(
            f"Imported {added} new entries from preset "
            f"({len(entries) - added} already present, skipped)."
        )

    def _collect_entries(self) -> list:
        entries = []
        for row in range(self.table.rowCount()):
            word_item = self.table.item(row, 0)
            pron_item = self.table.item(row, 1)
            entries.append({
                "word": word_item.text() if word_item else "",
                "pronunciation": pron_item.text() if pron_item else "",
            })
        return entries

    def _save_as(self) -> None:
        path, _unused = QFileDialog.getSaveFileName(
            self, "Save pronunciation dictionary as", "",
            "Pronunciation dictionaries (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        self.path_edit.setText(path)
        self._do_save(path)

    def _save(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            self._save_as()
            return
        self._do_save(path)

    def _do_save(self, path: str) -> None:
        ok, message = self.vm.save_pronunciation_entries(
            path, self._collect_entries())
        if not ok:
            QMessageBox.warning(self, "Could not save", message)
            return
        self.result_path = path
        self.accept()
