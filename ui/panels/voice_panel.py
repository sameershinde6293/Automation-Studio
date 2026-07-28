"""Voice Store panel (ui_specification.txt Section 13).

Search + gender/language filters over the voice catalog (engine
module feeds it when available; DB cache is the offline fallback —
the view-model reports which). Voice cards show language/gender/
quality/size and Install/Remove buttons via the store module seam;
Preview plays the catalog preview clip when one exists locally,
otherwise it says so plainly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.theme import ACCENT
from ui.viewmodel import UiViewModel


class VoiceCard(QFrame):
    """One voice: details + Install/Remove (+preview when local)."""

    def __init__(
        self,
        voice: Dict[str, Any],
        on_install: Callable[[str], None],
        on_remove: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.setObjectName("sceneCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # UI REDESIGN (v3.2.7): fixed card width so the grid renders
        # even columns (previously each card just took its natural
        # content width in a single-column vertical list).
        self.setMinimumWidth(200)
        self.setMaximumWidth(280)
        self.voice_id = voice["id"]
        layout = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel(voice["name"])
        title.setStyleSheet(f"color: {ACCENT}; font-weight: 600; font-size: 14px;")
        title_row.addWidget(title, 1)
        stars = "★" * max(0, min(5, int(voice.get("quality") or 0)))
        if stars:
            rating = QLabel(stars)
            rating.setStyleSheet(f"color: {ACCENT};")
            title_row.addWidget(rating)
        layout.addLayout(title_row)

        # Badge pills instead of one dense "engine · lang · gender ·
        # size" text line — each attribute reads at a glance.
        badges_row = QHBoxLayout()
        badges_row.setSpacing(6)
        for text in (voice["engine"], voice["language"], voice["gender"]):
            if not text:
                continue
            badge = QLabel(str(text))
            badge.setObjectName("badge")
            badges_row.addWidget(badge)
        badges_row.addStretch(1)
        layout.addLayout(badges_row)

        size_label = QLabel(f"{voice['size_mb']:.0f} MB")
        size_label.setObjectName("muted")
        layout.addWidget(size_label)

        if voice.get("style"):
            style = QLabel(voice["style"] + (
                f" — {voice['description'][:90]}"
                if voice.get("description") else ""
            ))
            style.setObjectName("muted")
            style.setWordWrap(True)
            layout.addWidget(style)
        row = QHBoxLayout()
        state = QLabel("✓ installed" if voice["installed"] else "not installed")
        state.setObjectName(
            "badgeSuccess" if voice["installed"] else "badgeMuted"
        )
        row.addWidget(state)
        row.addStretch(1)
        self.action = QPushButton(
            "Remove" if voice["installed"] else "Install"
        )
        self.action.setObjectName(
            "danger" if voice["installed"] else "primary"
        )
        if voice["installed"]:
            self.action.clicked.connect(
                lambda _c=False, v=self.voice_id: on_remove(v)
            )
        else:
            self.action.clicked.connect(
                lambda _c=False, v=self.voice_id: on_install(v)
            )
        row.addWidget(self.action)
        layout.addLayout(row)


class VoicePanel(QWidget):
    """Search/filter + voice cards grid (§13)."""

    def __init__(
        self,
        viewmodel: UiViewModel,
        status_sink: Optional[Callable[[str], None]] = None,
        preview_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self.vm = viewmodel
        self._status_sink = status_sink
        self._preview_callback = preview_callback
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("Voice Store")
        heading.setObjectName("panelTitle")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search voices…")
        self.search_edit.textChanged.connect(self.refresh)
        self.gender_combo = QComboBox()
        self.gender_combo.addItem("Any gender", "")
        self.gender_combo.addItem("Male", "male")
        self.gender_combo.addItem("Female", "female")
        self.gender_combo.currentIndexChanged.connect(self.refresh)
        self.language_combo = QComboBox()
        self.language_combo.addItem("(All languages)", "")
        for code in self.vm.voice_languages():
            self.language_combo.addItem(code, code)
        self.language_combo.currentIndexChanged.connect(self.refresh)
        refresh = QPushButton("↻")
        refresh.setToolTip("Reload catalog")
        refresh.clicked.connect(self.refresh)
        header.addWidget(heading, 1)
        header.addWidget(self.search_edit, 2)
        header.addWidget(self.gender_combo)
        header.addWidget(self.language_combo)
        header.addWidget(refresh)
        layout.addLayout(header)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        layout.addWidget(self.summary_label)
        # Engine installation wizard strip (deep-dive fix #2): the
        # store can only render voices for engines that exist.
        engine_card = QFrame()
        engine_card.setObjectName("card")
        ec = QHBoxLayout(engine_card)
        self.engines_label = QLabel(self._engines_summary())
        self.engines_label.setObjectName("muted")
        self.engines_label.setWordWrap(True)
        install = QPushButton("🧩 Engine Install…")
        install.setToolTip(
            "What each TTS engine needs and where to put it")
        install.clicked.connect(self._engine_install)
        wizard = QPushButton("🧭 Setup Wizard…")
        wizard.setToolTip("Guided first-time setup for TTS engines")
        wizard.clicked.connect(self._setup_wizard)
        ec.addWidget(self.engines_label, 1)
        ec.addWidget(install)
        ec.addWidget(wizard)
        layout.addWidget(engine_card)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.cards_host = QWidget()
        # UI REDESIGN (v3.2.7): was a single-column QVBoxLayout — every
        # voice took a full-width row. Now a proper multi-column grid,
        # matching how a voice picker should actually look (cards side
        # by side, not a long scrolling list of one-per-line entries).
        self.cards_layout = QGridLayout(self.cards_host)
        self.cards_layout.setSpacing(12)
        self.cards_layout.setContentsMargins(2, 2, 2, 2)
        self._cards_columns = 3
        scroll.setWidget(self.cards_host)
        layout.addWidget(scroll, 1)
        self.cards: List[VoiceCard] = []
        self.refresh()

    # ------------------------------------------------------------------
    def _engines_summary(self) -> str:
        try:
            status = self.vm.engines_status()
        except Exception:  # noqa: BLE001 - advisory line only
            return "TTS engines: status unavailable"
        piper = "found" if status.get("piper") else "missing"
        return (
            f"TTS engines — modules: {status.get('modules_loaded', 0)} "
            f"· Piper: {piper} · drop engine folders under engines/ "
            f"then run the wizard"
        )

    def _engine_install(self) -> None:
        from ui.dialogs.app_dialogs import EngineInstallDialog

        EngineInstallDialog(self.vm, self).exec()
        self.refresh()

    def _setup_wizard(self) -> None:
        from ui.dialogs.app_dialogs import FirstRunWizard

        FirstRunWizard(self.vm, self).exec()
        self.engines_label.setText(self._engines_summary())
        self.refresh()

    def _status(self, text: str) -> None:
        if self._status_sink is not None:
            self._status_sink(text)

    def refresh(self) -> None:
        # Review fix (3.0.3): the language filter grows with the
        # catalog (new engines, new languages) — current selection
        # is preserved whenever possible.
        current_lang = self.language_combo.currentData()
        known = {
            self.language_combo.itemData(i)
            for i in range(self.language_combo.count())
        }
        self.language_combo.blockSignals(True)
        for code in self.vm.voice_languages():
            if code not in known:
                self.language_combo.addItem(code, code)
        index = self.language_combo.findData(current_lang)
        self.language_combo.setCurrentIndex(max(0, index))
        self.language_combo.blockSignals(False)
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.cards = []
        model = self.vm.voice_store_model(
            query=self.search_edit.text(),
            gender=str(self.gender_combo.currentData() or ""),
            language=str(self.language_combo.currentData() or ""),
        )
        self.summary_label.setText(model["summary_text"])
        columns = self._cards_columns
        for index, voice in enumerate(model["voices"]):
            card = VoiceCard(voice, self._install, self._remove)
            self.cards.append(card)
            self.cards_layout.addWidget(card, index // columns, index % columns)

    def _install(self, voice_id: str) -> None:
        ok, message = self.vm.voice_install(voice_id)
        self._status(message)
        self.refresh()

    def _remove(self, voice_id: str) -> None:
        ok, message = self.vm.voice_remove(voice_id)
        self._status(message)
        self.refresh()
