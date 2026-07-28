"""Themes for the Autopilot UI (D.6 dark identity + full UI theme switcher).

Stylesheets are plain strings with NO Qt imports so design tweaks can
be asserted headless. ``apply_theme`` imports Qt lazily inside the
function, so importing this module never requires PyQt6.

THEMES is the single source of truth for the View -> Theme actions;
the viewmodel reads THEME_NAMES, the shell calls
``apply_theme(app, name)``. Palette constants below describe the dark
identity (the default); light variants live in LIGHT_*.
"""

from __future__ import annotations

# Palette (ui_specification dark identity — #1A1A2E family)
WINDOW_BG = "#1a1a2e"
PANEL_BG = "#23233c"
PANEL_ALT = "#2c2c4a"
TEXT_MAIN = "#e8e8ec"
TEXT_MUTED = "#9a9ab0"
ACCENT = "#e0a458"       # amber — primary action / progress chunk
ACCENT_SOFT = "#3d3560"
BORDER = "#3c3c58"
SUCCESS = "#63c98d"
DANGER = "#e06464"

# Light palette (mirrors the dark structure for the theme switcher)
LIGHT_WINDOW_BG = "#f3f1ec"
LIGHT_PANEL_BG = "#e6e3db"
LIGHT_PANEL_ALT = "#d9d5ca"
LIGHT_TEXT_MAIN = "#26242e"
LIGHT_TEXT_MUTED = "#6b6875"
LIGHT_ACCENT = "#b5772a"
LIGHT_ACCENT_SOFT = "#e8d5b8"
LIGHT_BORDER = "#c7c2b4"
LIGHT_SUCCESS = "#2e8b57"
LIGHT_DANGER = "#b34646"

