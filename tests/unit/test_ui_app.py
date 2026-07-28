"""Import contract for ui.app (the PyQt6 shell).

Sandbox + minimal installs have no PyQt6: importing ui.app must raise
ImportError *mentioning PyQt6* (that is the documented seam cmd_ui
turns into a friendly hint) — never some other coding bug. Machines
with PyQt6 installed must get a callable launch(ctx).
"""

from __future__ import annotations

import importlib


def test_ui_app_import_contract() -> None:
    try:
        module = importlib.import_module("ui.app")
    except ImportError as exc:
        # acceptable ONLY when the environment lacks a working PyQt6
        # (not installed / Qt system libs missing / broken DLL loader)
        text = str(exc)
        environmental = (
            "PyQt6",
            "No module named",
            "cannot open shared object",
            "DLL load failed",
            "libxkbcommon",
        )
        assert any(hint in text for hint in environmental), (
            f"unexpected import error (coding bug suspected): {exc}"
        )
    else:
        assert callable(module.launch)
        assert hasattr(module, "MainWindow")


def test_ui_package_docstring_declares_rule1_seam() -> None:
    import ui

    assert ui.__doc__ is not None
    assert "modules/*" in ui.__doc__
