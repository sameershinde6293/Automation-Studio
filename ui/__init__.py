"""Autopilot UI package (Phase D.4 — PyQt6 shell).

RULE 1 note: ``ui/`` is an entry layer in the same seam class as
``main.py`` — it may import ``core.core_engine`` (the orchestrator)
but never ``modules/*`` directly. All UI logic that does not need Qt
lives in ``ui/viewmodel.py`` (fully testable headless); ``ui/app.py``
is the thin PyQt6 shell and fails ImportError when PyQt6 is missing,
which ``main.py cmd_ui`` turns into a friendly CLI hint.
"""