_QSS_TEMPLATE = """
QMainWindow, QDialog, QWidget {{
    background-color: {window_bg};
    color: {text_main};
    font-family: "Montserrat", "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
}}
QMenuBar, QMenu {{
    background-color: {panel_bg};
    color: {text_main};
    border: 1px solid {border};
}}
QMenu::item:selected, QMenuBar::item:selected {{
    background-color: {accent_soft};
}}
QToolBar {{
    background-color: {panel_bg};
    border: none;
    border-bottom: 1px solid {border};
    spacing: 6px;
    padding: 4px 8px;
}}
QToolBar::separator {{
    background-color: {border};
    width: 1px;
    margin: 4px 6px;
}}
QToolButton {{
    background-color: transparent;
    color: {text_main};
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 5px 10px;
}}
QToolButton:hover {{ border-color: {accent}; }}
QToolButton:pressed {{ background-color: {accent_soft}; }}
QToolButton:disabled {{ color: {text_muted}; }}
QLabel {{ background-color: transparent; }}
QLabel#muted {{ color: {text_muted}; }}
QLabel#panelTitle {{
    color: {accent};
    font-weight: 600;
    padding: 2px 0;
}}
QLabel#splashTitle {{
    color: {accent};
    font-size: 26px;
    font-weight: 800;
}}
QLabel#dropZone {{
    color: {text_muted};
    border: 2px dashed {border};
    border-radius: 8px;
    padding: 18px;
}}
QLabel#dropZone:hover {{
    border: 2px dashed {accent};
    background-color: {accent_soft};
}}
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QSpinBox,
QDoubleSpinBox, QListWidget, QTreeWidget, QTableWidget {{
    background-color: {panel_bg};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {accent_soft};
}}
QLineEdit:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background-color: {panel_bg};
    color: {text_main};
    selection-background-color: {accent_soft};
}}
QPushButton {{
    background-color: {panel_alt};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:pressed {{ background-color: {accent_soft}; }}
QPushButton:disabled {{ color: {text_muted}; background-color: {panel_bg}; }}
QPushButton#primary {{
    background-color: {accent};
    color: {accent_contrast};
    font-weight: 700;
}}
QPushButton#primary:hover {{ background-color: {accent_hover}; }}
QPushButton#danger {{ border-color: {danger}; color: {danger}; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {border};
    border-radius: 3px;
    background-color: {panel_bg};
}}
QCheckBox::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
}}
QListWidget#navList {{
    background-color: {panel_bg};
    border: none;
    border-right: 1px solid {border};
    padding-top: 8px;
}}
QListWidget#navList::item {{
    padding: 10px 14px;
    border-left: 3px solid transparent;
}}
QListWidget#navList::item:selected {{
    background-color: {panel_alt};
    border-left: 3px solid {accent};
    color: {accent};
}}
QListWidget#stageList::item {{ padding: 3px 6px; }}
QProgressBar {{
    background-color: {panel_bg};
    border: 1px solid {border};
    border-radius: 5px;
    text-align: center;
    color: {text_main};
    height: 18px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {accent_hover});
    border-radius: 4px;
}}
QFrame#card {{
    background-color: {panel_bg};
    border: 1px solid {border};
    border-radius: 10px;
}}
QLabel#cardTitle {{
    color: {accent};
    font-size: 14px;
    font-weight: 700;
}}
QLabel#h2 {{
    color: {text_main};
    font-size: 15px;
    font-weight: 600;
}}
QListWidget::item:hover {{
    background-color: {accent_soft};
}}
QListWidget#navList::item:hover {{
    background-color: {panel_alt};
}}
QSlider::handle:horizontal:hover {{
    background-color: {accent_hover};
}}
QTabBar::tab:hover {{
    color: {text_main};
}}
QDockWidget::title {{
    background-color: {panel_bg};
    padding: 6px 10px;
    border-bottom: 1px solid {border};
}}
QSplitter::handle {{ background-color: {border}; }}
QSlider::groove:horizontal {{
    background-color: {panel_bg};
    border: 1px solid {border};
    border-radius: 3px;
    height: 6px;
}}
QSlider::handle:horizontal {{
    background-color: {accent};
    width: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QStatusBar {{
    background-color: {panel_bg};
    color: {text_muted};
    border-top: 1px solid {border};
}}
QTabWidget::pane {{ border: 1px solid {border}; }}
QTabBar::tab {{
    background-color: {panel_bg};
    color: {text_muted};
    padding: 6px 14px;
    border: 1px solid {border};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QTabBar::tab:selected {{ color: {accent}; background-color: {panel_alt}; }}
QToolTip {{
    background-color: {panel_alt};
    color: {text_main};
    border: 1px solid {border};
}}
QScrollArea, QGroupBox {{
    background-color: transparent;
    border: none;
}}
QSpinBox, QDoubleSpinBox {{
    background-color: {panel_bg};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 3px 6px;
}}
QMessageBox QLabel {{
    color: {text_main};
}}
/* Empty states (deep-dive fix #6): icon + text + hint hierarchy */
QLabel#emptyIcon {{
    font-size: 34px;
    padding: 6px 0 0 0;
}}
QLabel#emptyText {{
    color: {text_main};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#emptyHint {{
    color: {text_muted};
}}

/* UI REDESIGN (v3.2.7): compact field labels, section headers, and a
   thinner/more precise slider — replaces the old boxy full-width
   slider look. Applies globally so every panel benefits, not just
   Grade/Voice. */
QLabel#sectionLabel {{
    color: {text_muted};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding-bottom: 4px;
    border-bottom: 1px solid {border};
    margin-bottom: 4px;
}}
QLabel#fieldLabel {{
    color: {text_main};
    font-size: 12px;
}}
QLabel#badge {{
    background-color: {accent_soft};
    color: {accent};
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#badgeSuccess {{
    background-color: rgba(99, 201, 141, 0.18);
    color: {success};
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#badgeMuted {{
    background-color: {panel_alt};
    color: {text_muted};
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QFrame#card {{
    padding: 14px;
}}
QPushButton#ghost {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {text_muted};
}}
QPushButton#ghost:hover {{
    color: {text_main};
    border-color: {border};
}}
QSlider::groove:horizontal {{
    background-color: {border};
    border: none;
    border-radius: 2px;
    height: 4px;
}}
QSlider::handle:horizontal {{
    background-color: {accent};
    width: 15px;
    height: 15px;
    margin: -6px 0;
    border-radius: 8px;
    border: 2px solid {window_bg};
}}
QSlider::handle:horizontal:hover {{
    background-color: {accent_hover};
}}
QSlider::sub-page:horizontal {{
    background-color: {accent};
    border-radius: 2px;
    height: 4px;
}}
QFrame#sceneCard {{
    background-color: {panel_bg};
    border: 1px solid {border};
    border-radius: 10px;
    padding: 12px;
}}
QFrame#sceneCard:hover {{
    border: 1px solid {accent};
}}
QTreeWidget {{
    alternate-background-color: {panel_alt};
    outline: none;
}}
QTreeWidget::item {{
    padding: 6px 4px;
    border-bottom: 1px solid {border};
}}
QTreeWidget::item:selected {{
    background-color: {accent_soft};
    color: {text_main};
}}
QHeaderView::section {{
    background-color: {panel_alt};
    color: {text_muted};
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid {border};
    font-weight: 600;
    font-size: 11px;
}}

/* 3.0.6 round-5 readability: breathing room on every surface */
QFrame#card {{
    padding: 8px;
}}
QListWidget#navList::item {{
    padding: 8px 10px;
}}
QPushButton {{
    padding: 6px 14px;
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    padding: 4px 6px;
    min-height: 22px;
}}
QLabel#sliderValue {{
    font-weight: 700;
    font-size: 13px;
    min-width: 50px;
    padding-left: 6px;
}}
"""

DARK_QSS = _QSS_TEMPLATE.format(
    window_bg=WINDOW_BG,
    panel_bg=PANEL_BG,
    panel_alt=PANEL_ALT,
    text_main=TEXT_MAIN,
    text_muted=TEXT_MUTED,
    accent=ACCENT,
    accent_soft=ACCENT_SOFT,
    accent_contrast="#1c1610",
    accent_hover="#ecb877",
    border=BORDER,
    danger=DANGER,
    success=SUCCESS,
)

LIGHT_QSS = _QSS_TEMPLATE.format(
    window_bg=LIGHT_WINDOW_BG,
    panel_bg=LIGHT_PANEL_BG,
    panel_alt=LIGHT_PANEL_ALT,
    text_main=LIGHT_TEXT_MAIN,
    text_muted=LIGHT_TEXT_MUTED,
    accent=LIGHT_ACCENT,
    accent_soft=LIGHT_ACCENT_SOFT,
    accent_contrast="#fffaf2",
    accent_hover="#cf9241",
    border=LIGHT_BORDER,
    danger=LIGHT_DANGER,
    success=LIGHT_SUCCESS,
)

# AMOLED: pure-black surfaces (OLED battery saver), amber accent kept.
AMOLED_QSS = _QSS_TEMPLATE.format(
    window_bg="#000000",
    panel_bg="#060606",
    panel_alt="#101014",
    text_main="#f2f2f7",
    text_muted="#8e8e93",
    accent=ACCENT,
    accent_soft="#241c10",
    accent_contrast="#000000",
    accent_hover="#f2c078",
    border="#1c1c1e",
    danger="#ff5a5a",
    success="#63c98d",
)

# High-contrast: accessibility preset — black/white + yellow accent.
HIGH_CONTRAST_QSS = _QSS_TEMPLATE.format(
    window_bg="#000000",
    panel_bg="#000000",
    panel_alt="#101010",
    text_main="#ffffff",
    text_muted="#e0e0e0",
    accent="#ffd700",
    accent_soft="#3a3200",
    accent_contrast="#000000",
    accent_hover="#ffe44d",
    border="#ffffff",
    danger="#ff4d4d",
    success="#7CFC00",
)

# name -> (qss, window_bg, text_main, base_bg, highlight)
THEMES = {
    "dark": (
        DARK_QSS, WINDOW_BG, TEXT_MAIN, PANEL_BG, ACCENT_SOFT,
    ),
    "light": (
        LIGHT_QSS, LIGHT_WINDOW_BG, LIGHT_TEXT_MAIN,
        LIGHT_PANEL_BG, LIGHT_ACCENT_SOFT,
    ),
    "amoled": (
        AMOLED_QSS, "#000000", "#f2f2f7", "#060606", "#241c10",
    ),
    "high_contrast": (
        HIGH_CONTRAST_QSS, "#000000", "#ffffff", "#000000", "#3a3200",
    ),
}
THEME_NAMES = tuple(THEMES)
DEFAULT_THEME = "dark"

_NAV_WIDTH = 180  # 3.0.6: wider rail, no clipped labels

# Splash branding (used by the QSplashScreen paint — hex literals only)
SPLASH_BG = WINDOW_BG
SPLASH_ACCENT = ACCENT
SPLASH_TEXT = TEXT_MAIN
SPLASH_MUTED = TEXT_MUTED


def apply_theme(app: object, name: str = DEFAULT_THEME) -> None:
    """Apply a named theme to a QApplication (Qt imported lazily)."""
    from PyQt6.QtGui import QColor, QPalette

    qss, window_bg, text_main, base_bg, highlight = THEMES.get(
        str(name or DEFAULT_THEME), THEMES[DEFAULT_THEME]
    )
    app.setStyleSheet(qss)  # type: ignore[attr-defined]
    palette = app.palette()  # type: ignore[attr-defined]
    palette.setColor(
        QPalette.ColorRole.Window, QColor(window_bg)
    )
    palette.setColor(
        QPalette.ColorRole.WindowText, QColor(text_main)
    )
    palette.setColor(
        QPalette.ColorRole.Base, QColor(base_bg)
    )
    palette.setColor(
        QPalette.ColorRole.Highlight, QColor(highlight)
    )
    app.setPalette(palette)  # type: ignore[attr-defined]
