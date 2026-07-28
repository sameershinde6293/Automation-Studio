"""Qt-free view-model for the PyQt6 shell (D.4).

Every piece of UI logic that does not need Qt lives here so the whole
brain of the window is unit-testable headless (spec File 12: "Automated
testing does not need UI"). ``ui/app.py`` only paints what this class
decides. RULE 1: talks to the CoreEngine/boot ctx seam only — never to
``modules/*`` directly. RULE 2: engine/module responses are consumed as
the standard make_response dicts they are.
"""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.safe_io import atomic_write_bytes, atomic_write_json

PIPELINE_EVENTS: Tuple[str, ...] = (
    "pipeline.started",
    "pipeline.stage_started",
    "pipeline.stage_completed",
    "pipeline.stage_skipped",
    "pipeline.render_progress",
    "pipeline.completed",
    "pipeline.failed",
)

_STAGE_VERB = {
    "license": "Checking license",
    "parse": "Parsing script",
    "channel_profile": "Loading channel profile",
    "voice_profiles": "Preparing voice profiles",
    "keywords": "Analysing keywords",
    "quality_gate": "Running quality gate",
    "images": "Processing images",
    "tts": "Synthesising narration",
    "sfx": "Placing sound effects",
    "audio_mix": "Mixing audio",
    "subtitles": "Writing subtitles",
    "intro_outro": "Building intro/outro",
    "timeline": "Assembling timeline",
    "export": "Rendering scenes",
    "burn_subtitles": "Burning subtitles",
    "verify": "Verifying output",
    "thumbnails": "Generating thumbnails",
    "drive_upload": "Uploading to Google Drive",
}


# ======================================================================
# UI chrome models (menus / toolbar / status bar / splash / theme)
# ======================================================================
# Fallback when config/keyboard_shortcuts.json is missing (RULE 7).
# Keys/values mirror the shipped config exactly.
# Spec File 04 Section 5 pins Start=F9 and Quick Preview=F5.
DEFAULT_SHORTCUTS: Dict[str, str] = {
    "new_project": "Ctrl+N",
    "open_project": "Ctrl+O",
    "save_project": "Ctrl+S",
    "start_render": "F9",
    "quick_preview": "F5",
    "cancel_render": "Escape",
    "pause_render": "Ctrl+P",
    "open_settings": "Ctrl+,",
    "toggle_preview": "Space",
    "import_files": "Ctrl+I",
    "import_zip": "Ctrl+Shift+I",
    "export_video": "Ctrl+E",
    "copy_frame": "Ctrl+Shift+C",
    "backup_project": "Ctrl+B",
    "quit": "Ctrl+Q",
    "undo": "Ctrl+Z",
    "redo": "Ctrl+Y",
    "select_all": "Ctrl+A",
    "refresh_projects": "Ctrl+F5",
    "pre_render_report": "Ctrl+Shift+R",
    "toggle_fullscreen": "F11",
    "copy_scene": "Ctrl+C",
    "paste_scene": "Ctrl+V",
    "delete_scene": "Delete",
    "batch_render": "Ctrl+Shift+B",
    "voice_store": "Ctrl+Shift+V",
    "user_guide": "F1",
}

# Canonical action table — menu_model() and toolbar_model() are both
# derived from this so the toolbar can never drift from the menus.
# "menus" lists which menus an action appears in; separators are
# inserted by MENU_LAYOUT, not by this table.
# NOTE: table order = toolbar order (menus come from MENU_LAYOUT);
# keep the six toolbar entries in their shipped sequence.
# Menu structure implements ui_specification.txt Section 5 VERBATIM
# (File/Edit/View/Project/Render/Tools/Help, all sub-items).
ACTION_DEFS: Tuple[Dict[str, Any], ...] = (
    {"id": "new_project", "text": "&New Project…", "menus": ("file",),
     "toolbar": True},
    {"id": "open_project", "text": "&Open Project…", "menus": ("file",),
     "toolbar": False},
    {"id": "save_project", "text": "&Save Project", "menus": ("file",),
     "toolbar": False},
    {"id": "import_files", "text": "&Import Files…", "menus": ("file",),
     "toolbar": True},
    {"id": "import_zip", "text": "Import Project &ZIP…",
     "menus": ("file",), "toolbar": False},
    {"id": "export_video", "text": "&Export Video", "menus": ("file",),
     "toolbar": False},
    {"id": "export_audio_only", "text": "Export &Audio Only…",
     "menus": ("file",), "toolbar": False, "submenu": "export"},
    {"id": "export_audio_mix", "text": "Export Audio &Mix…",
     "menus": ("file",), "toolbar": False, "submenu": "export"},
    {"id": "burn_subtitles", "text": "&Burn Subtitles to Video…",
     "menus": ("file",), "toolbar": False, "submenu": "export"},
    {"id": "export_thumbnails", "text": "Export &Thumbnails Only…",
     "menus": ("file",), "toolbar": False, "submenu": "export"},
    {"id": "export_storyboard_pdf", "text": "Export Storyboard &PDF…",
     "menus": ("file",), "toolbar": False, "submenu": "export"},
    {"id": "backup_project", "text": "&Backup…", "menus": ("file",),
     "toolbar": False},
    {"id": "quit", "text": "&Quit", "menus": ("file",), "toolbar": False},
    {"id": "start_render", "text": "&Start Render", "menus": ("render",),
     "toolbar": True},
    {"id": "quick_preview", "text": "&Quick Preview", "menus": ("render",),
     "toolbar": False},
    {"id": "cancel_render", "text": "&Cancel Render", "menus": ("render",),
     "toolbar": True},
    {"id": "pause_render", "text": "&Pause Render", "menus": ("render",),
     "toolbar": True, "enabled": False,
     "reason": "pause is not supported by engine v1"},
    {"id": "resume_render", "text": "Res&ume Render…", "menus": ("render",),
     "toolbar": False},
    {"id": "batch_render", "text": "&Batch Queue", "menus": ("render",),
     "toolbar": True},
    {"id": "render_settings", "text": "Render &Settings",
     "menus": ("render",), "toolbar": False},
    {"id": "undo", "text": "&Undo Scene Op", "menus": ("edit",),
     "toolbar": False},
    {"id": "redo", "text": "&Redo Scene Op", "menus": ("edit",),
     "toolbar": False},
    {"id": "select_all", "text": "Select &All Scenes", "menus": ("edit",),
     "toolbar": False},
    {"id": "copy_scene", "text": "&Copy Scene", "menus": ("edit",),
     "toolbar": False},
    {"id": "paste_scene", "text": "&Paste Scene", "menus": ("edit",),
     "toolbar": False},
    {"id": "delete_scene", "text": "&Delete Scene", "menus": ("edit",),
     "toolbar": False},
    {"id": "toggle_preview", "text": "Toggle &Preview",
     "menus": ("view",), "toolbar": True},
    {"id": "refresh_projects", "text": "&Refresh Projects",
     "menus": ("view",), "toolbar": False},
    {"id": "theme_dark", "text": "Theme: &Dark", "menus": ("view",),
     "toolbar": False, "theme": "dark"},
    {"id": "theme_light", "text": "Theme: &Light", "menus": ("view",),
     "toolbar": False, "theme": "light"},
    {"id": "theme_amoled", "text": "Theme: &AMOLED", "menus": ("view",),
     "toolbar": False, "theme": "amoled"},
    {"id": "theme_high_contrast", "text": "Theme: &High Contrast",
     "menus": ("view",), "toolbar": False, "theme": "high_contrast"},
    {"id": "toggle_toolbar", "text": "Show &Toolbar", "menus": ("view",),
     "toolbar": False},
    {"id": "toggle_statusbar", "text": "Show Status &Bar",
     "menus": ("view",), "toolbar": False},
    {"id": "toggle_progress_panel", "text": "Show Render &Progress",
     "menus": ("view",), "toolbar": False},
    {"id": "toggle_fullscreen", "text": "Toggle &Fullscreen",
     "menus": ("view",), "toolbar": False},
    {"id": "toggle_inspector", "text": "Show &Inspector",
     "menus": ("view",), "toolbar": False},
    {"id": "open_settings", "text": "Project &Settings",
     "menus": ("project",), "toolbar": True},
    {"id": "channel_profiles", "text": "&Channel Profile Manager…",
     "menus": ("project",), "toolbar": False},
    {"id": "quality_check", "text": "&Quality Check…", "menus": ("project",),
     "toolbar": False},
    {"id": "pre_render_report", "text": "Pre-&Render Report…",
     "menus": ("project",), "toolbar": False},
    {"id": "voice_store", "text": "&Voice Store", "menus": ("tools",),
     "toolbar": False},
    {"id": "voice_clone", "text": "Voice &Clone…", "menus": ("tools",),
     "toolbar": False},
    {"id": "engine_manager", "text": "&Engine Manager…", "menus": ("tools",),
     "toolbar": False},
    {"id": "setup_wizard", "text": "Setup &Wizard…", "menus": ("tools",),
     "toolbar": False},
    {"id": "key_generator", "text": "My &License / Machine ID…",
     "menus": ("tools",), "toolbar": False},
    {"id": "modules", "text": "&Modules…", "menus": ("tools",),
     "toolbar": False},
    {"id": "plugins_status", "text": "&Plugins…", "menus": ("tools",),
     "toolbar": False},
    {"id": "open_logs", "text": "Open &Logs Folder", "menus": ("tools",),
     "toolbar": False},
    {"id": "user_guide", "text": "&User Guide", "menus": ("help",),
     "toolbar": True},
    {"id": "shortcuts", "text": "&Keyboard Shortcuts…", "menus": ("help",),
     "toolbar": False},
    {"id": "license_status", "text": "&License Status…", "menus": ("help",),
     "toolbar": False},
    {"id": "about", "text": "&About Autopilot", "menus": ("help",),
     "toolbar": False},
)

# Full menu bar — ui_specification.txt Section 5, in spec order.
# An action id may appear in several menus — the shell shares ONE
# QAction per id, so enablement/shortcuts can never drift apart.
MENU_LAYOUT: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("file", "&File",
     ("new_project", "open_project", "save_project", "|",
      "import_files", "import_zip", "|", "export_video",
      "export_audio_only", "export_audio_mix", "burn_subtitles",
      "export_thumbnails", "export_storyboard_pdf", "backup_project",
      "|", "quit")),
    ("edit", "&Edit",
     ("undo", "redo", "|", "select_all", "|", "copy_scene", "paste_scene",
      "delete_scene")),
    ("view", "&View",
     ("toggle_preview", "refresh_projects", "|", "theme_dark",
      "theme_light", "theme_amoled", "theme_high_contrast", "|",
      "toggle_toolbar", "toggle_statusbar", "toggle_progress_panel",
      "|", "toggle_fullscreen", "toggle_inspector")),
    ("project", "&Project",
     ("open_settings", "channel_profiles", "quality_check", "|",
      "pre_render_report")),
    ("render", "&Render",
     ("start_render", "quick_preview", "|", "cancel_render",
      "pause_render", "resume_render", "|", "batch_render",
      "render_settings")),
    ("tools", "&Tools",
     ("voice_store", "voice_clone", "engine_manager", "setup_wizard",
      "key_generator",
      "|", "modules", "plugins_status", "|", "open_logs")),
    ("help", "&Help",
     ("user_guide", "shortcuts", "|", "license_status", "|", "about")),
)

SPLASH_STEPS: Tuple[str, ...] = (
    "Loading configuration",
    "Opening database",
    "Registering core services",
    "Loading engine modules",
    "Checking license",
    "Preparing window",
)

# Emoji glyphs shown next to menu/toolbar texts (ui_specification:
# "icons everywhere"). Unicode ships with every OS — zero binary
# assets, works fully offline.
ACTION_ICONS: Dict[str, str] = {
    "new_project": "🆕", "open_project": "📂", "save_project": "💾",
    "import_files": "📥", "import_zip": "🗜", "export_video": "🎬",
    "export_audio_only": "🎧", "export_audio_mix": "🎚",
    "burn_subtitles": "💬", "export_thumbnails": "🏞",
    "export_storyboard_pdf": "📑",
    "backup_project": "🗄", "quit": "⏻",
    "undo": "↩", "redo": "↪", "select_all": "⬚",
    "copy_scene": "📄", "paste_scene": "📋", "delete_scene": "🗑",
    "toggle_preview": "👁", "refresh_projects": "🔄",
    "theme_dark": "🌙", "theme_light": "☀", "theme_amoled": "⬛",
    "theme_high_contrast": "◐", "toggle_toolbar": "🧰",
    "toggle_statusbar": "📏", "toggle_progress_panel": "📊",
    "toggle_fullscreen": "⛶", "toggle_inspector": "🎛",
    "open_settings": "⚙", "channel_profiles": "🎚",
    "quality_check": "✅", "pre_render_report": "📝",
    "start_render": "▶", "quick_preview": "⚡", "cancel_render": "✖",
    "pause_render": "⏸", "resume_render": "⏵", "batch_render": "▦",
    "render_settings": "🛠", "voice_store": "🎙", "voice_clone": "🧬",
    "engine_manager": "🧩", "setup_wizard": "🧭", "key_generator": "🔑",
    "modules": "📦",
    "plugins_status": "🔌", "open_logs": "🧾", "user_guide": "📖",
    "shortcuts": "⌨", "license_status": "📜", "about": "ℹ",
}


class UiChromeMixin:
    """View-model chrome providers (mixed into UiViewModel below)."""

    # -- shortcuts -----------------------------------------------------
    def shortcuts_map(self) -> Dict[str, str]:
        """{action_id: key sequence} — config file wins, defaults fill."""
        merged = dict(DEFAULT_SHORTCUTS)
        data: Any = None
        if self.container is not None:
            try:
                data = self.container.get("config").get_config(
                    "keyboard_shortcuts"
                )
            except Exception:  # noqa: BLE001 - UI falls back to defaults
                data = None
        if isinstance(data, dict):
            raw = data.get("shortcuts", data)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    merged[str(key)] = str(value)
        return merged

    def shortcut_for(self, action_id: str) -> str:
        return self.shortcuts_map().get(str(action_id), "")

    # -- menus / toolbar -------------------------------------------------
    def menu_model(self) -> List[Dict[str, Any]]:
        """[{menu, title, items:[action|{separator}]}] for the shell."""
        defs = {a["id"]: a for a in ACTION_DEFS}
        keys = self.shortcuts_map()
        model: List[Dict[str, Any]] = []
        for menu_id, title, order in MENU_LAYOUT:
            items: List[Dict[str, Any]] = []
            submenu: Optional[Dict[str, Any]] = None
            for action_id in order:
                if action_id == "|":
                    submenu = None
                    items.append({"separator": True})
                    continue
                definition = defs[action_id]
                item = {
                    "id": action_id,
                    "text": definition["text"],
                    "icon": ACTION_ICONS.get(action_id, ""),
                    "shortcut": keys.get(action_id, ""),
                    "enabled": bool(definition.get("enabled", True)),
                    "reason": str(definition.get("reason") or ""),
                    "separator": False,
                }
                if definition.get("submenu"):
                    # File ▸ Export: partial-workflow exports live in
                    # ONE submenu so the File menu stays scannable.
                    if submenu is None:
                        submenu = {"submenu": "&Export", "items": []}
                        items.append(submenu)
                    submenu["items"].append(item)
                    continue
                submenu = None
                items.append(item)
            model.append({"menu": menu_id, "title": title, "items": items})
        return model

    def toolbar_model(self) -> List[Dict[str, Any]]:
        """Toolbar subset of ACTION_DEFS (same texts/shortcuts/enable)."""
        keys = self.shortcuts_map()
        return [
            {
                "id": a["id"],
                "text": a["text"].replace("&", ""),
                "icon": ACTION_ICONS.get(a["id"], ""),
                "shortcut": keys.get(a["id"], ""),
                "enabled": bool(a.get("enabled", True)),
                "reason": str(a.get("reason") or ""),
            }
            for a in ACTION_DEFS
            if a.get("toolbar")
        ]

    # -- status bar --------------------------------------------------------
    def status_bar_model(self) -> Dict[str, str]:
        """Permanent status-bar fields (right side); never raises."""
        license_summary = self.license_summary()
        status = {"license": f"License: {license_summary['status']}"}
        days = license_summary.get("days_remaining")
        if days is not None:
            status["license"] += f" ({days}d)"
        status["modules"] = f"Modules: {self.module_count()}"
        try:
            status["plugins"] = (
                f"Plugins: {self.engines_status().get('plugins_loaded', 0)}"
            )
        except Exception:  # noqa: BLE001
            status["plugins"] = "Plugins: 0"
        return status

    # -- status bar: machine + project + FFmpeg (spec deep-dive #5) ----
    def system_status_model(self) -> Dict[str, str]:
        """{ram, cpu, ffmpeg} strings — stdlib first, psutil when
        installed, honest em-dash where a platform reports nothing.
        Never raises (status bar refresh ticks every 5 s)."""
        ram = cpu = None
        try:  # psutil is an OPTIONAL dependency; use it when present
            import psutil
            ram = f"RAM {psutil.virtual_memory().percent:.0f}%"
            cpu = f"CPU {psutil.cpu_percent(interval=None):.0f}%"
        except Exception:  # noqa: BLE001 - fall through to stdlib
            ram = cpu = None
        if ram is None and Path("/proc/meminfo").is_file():
            try:
                info: Dict[str, int] = {}
                for line in Path("/proc/meminfo").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    key, _, rest = line.partition(":")
                    try:
                        info[key] = int(rest.strip().split()[0])
                    except (IndexError, ValueError):
                        continue
                total = info.get("MemTotal", 0)
                avail = info.get("MemAvailable", 0)
                if total > 0:
                    ram = f"RAM {100 * (total - avail) // total}%"
            except OSError:  # noqa: BLE001
                ram = None
        if cpu is None:
            try:
                import os
                load = os.getloadavg()[0]  # POSIX only
                count = os.cpu_count() or 1
                cpu = f"CPU {min(100, int(100 * load / count))}%"
            except (AttributeError, OSError):
                cpu = None
        try:
            engines = self.engines_status()
            ff = "ok" if engines.get("ffmpeg") else "missing"
        except Exception:  # noqa: BLE001
            ff = "unknown"
        return {
            "ram": ram or "RAM —",
            "cpu": cpu or "CPU —",
            "ffmpeg": f"FFmpeg: {ff}",
        }

    # -- Inspector default: app stats (spec deep-dive #3) ---------------
    def inspector_stats_model(self) -> List[str]:
        """Right-panel 'app stats' lines when no scene is selected."""
        try:
            engines = self.engines_status()
        except Exception:  # noqa: BLE001
            engines = {}
        lic = self.license_summary()
        try:
            projects = len(self.refresh_projects(limit=100))
        except Exception:  # noqa: BLE001
            projects = 0
        modules = 0
        try:
            modules = self.module_count()
        except Exception:  # noqa: BLE001
            modules = 0
        return [
            "Autopilot 3.1.0 — Offline Documentary Studio",
            f"License: {lic.get('message') or lic.get('status', '?')}",
            f"Projects on disk: {projects}",
            f"Engine modules: {modules}",
            f"Plugins loaded: {engines.get('plugins_loaded', 0)}",
            "FFmpeg: "
            + ("✓ found" if engines.get("ffmpeg") else "✗ not found"),
            "Piper TTS: "
            + ("✓ found" if engines.get("piper") else "✗ not found"),
            "",
            "▶ Select a scene card on the Studio timeline to "
            "inspect its media, timing and chapter info here.",
        ]

    # -- splash --------------------------------------------------------------
    def splash_model(self) -> Dict[str, Any]:
        return {
            "title": "AUTOPILOT",
            "subtitle": "Offline Documentary Studio",
            "logo": "▶",
            "version": "3.1.0",
            "steps": [str(step) for step in SPLASH_STEPS],
        }

    # -- license screen (ui_specification §3: HWID display) -----------------
    def license_screen_model(self) -> Dict[str, Any]:
        """HWID + status for the boot-time License Screen dialog."""
        summary = self.license_summary()
        hwid = ""
        if self.license_manager is not None:
            try:
                hwid = str(self.license_manager.generate_hwid())
            except Exception:  # noqa: BLE001 - advisory display only
                hwid = ""
        return {
            "hwid": hwid,
            "status": str(summary.get("status") or "unknown"),
            "message": str(summary.get("message") or ""),
            "days_remaining": summary.get("days_remaining"),
        }

    def license_screen_needed(self) -> bool:
        """True when no active/trial license is on file at boot."""
        return str(self.license_summary().get("status")) not in (
            "active", "trial")

    # -- window memory (ui_specification: geometry persistence) ------------
    def window_state_save(self, geometry: str, state: str) -> None:
        """Persist base64 window geometry/state (best effort)."""
        if self.container is None:
            return
        try:
            config = self.container.get("config")
            config.set("window_geometry", str(geometry))
            config.set("window_state", str(state))
        except Exception:  # noqa: BLE001 - layout restore is optional
            pass

    def window_state_load(self) -> Dict[str, str]:
        """{geometry, state} base64 strings; empty when none saved."""
        out = {"geometry": "", "state": ""}
        if self.container is None:
            return out
        try:
            config = self.container.get("config")
            out["geometry"] = str(
                config.get("window_geometry", "") or "")
            out["state"] = str(config.get("window_state", "") or "")
        except Exception:  # noqa: BLE001
            pass
        return out

    # -- theme -----------------------------------------------------------------
    def theme_names(self) -> Tuple[str, ...]:
        from ui.theme import THEME_NAMES

        return THEME_NAMES

    def current_theme(self) -> str:
        from ui.theme import DEFAULT_THEME

        if self.container is None:
            return DEFAULT_THEME
        try:
            value = self.container.get("config").get(
                "theme", DEFAULT_THEME
            )
        except Exception:  # noqa: BLE001
            return DEFAULT_THEME
        return str(value) if value in self.theme_names() else DEFAULT_THEME

    def set_theme(self, name: str) -> Tuple[bool, str]:
        name = str(name or "")
        if name not in self.theme_names():
            return False, f"Unknown theme: {name}"
        if self.container is not None:
            try:
                self.container.get("config").set("theme", name)
            except Exception as exc:  # noqa: BLE001 - persist is best-effort
                return True, f"Theme switched to {name} (not saved: {exc})"
        return True, f"Theme switched to {name}."

    # -- static text blocks ----------------------------------------------------
    def about_text(self) -> str:
        engines = self.engines_status()
        return (
            "Autopilot 3.1.0 — offline documentary video automation.\n"
            "Scripts, footage and credentials never leave this machine.\n\n"
            f"FFmpeg: {engines.get('ffmpeg') or 'not found'}\n"
            f"Piper TTS: {engines.get('piper') or 'not found'}\n"
            f"Engine modules loaded: {engines.get('modules_loaded', 0)}\n"
            f"Plugins loaded: {engines.get('plugins_loaded', 0)}"
        )

    def shortcuts_text(self) -> str:
        keys = self.shortcuts_map()
        lines = []
        for definition in ACTION_DEFS:
            key = keys.get(definition["id"])
            if not key:
                continue
            label = definition["text"].replace("&", "")
            note = ""
            if not definition.get("enabled", True):
                note = f"  ({definition.get('reason', 'unavailable')})"
            lines.append(f"{key:<14} {label}{note}")
        return "\n".join(lines)


# ======================================================================
# Studio models (import / preview / timeline) — full-UI Batch 2
# ======================================================================

# Supported import formats (spec File 04 — the panel's drop zones must
# SHOW these to the user, JSON included):
#   scripts: TXT, JSON, CSV, DOCX, PDF
#   images:  JPG (incl. JPEG), PNG
#   audio:   MP3, WAV
# Anything else is classified "other" and flagged "unsupported".
IMPORT_KINDS: Dict[str, Tuple[str, ...]] = {
    # FEATURE (v3.2.11): .zip added — lets a script be dropped in as a
    # zip bundle (e.g. exported from an AI script-writing tool) instead
    # of a loose file. apply_import() extracts the actual script from
    # inside it automatically; see there for the extraction logic.
    "script": (".txt", ".json", ".csv", ".docx", ".pdf", ".zip"),
    "image": (".jpg", ".jpeg", ".png"),
    "audio": (".mp3", ".wav"),
    "video": (".mp4", ".mov", ".mkv", ".webm"),
}

# One drop zone per import kind; texts are what the user actually sees.
IMPORT_ZONES: Tuple[Dict[str, Any], ...] = (
    {
        "kind": "script",
        "title": "📝  Script",
        "formats": "TXT · JSON · CSV · DOCX · PDF · ZIP",
        "hint": "One narration script per project (ZIP: extracts the script inside automatically)",
        "staged_folder": "scripts",
    },
    {
        "kind": "image",
        "title": "🖼  Images",
        "formats": "JPG · JPEG · PNG",
        "hint": "Scene images (matched to scenes in order)",
        "staged_folder": "images",
    },
    {
        "kind": "audio",
        "title": "🎵  Music",
        "formats": "MP3 · WAV",
        "hint": "Background music bed / sound effects",
        "staged_folder": "audios",
    },
    {
        "kind": "audio",
        "title": "🎙  Voice-over",
        "formats": "MP3 · WAV",
        "hint": "Pre-recorded narration (staged with audio)",
        "staged_folder": "audios",
    },
    {
        "kind": "video",
        "title": "🎞  Video clips",
        "formats": "MP4 · MOV · MKV · WEBM",
        "hint": "Reference / cutaway clips (staged only)",
        "staged_folder": "videos",
    },
)

PLAYBACK_RATES: Tuple[float, ...] = (0.5, 1.0, 1.25, 1.5, 2.0)

_TEXT_KEYS = ("text_content", "text", "line_text", "dialogue_text",
               "content", "line")


def classify_import(path_str: str) -> str:
    """Import kind from the file extension; 'other' when unmapped."""
    suffix = Path(str(path_str)).suffix.lower()
    for kind, suffixes in IMPORT_KINDS.items():
        if suffix in suffixes:
            return kind
    return "other"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _wav_peaks(path: str, buckets: int = 160) -> Optional[List[float]]:
    """Normalized 0..1 min/max envelope; None when not a PCM .wav."""
    import struct

    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frames = handle.getnframes()
            if width != 2 or frames <= 0:
                return None
            raw = handle.readframes(frames)
    except (wave.Error, EOFError, OSError):
        return None
    try:
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    except struct.error:
        return None
    if channels > 1:  # down-mix stereo to mono
        samples = samples[::channels]
    count = len(samples)
    if count == 0:
        return None
    buckets = max(8, min(int(buckets), count))
    size = count / buckets
    peaks: List[float] = []
    for index in range(buckets):
        seg = samples[int(index * size):int((index + 1) * size)] or (0,)
        peaks.append(round(max(abs(min(seg)), abs(max(seg))) / 32768.0, 3))
    return peaks


def fmt_timecode(seconds: Any) -> str:
    """Media time label: m:ss under an hour, else h:mm:ss."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def position_percent(position_s: Any, duration_s: Any) -> float:
    """Slider percentage, clamped 0-100; 0 when duration is unknown."""
    try:
        pos, dur = float(position_s), float(duration_s)
    except (TypeError, ValueError):
        return 0.0
    if dur <= 0:
        return 0.0
    return max(0.0, min(100.0, pos / dur * 100.0))


class UiStudioMixin:
    """Import/preview/timeline providers (mixed into UiViewModel)."""

    # -- import ----------------------------------------------------------
    @staticmethod
    def import_zones() -> List[Dict[str, Any]]:
        """Drop-zone cards for the import panel (one per kind)."""
        return [dict(zone) for zone in IMPORT_ZONES]

    def import_plan(self, paths: List[str]) -> List[Dict[str, Any]]:
        """Classify + validate dropped files (RULE 7: rows never fail)."""
        plan: List[Dict[str, Any]] = []
        seen = set()
        for raw in paths or []:
            path = Path(str(raw)).expanduser()
            key = str(path)
            kind = classify_import(key)
            row: Dict[str, Any] = {
                "path": key,
                "name": path.name or key,
                "kind": kind,
                "size_bytes": 0,
                "exists": path.is_file(),
                "status": "ready",
            }
            if key in seen:
                row["status"] = "duplicate"
            elif not row["exists"]:
                row["status"] = "missing"
            elif kind == "other":
                row["status"] = "unsupported"
            else:
                try:
                    row["size_bytes"] = path.stat().st_size
                except OSError:
                    row["size_bytes"] = 0
            seen.add(key)
            plan.append(row)
        return plan

    def import_summary(self, plan: List[Dict[str, Any]]) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        ready = 0
        total_bytes = 0
        for row in plan or []:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
            if row["status"] == "ready":
                ready += 1
                total_bytes += int(row.get("size_bytes") or 0)
        parts = [f"{n} {k}" for k, n in sorted(counts.items())]
        text = (
            f"📥  {ready} ready to stage ({', '.join(parts)})"
            if ready
            else "📥  Drop files into a zone above, or use Add Files…"
        )
        return {
            "counts": counts, "ready": ready,
            "total_bytes": total_bytes, "text": text,
        }

    def apply_import(
        self, plan: List[Dict[str, Any]], project_folder: str
    ) -> Dict[str, Any]:
        """Stage ready files into <project>/imports/<kind>/ copies.

        Returns script_path/images_folder suggestions for the render
        form plus an honest errors list; never raises (RULE 7/8).
        """
        import shutil

        ready = [r for r in plan or [] if r.get("status") == "ready"]
        if not ready:
            return {"success": False, "error": "Nothing ready to stage.",
                    "copied": 0, "errors": []}
        try:
            root = Path(str(project_folder)).expanduser()
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"success": False,
                    "error": f"Cannot create project folder: {exc}",
                    "copied": 0, "errors": []}
        staged_dir = root / "imports"
        copied = 0
        errors: List[str] = []
        taken = set()
        first_script: Optional[str] = None
        images_staged = 0
        for row in ready:
            kind_dir = staged_dir / (
                "images" if row["kind"] == "image" else f"{row['kind']}s"
            )
            try:
                kind_dir.mkdir(parents=True, exist_ok=True)
                name = row["name"]
                source_path = row["path"]
                # FEATURE (v3.2.11): a script dropped as a .zip bundle —
                # extract the actual script file from inside it instead
                # of staging the zip itself (which the render pipeline
                # can't parse as a script). Picks the first recognized
                # script-format file found inside, preferring .txt (the
                # documented "TTS-ready script" format) if more than one
                # candidate exists.
                if row["kind"] == "script" and name.lower().endswith(".zip"):
                    extracted = self._extract_script_from_zip(
                        source_path, kind_dir
                    )
                    if extracted is None:
                        errors.append(
                            f"{name}: no script file (.txt/.docx/.pdf/"
                            ".json/.csv) found inside this zip"
                        )
                        continue
                    source_path, name = extracted, Path(extracted).name
                candidate = kind_dir / name
                index = 2
                while str(candidate).lower() in taken or candidate.exists():
                    candidate = kind_dir / (
                        f"{candidate.stem} ({index})"
                        f"{candidate.suffix}"
                    )
                    index += 1
                shutil.copy2(source_path, candidate)
                taken.add(str(candidate).lower())
                copied += 1
                if row["kind"] == "script" and first_script is None:
                    first_script = str(candidate)
                if row["kind"] == "image":
                    images_staged += 1
            except OSError as exc:
                errors.append(f"{row['name']}: {exc}")
        return {
            "success": not errors,
            "staged_dir": str(staged_dir),
            "script_path": first_script,
            "images_folder": str(staged_dir / "images")
            if images_staged
            else None,
            "copied": copied,
            "errors": errors,
        }

    def _extract_script_from_zip(
        self, zip_path: str, dest_dir: Path
    ) -> Optional[str]:
        """Pull the actual script file out of a zipped script bundle.

        FEATURE (v3.2.11): supports dropping a script in as a .zip
        (e.g. exported from an AI script-writing tool) instead of a
        loose file. Picks the first recognized script-format entry —
        .txt preferred (the documented "TTS-ready script" format with
        pause/spell tags), falling back to .docx/.pdf/.json/.csv if no
        .txt is present. Ignores directories, hidden/system entries,
        and non-script files that might also be bundled in the zip.
        Returns the extracted file's path, or None if nothing usable
        was found inside (caller reports this as an error, never
        silently guesses).
        """
        script_exts = (".txt", ".docx", ".pdf", ".json", ".csv")
        try:
            with zipfile.ZipFile(str(zip_path)) as bundle:
                names = [
                    n for n in bundle.namelist()
                    if not n.endswith("/")
                    and not Path(n).name.startswith((".", "__"))
                    and Path(n).suffix.lower() in script_exts
                ]
                if not names:
                    return None
                # Prefer .txt (the documented tag format) if present.
                names.sort(key=lambda n: (Path(n).suffix.lower() != ".txt", n))
                chosen = names[0]
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / Path(chosen).name
                with bundle.open(chosen) as src, open(target, "wb") as out:
                    out.write(src.read())
                return str(target)
        except (OSError, zipfile.BadZipFile):
            return None

    # -- preview --------------------------------------------------------
    def preview_source(
        self, project_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Latest rendered file for the player (explicit id wins)."""
        rows = self.refresh_projects(limit=50)
        chosen: Optional[Dict[str, Any]] = None
        if project_id:
            chosen = next(
                (r for r in rows if str(r.get("id")) == str(project_id)),
                None,
            )
        if chosen is None:
            chosen = next(
                (r for r in rows if r.get("last_render_output_path")), None
            )
        path = (chosen or {}).get("last_render_output_path")
        title = (chosen or {}).get("title") or ""
        exists = bool(path) and Path(str(path)).is_file()
        return {
            "path": str(path) if path else None,
            "exists": exists,
            "title": str(title),
            "project_id": (chosen or {}).get("id"),
        }

    @staticmethod
    def transport_state(
        position_s: Any, duration_s: Any, playing: bool
    ) -> Dict[str, Any]:
        """Slider/label state for the transport bar (pure logic)."""
        try:
            dur = float(duration_s)
        except (TypeError, ValueError):
            dur = 0.0
        return {
            "position_text": (
                f"{fmt_timecode(position_s)} / {fmt_timecode(dur)}"
            ),
            "percent": position_percent(position_s, dur),
            "has_media": dur > 0,
            "play_label": "❚❚" if playing else "►",
        }

    # -- timeline ----------------------------------------------------------
    def timeline_projects(self, limit: int = 10) -> List[Dict[str, str]]:
        rows = self.refresh_projects(limit=limit)
        return [
            {"id": str(r.get("id")), "label": str(r.get("title") or r["id"])}
            for r in rows
        ]

    def timeline_model(self, project_id: str) -> Dict[str, Any]:
        """Scene/dialogue structure for the timeline panel (advisory)."""
        empty = {"found": False, "title": "", "scenes": [], "scene_count": 0,
                 "total_duration": 0.0, "word_total": 0, "estimated": True}
        if self.container is None or not project_id:
            return empty
        try:
            db = self.container.get("database").db
            project = db.fetch_one(
                "SELECT id, title, status FROM projects WHERE id = ?",
                (str(project_id),),
            )
            if project is None:
                return empty
            scene_rows = db.fetch_all(
                "SELECT * FROM scenes WHERE project_id = ?"
                " ORDER BY scene_number",
                (str(project_id),),
            )
            line_rows = db.fetch_all(
                "SELECT * FROM dialogue_lines WHERE project_id = ?"
                " ORDER BY line_number",
                (str(project_id),),
            )
        except Exception:  # noqa: BLE001 - panel degrades to empty state
            return empty
        lines_by_scene: Dict[str, List[Dict[str, Any]]] = {}
        word_total = 0
        for row in line_rows:
            text = ""
            for key in _TEXT_KEYS:
                if isinstance(row.get(key), str) and row.get(key):
                    text = str(row[key])
                    break
            words = len(text.split()) if text else 0
            word_total += words
            lines_by_scene.setdefault(str(row.get("scene_id")), []).append(
                {
                    "number": int(row.get("line_number") or 0),
                    "character": str(row.get("character_name") or "?"),
                    "emotion": str(row.get("emotion") or ""),
                    "text": text,
                    "words": words,
                }
            )
        scenes: List[Dict[str, Any]] = []
        total = 0.0
        for row in scene_rows:
            scene_lines = lines_by_scene.get(str(row.get("id")), [])
            duration = float(row.get("duration") or 0.0)
            start = float(row.get("start_time") or 0.0)
            total += duration
            thumb = (
                row.get("proxy_image_path") or row.get("image_file_path")
                or None
            )
            scenes.append(
                {
                    "number": int(row.get("scene_number") or 0),
                    "title": str(row.get("scene_title") or ""),
                    "duration": duration,
                    "start": start,
                    "start_text": fmt_timecode(start),
                    "thumb_path": str(thumb) if thumb else None,
                    "image_matched": bool(row.get("image_matched")),
                    "image": str(row.get("image_filename") or ""),
                    "status": str(row.get("status") or "pending"),
                    "transition_in": str(row.get("transition_in") or ""),
                    "transition_out": str(row.get("transition_out") or ""),
                    "animation": str(row.get("animation_type") or ""),
                    "chapter": str(row.get("chapter_title") or "") or None,
                    "words": sum(l["words"] for l in scene_lines),
                    "lines": scene_lines,
                }
            )
        return {
            "found": True,
            "title": str(project.get("title") or ""),
            "status": str(project.get("status") or ""),
            "scenes": scenes,
            "scene_count": len(scenes),
            "total_duration": total,
            "word_total": word_total,
            "estimated": all(s["duration"] <= 0 for s in scenes),
        }

    @staticmethod
    def timeline_summary_text(model: Dict[str, Any]) -> str:
        if not model.get("found"):
            return "No project selected."
        count = model.get("scene_count", 0)
        words = model.get("word_total", 0)
        if model.get("estimated"):
            return f"{count} scenes · {words} words · durations after render"
        return (
            f"{count} scenes · {words} words · "
            f"≈{fmt_timecode(model.get('total_duration', 0))}"
        )

    # -- storyboard / scene details (Preview tabs 2+3, spec §8) ---------
    def storyboard_model(self, project_id: str) -> Dict[str, Any]:
        """Thumbnail-grid rows for the Storyboard tab."""
        model = self.timeline_model(project_id)
        cards = [
            {
                "number": s["number"], "title": s["title"],
                "thumb_path": s["thumb_path"], "duration": s["duration"],
                "status": s["status"], "image_matched": s["image_matched"],
            }
            for s in model.get("scenes", [])
        ]
        return {
            "found": model.get("found", False),
            "title": model.get("title", ""),
            "cards": cards,
            "count": len(cards),
        }

    def scene_details_model(
        self, project_id: str, scene_number: Any
    ) -> Dict[str, Any]:
        """Full info block for ONE scene (Scene Details tab)."""
        model = self.timeline_model(project_id)
        empty = {"found": False, "scene": None, "lines": []}
        if not model.get("found"):
            return empty
        try:
            wanted = int(scene_number)
        except (TypeError, ValueError):
            wanted = 1
        scene = next(
            (s for s in model["scenes"] if s["number"] == wanted), None
        )
        if scene is None:
            return empty
        thumb_exists = bool(
            scene.get("thumb_path")
            and Path(str(scene["thumb_path"])).is_file()
        )
        info = dict(scene)
        info["thumb_exists"] = thumb_exists
        transitions = (
            f"{scene['transition_in'] or '—'} → "
            f"{scene['transition_out'] or '—'}"
        )
        return {
            "found": True,
            "project_title": model.get("title", ""),
            "scene": info,
            "lines": scene.get("lines", []),
            "rows": [
                ("Scene", f"#{scene['number']:02d}  {scene['title']}"),
                ("Status", scene["status"]),
                ("Timing",
                 f"starts {scene['start_text']} · runs "
                 f"{fmt_timecode(scene['duration'])}"),
                ("Animation", scene["animation"] or "none"),
                ("Transitions", transitions),
                ("Image",
                 (scene["image"] or "(none)")
                 + ("" if scene["image_matched"] else " — not matched")),
                ("Words", str(scene["words"])),
            ],
        }

    @staticmethod
    def scene_at_position(
        scenes: List[Dict[str, Any]], seconds: Any
    ) -> Optional[Dict[str, Any]]:
        """Which scene is on screen at <seconds> (preview info bar)."""
        try:
            pos = float(seconds)
        except (TypeError, ValueError):
            return None
        for scene in scenes or []:
            start = float(scene.get("start") or 0.0)
            end = start + float(scene.get("duration") or 0.0)
            if start <= pos < end or (pos >= end and scene is scenes[-1]):
                return scene
        return scenes[0] if scenes else None

    # -- chapter markers + waveform (visual timeline, spec §9) ----------
    def chapter_markers(self, project_id: str) -> Dict[str, Any]:
        """[{seconds, percent, title}] — vertical lines over the strip."""
        model = self.timeline_model(project_id)
        markers: List[Dict[str, Any]] = []
        total = float(model.get("total_duration") or 0.0)
        scenes = model.get("scenes", [])
        flagged = [s for s in scenes if s.get("chapter")]
        source = flagged or scenes  # no explicit chapters -> every start
        for scene in source:
            seconds = float(scene.get("start") or 0.0)
            percent = (seconds / total * 100.0) if total > 0 else 0.0
            markers.append(
                {
                    "seconds": seconds,
                    "percent": max(0.0, min(100.0, percent)),
                    "title": scene.get("chapter")
                    or scene.get("title")
                    or f"Scene {scene['number']}",
                }
            )
        return {
            "found": bool(model.get("found")),
            "total_seconds": total,
            "markers": markers,
        }

    def waveform_model(self, project_id: str) -> Dict[str, Any]:
        """Narration audio for the timeline waveform strip.

        Prefers a narration/voice track from audio_tracks; peaks are
        decoded with stdlib ``wave`` (PCM .wav only — mp3 needs ffmpeg
        and is reported honestly instead of guessed).
        """
        result: Dict[str, Any] = {
            "found": False, "path": None, "peaks": [],
            "duration": 0.0, "note": "narration mix appears after render",
        }
        if self.container is None or not project_id:
            return result
        try:
            db = self.container.get("database").db
            row = db.fetch_one(
                "SELECT file_path, duration_seconds, format"
                " FROM audio_tracks WHERE project_id = ?"
                " ORDER BY CASE track_type"
                " WHEN 'narration' THEN 0 WHEN 'voiceover' THEN 1"
                " WHEN 'tts' THEN 2 WHEN 'mixed' THEN 3 ELSE 4 END"
                " LIMIT 1",
                (str(project_id),),
            )
        except Exception:  # noqa: BLE001
            row = None
        if not row:
            return result
        path = str(row.get("file_path") or "")
        result["duration"] = float(row.get("duration_seconds") or 0.0)
        if not path or not Path(path).is_file():
            result["note"] = "narration file is missing on disk"
            return result
        result["path"] = path
        peaks = _wav_peaks(path)
        if peaks is None:
            result["note"] = (
                f"{Path(path).suffix or 'audio'}: live peaks need a "
                "PCM .wav mix (mp3 decodes via ffmpeg at render time)"
            )
            return result
        result["found"] = True
        result["peaks"] = peaks
        result["note"] = ""
        return result

    # -- scene structure edits + undo/redo (spec §5 Edit menu) ----------
    def _structure_snapshot(self, project_id: str) -> Dict[str, Any]:
        db = self.container.get("database").db
        scenes = [
            dict(r)
            for r in db.fetch_all(
                "SELECT * FROM scenes WHERE project_id = ?"
                " ORDER BY scene_number",
                (str(project_id),),
            )
        ]
        lines = [
            dict(r)
            for r in db.fetch_all(
                "SELECT * FROM dialogue_lines WHERE project_id = ?"
                " ORDER BY line_number",
                (str(project_id),),
            )
        ]
        return {
            "order": [str(s["id"]) for s in scenes],
            "scenes": scenes,
            "lines": lines,
        }

    def _restore_structure(
        self, project_id: str, snapshot: Dict[str, Any]
    ) -> None:
        db = self.container.get("database").db
        db.execute(
            "DELETE FROM dialogue_lines WHERE project_id = ?",
            (str(project_id),),
        )
        db.execute(
            "DELETE FROM scenes WHERE project_id = ?", (str(project_id),)
        )
        for row in snapshot.get("scenes", []):
            cols = sorted(row.keys())
            db.execute(
                f"INSERT INTO scenes ({', '.join(cols)})"
                f" VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols),
            )
        for row in snapshot.get("lines", []):
            cols = sorted(row.keys())
            db.execute(
                f"INSERT INTO dialogue_lines ({', '.join(cols)})"
                f" VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols),
            )

    def _push_undo(
        self, label: str, project_id: str, before: Dict[str, Any]
    ) -> None:
        after = self._structure_snapshot(project_id)
        self._undo_history.append((label, project_id, before, after))
        del self._undo_history[:-20]  # keep last 20 ops
        self._redo_history.clear()

    def undo_label(self) -> str:
        return self._undo_history[-1][0] if self._undo_history else ""

    def redo_label(self) -> str:
        return self._redo_history[-1][0] if self._redo_history else ""

    def undo(self) -> Tuple[bool, str]:
        """Restore the structure snapshot taken BEFORE the last op."""
        if not self._undo_history:
            return False, "Nothing to undo."
        label, project_id, before, after = self._undo_history.pop()
        if self.container is None:
            return False, "Database unavailable."
        try:
            self._restore_structure(project_id, before)
        except Exception as exc:  # noqa: BLE001
            return False, f"Undo failed: {exc}"
        self._redo_history.append((label, project_id, before, after))
        return True, f"Undid: {label}"

    def redo(self) -> Tuple[bool, str]:
        if not self._redo_history:
            return False, "Nothing to redo."
        label, project_id, before, after = self._redo_history.pop()
        if self.container is None:
            return False, "Database unavailable."
        try:
            self._restore_structure(project_id, after)
        except Exception as exc:  # noqa: BLE001
            return False, f"Redo failed: {exc}"
        self._undo_history.append((label, project_id, before, after))
        return True, f"Redid: {label}"

    def _scene_row(
        self, project_id: str, scene_number: Any
    ) -> Optional[Dict[str, Any]]:
        if self.container is None:
            return None
        try:
            row = self.container.get("database").db.fetch_one(
                "SELECT * FROM scenes WHERE project_id = ?"
                " AND scene_number = ?",
                (str(project_id), int(scene_number)),
            )
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def copy_scene(
        self, project_id: str, scene_number: Any
    ) -> Tuple[bool, str]:
        """Copy a scene (+dialogue) into the in-app scene clipboard."""
        row = self._scene_row(project_id, scene_number)
        if row is None:
            return False, "Select a scene first."
        try:
            lines = [
                dict(r)
                for r in self.container.get("database").db.fetch_all(
                    "SELECT * FROM dialogue_lines WHERE scene_id = ?"
                    " ORDER BY line_number",
                    (str(row["id"]),),
                )
            ]
        except Exception:  # noqa: BLE001
            lines = []
        self.scene_clipboard = {
            "project_id": str(project_id),
            "scene": row,
            "lines": lines,
        }
        title = row.get("scene_title") or f"Scene {scene_number}"
        return True, f"Scene copied: {title}"

    def paste_scene(
        self, project_id: str, after_number: Any = 0
    ) -> Tuple[bool, str]:
        """Insert the clipboard scene after <after_number> (0 = end)."""
        clip = getattr(self, "scene_clipboard", None)
        if not clip:
            return False, "Clipboard is empty — copy a scene first."
        if self.container is None:
            return False, "Database unavailable."
        before = self._structure_snapshot(project_id)
        db = self.container.get("database").db
        order = list(before["order"])
        try:
            after = int(after_number or 0)
        except (TypeError, ValueError):
            after = 0
        index = after if 0 < after <= len(order) else len(order)
        now = _utcnow()
        scene = dict(clip["scene"])
        new_scene_id = f"{scene['id']}_copy{len(order) + 1}"
        scene["id"] = new_scene_id
        scene["project_id"] = str(project_id)
        scene["created_at"] = scene["updated_at"] = now
        cols = sorted(scene.keys())
        try:
            db.execute(
                f"INSERT INTO scenes ({', '.join(cols)})"
                f" VALUES ({', '.join('?' for _ in cols)})",
                tuple(scene[c] for c in cols),
            )
            new_line_ids: Dict[str, str] = {}
            for line in clip["lines"]:
                row = dict(line)
                old_id = str(row["id"])
                row["id"] = f"{old_id}_copy{index}"
                row["project_id"] = str(project_id)
                row["scene_id"] = new_scene_id
                row["created_at"] = row["updated_at"] = now
                lcols = sorted(row.keys())
                db.execute(
                    f"INSERT INTO dialogue_lines ({', '.join(lcols)})"
                    f" VALUES ({', '.join('?' for _ in lcols)})",
                    tuple(row[c] for c in lcols),
                )
                new_line_ids[old_id] = row["id"]
        except Exception as exc:  # noqa: BLE001
            self._restore_structure(project_id, before)
            return False, f"Paste failed (nothing changed): {exc}"
        order.insert(index, new_scene_id)
        self._renumber(project_id, order)
        self._push_undo(f"paste scene at #{index + 1}", project_id, before)
        return True, f"Scene pasted as #{index + 1}."

    def delete_scene(
        self, project_id: str, scene_number: Any
    ) -> Tuple[bool, str]:
        row = self._scene_row(project_id, scene_number)
        if row is None:
            return False, "Select a scene first."
        before = self._structure_snapshot(project_id)
        db = self.container.get("database").db
        try:
            db.execute(
                "DELETE FROM dialogue_lines WHERE scene_id = ?",
                (str(row["id"]),),
            )
            db.execute(
                "DELETE FROM scenes WHERE id = ?", (str(row["id"]),)
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Delete failed: {exc}"
        order = [i for i in before["order"] if i != str(row["id"])]
        self._renumber(project_id, order)
        title = row.get("scene_title") or f"Scene {scene_number}"
        self._push_undo(f"delete scene '{title}'", project_id, before)
        return True, f"Deleted scene: {title}"

    def reorder_scene(
        self, project_id: str, scene_number: Any, to_index: Any
    ) -> Tuple[bool, str]:
        """Drag-reorder: move scene <scene_number> to index <to_index>."""
        row = self._scene_row(project_id, scene_number)
        if row is None:
            return False, "Select a scene first."
        before = self._structure_snapshot(project_id)
        order = list(before["order"])
        old_index = order.index(str(row["id"]))
        try:
            new_index = max(0, min(int(to_index), len(order) - 1))
        except (TypeError, ValueError):
            new_index = old_index
        if new_index == old_index:
            return True, "Scene already in position."
        order.insert(new_index, order.pop(old_index))
        self._renumber(project_id, order)
        label = f"move scene to #{new_index + 1}"
        self._push_undo(label, project_id, before)
        return True, f"Moved scene to position {new_index + 1}."

    def _renumber(self, project_id: str, order: List[str]) -> None:
        db = self.container.get("database").db
        for number, scene_id in enumerate(order, start=1):
            db.execute(
                "UPDATE scenes SET scene_number = ?, updated_at = ?"
                " WHERE id = ?",
                (number, _utcnow(), str(scene_id)),
            )


def scene_card_lines(scene: Dict[str, Any]) -> Tuple[str, str, str]:
    """(title, meta, detail) lines for one visual-timeline scene card.

    Pure text so the card content is pinned headless; the Qt shell
    only adds the thumbnail pixmap beside these lines.
    """
    number = int(scene.get("number") or 0)
    title = str(scene.get("title") or "") or f"Scene {number}"
    title_line = f"#{number:02d}   {title}"
    duration = fmt_timecode(scene.get("duration") or 0)
    animation = str(scene.get("animation") or "") or "no animation"
    status = str(scene.get("status") or "pending")
    meta_line = (
        f"⏱ {duration}  ·  ✨ {animation}  ·  {status}"
        f"  ·  starts {scene.get('start_text') or '0:00'}"
    )
    if scene.get("thumb_path") or scene.get("image_matched"):
        image_text = "image ✓"
    else:
        image_text = "no image"
    words = int(scene.get("words") or 0)
    lines = scene.get("lines") or []
    snippet = str(lines[0].get("text") or "")[:60] if lines else ""
    detail_line = f"{image_text}  ·  {words} words"
    if snippet:
        detail_line += f"  ·  “{snippet}”"
    return title_line, meta_line, detail_line



# ======================================================================
# Dialog models (new project / recovery / render complete) — Batch 3
# ======================================================================

def slugify_title(title: str) -> str:
    """Filesystem-safe slug for project folders ('My Doc!' -> 'my-doc')."""
    text = "".join(
        ch.lower() if ch.isalnum() else "-" for ch in str(title or "")
    ).strip("-")
    return "-".join(part for part in text.split("-") if part) or "untitled"


class UiDialogsMixin:
    """Dialog-brains (mixed into UiViewModel); Qt dialogs just paint."""

    # -- new project -----------------------------------------------------
    def new_project_defaults(self, title: str = "") -> Dict[str, Any]:
        return {
            "title": str(title or ""),
            "folder": f"projects/{slugify_title(title)}",
        }

    def validate_new_project(
        self, title: str, folder: str
    ) -> Tuple[bool, str]:
        if not str(title or "").strip():
            return False, "Give the project a title."
        if not str(folder or "").strip():
            return False, "Choose a project folder."
        try:
            Path(str(folder)).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"Cannot create project folder: {exc}"
        return True, ""

    # -- crash recovery (render_progress markers) -------------------------
    def recovery_candidates(self) -> List[Dict[str, Any]]:
        """Interrupted, resumable renders (RULE 7: [] on any trouble)."""
        if self.container is None:
            return []
        try:
            db = self.container.get("database").db
            rows = db.fetch_all(
                "SELECT rp.project_id, rp.current_stage,"
                " rp.stage_percent, rp.total_scenes, rp.error_count,"
                " rp.last_error, rp.updated_at, p.title"
                " FROM render_progress rp"
                " LEFT JOIN projects p ON p.id = rp.project_id"
                " WHERE rp.is_resumable = 1"
                " AND rp.current_stage != 'completed'"
                " ORDER BY rp.updated_at DESC"
            )
        except Exception:  # noqa: BLE001
            return []
        candidates = []
        for row in rows or []:
            candidates.append(
                {
                    "project_id": str(row.get("project_id") or ""),
                    "title": str(row.get("title") or row.get("project_id")),
                    "stage": str(row.get("current_stage") or "?"),
                    "percent": float(row.get("stage_percent") or 0.0),
                    "error_count": int(row.get("error_count") or 0),
                    "updated_at": str(row.get("updated_at") or ""),
                }
            )
        return candidates

    def discard_recovery(self, project_id: str) -> Tuple[bool, str]:
        """User chose to drop the resume marker for a project."""
        if self.container is None:
            return False, "Database unavailable."
        try:
            self.container.get("database").db.execute(
                "DELETE FROM render_progress WHERE project_id = ?",
                (str(project_id),),
            )
            return True, "Recovery marker discarded."
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not discard marker: {exc}"

    # -- pre-render report ----------------------------------------------
    def pre_render_report_model(
        self,
        script_path: str,
        images_folder: str,
        project_folder: str,
        title: str = "",
    ) -> Dict[str, Any]:
        """Pre-flight quality checks shown BEFORE a render starts.

        Mirrors what the pipeline would trip over (script format,
        images, writable folder, FFmpeg/Piper presence, license, Drive)
        so the user fixes problems before burning render time. RULE 7:
        every check degrades to a row, never an exception.
        """
        rows: List[Dict[str, str]] = []
        errors = 0
        warnings = 0

        def add(label: str, value: str, level: str = "ok") -> None:
            rows.append({"label": label, "value": value, "level": level})

        words = 0
        script = Path(str(script_path or "")).expanduser()
        if not script_path or not script.is_file():
            add("Script file", "missing — choose a script first", "error")
            errors += 1
        else:
            suffix = script.suffix.lower()
            if suffix not in IMPORT_KINDS["script"]:
                add(
                    "Script format",
                    f"'{suffix or '(none)'}' unsupported — use TXT, "
                    "JSON, CSV, DOCX or PDF",
                    "error",
                )
                errors += 1
            else:
                add("Script format", suffix.lstrip(".").upper(), "ok")
            try:
                add("Script size", _fmt_bytes(script.stat().st_size),
                    "info")
            except OSError:
                pass
            if suffix in (".txt", ".json", ".csv"):
                try:
                    words = len(
                        script.read_text(
                            encoding="utf-8", errors="replace"
                        ).split()
                    )
                    if words:
                        add("Script length",
                            f"≈{words} spoken words", "ok")
                    else:
                        add("Script length", "script is empty", "warn")
                        warnings += 1
                except OSError as exc:
                    add("Script length", f"unreadable: {exc}", "warn")
                    warnings += 1
            else:
                add(
                    "Script length",
                    f"counted after the {suffix[1:].upper()} parse stage",
                    "info",
                )

        images = Path(str(images_folder or "")).expanduser()
        if not images_folder or not images.is_dir():
            add("Images folder", "missing — choose a folder", "error")
            errors += 1
        else:
            count = 0
            try:
                count = sum(
                    1
                    for item in images.iterdir()
                    if item.is_file()
                    and item.suffix.lower() in IMPORT_KINDS["image"]
                )
            except OSError:
                count = 0
            if count:
                add("Images", f"{count} JPG/PNG ready to match", "ok")
            else:
                add("Images",
                    "no JPG/PNG found — scenes will lack visuals",
                    "warn")
                warnings += 1

        folder = Path(str(project_folder or "")).expanduser()
        if not project_folder:
            add("Project folder", "missing — choose where to render",
                "error")
            errors += 1
        else:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(str(folder)).free
                add("Project folder",
                    f"writable — {_fmt_bytes(free)} free", "ok")
            except OSError as exc:
                add("Project folder", f"not writable: {exc}", "error")
                errors += 1

        engines = self.engines_status()
        if engines.get("ffmpeg"):
            add("FFmpeg", "found", "ok")
        else:
            add("FFmpeg", "NOT found — the export stage will fail",
                "warn")
            warnings += 1
        if engines.get("piper"):
            add("Piper TTS", "found", "ok")
        else:
            add("Piper TTS", "NOT found — narration will fail", "warn")
            warnings += 1
        add("License", self.license_summary()["status"], "info")
        drive = self.drive_upload_status()
        add(
            "Drive backup",
            "configured — uploads after render"
            if drive.get("configured")
            else "not configured (optional)",
            "info",
        )

        if words:
            estimate = (
                f"≈{fmt_timecode(words / 150.0 * 60)} of narration at "
                f"~150 wpm · ≈{max(1, round(words / 45))} scenes (rough)"
            )
        else:
            estimate = "Duration and scene count settle after parsing."
        ready = errors == 0
        if ready:
            summary = "Ready to render" + (
                f" — {warnings} warning(s)." if warnings
                else " — no issues found."
            )
        else:
            summary = f"Fix {errors} error(s) before rendering."
        return {
            "ready": ready,
            "title": str(title or "")
            or (script.stem.replace("_", " ") if script_path else ""),
            "rows": rows,
            "errors": errors,
            "warnings": warnings,
            "words": words,
            "estimate_text": estimate,
            "summary_text": summary,
        }

    # -- image matching preview (3.0.4 review #1) ----------------------
    @staticmethod
    def _norm_match(text: str) -> str:
        return "".join(
            ch for ch in str(text or "").lower() if ch.isalnum())

    def _match_score(self, scene_text: str, stem: str) -> float:
        left = self._norm_match(scene_text)
        right = self._norm_match(stem)
        if not left or not right:
            return 0.0
        try:  # thefuzz ships as a dep — sharper fuzzy matching
            from thefuzz import fuzz
            return float(fuzz.ratio(left, right)) / 100.0
        except Exception:  # noqa: BLE001 - stdlib fallback stays honest
            from difflib import SequenceMatcher
            return float(SequenceMatcher(None, left, right).ratio())

    def _recent_project_scenes(self) -> List[Dict[str, Any]]:
        try:
            projects = self.timeline_projects() or []
        except Exception:  # noqa: BLE001
            return []
        for project in projects:
            try:
                rows = self._scene_rows_all(str(project.get("id") or ""))
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                return list(rows)
        return []

    def pre_render_match_report(
        self,
        images_folder: str = "",
        scenes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Per-scene image-match preview BEFORE a render (3.0.4 #1).

        Prefers a real matcher module when one is installed
        (``file_parser_match`` / ``image_matcher`` / ``scene_image_matcher``
        with ``best_match``/``match_scene``/``match``); otherwise runs the
        built-in fuzzy matcher (thefuzz, difflib fallback). Every failure
        degrades to an honest note — never an exception (RULE 7).
        """
        images: List[str] = []
        folder = Path(str(images_folder or "")).expanduser()
        if images_folder and folder.is_dir():
            try:
                images = sorted(
                    item.name
                    for item in folder.iterdir()
                    if item.is_file()
                    and item.suffix.lower() in IMPORT_KINDS["image"]
                )
            except OSError:
                images = []
        stems = [Path(name).stem for name in images]

        engine_module = None
        module_getter = getattr(getattr(self, "engine", None),
                                "module", None)
        if callable(module_getter):
            for name in ("file_parser_match", "image_matcher",
                         "scene_image_matcher"):
                try:
                    candidate = module_getter(name)
                except Exception:  # noqa: BLE001
                    candidate = None
                if candidate is not None:
                    engine_module = candidate
                    break

        def engine_score(text: str, stem_list: List[str]):
            for method_name in ("best_match", "match_scene", "match"):
                func = getattr(engine_module, method_name, None)
                if not callable(func):
                    continue
                try:
                    return func(text, stem_list)
                except TypeError:
                    continue
                except Exception:  # noqa: BLE001
                    return None
            return None

        if scenes is not None:
            scene_list = list(scenes)
        else:
            scene_list = self._recent_project_scenes()

        rows: List[Dict[str, Any]] = []
        used_engine = False
        for index, scene in enumerate(scene_list, start=1):
            try:
                number = int(scene.get("scene_number") or index)
            except (TypeError, ValueError):
                number = index
            title = str(scene.get("scene_title") or f"Scene {number}")
            best_image = ""
            best = 0.0
            parsed = False
            if engine_module is not None and stems:
                reply = engine_score(title, list(stems))
                if isinstance(reply, dict):
                    best_image = str(
                        reply.get("image") or reply.get("stem") or "")
                    try:
                        best = float(reply.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        best = 0.0
                    parsed = bool(best_image)
                elif isinstance(reply, (tuple, list)) and reply:
                    best_image = str(reply[0] or "")
                    if len(reply) > 1:
                        try:
                            best = float(reply[1])
                        except (TypeError, ValueError):
                            best = 1.0
                    else:
                        best = 1.0
                    parsed = bool(best_image)
                if best > 1.0:
                    best = best / 100.0
                if parsed:
                    used_engine = True
            if not parsed and stems:
                left = self._norm_match(title)
                scene_tag = f"scene{number}"
                for name, stem in zip(images, stems):
                    right = self._norm_match(stem)
                    if left and right and (
                            left == right or right == scene_tag
                            or right == self._norm_match(str(number))):
                        best = 1.0
                        best_image = name
                        break
                    value = self._match_score(title, stem)
                    if value > best:
                        best = value
                        best_image = name
            best = max(0.0, min(1.0, float(best)))
            if best >= 0.999:
                status = "exact"
            elif best >= 0.70:
                status = "fuzzy"
            else:
                status = "no_match"
                best_image = ""
            rows.append({
                "scene": number,
                "title": title,
                "image": best_image,
                "status": status,
                "confidence": round(best, 2),
            })
        summary = {
            "exact": sum(1 for row in rows if row["status"] == "exact"),
            "fuzzy": sum(1 for row in rows if row["status"] == "fuzzy"),
            "no_match": sum(
                1 for row in rows if row["status"] == "no_match"),
        }
        summary_text = (
            f"{summary['exact']} exact matches, "
            f"{summary['fuzzy']} fuzzy matches, "
            f"{summary['no_match']} unmatched")
        note = ""
        if not scene_list:
            note = ("No scenes yet — parsing the script creates them, "
                    "then the match runs here.")
        elif not images:
            note = ("No JPG/PNG images found — check the images "
                    "folder path.")
        return {
            "available": bool(scene_list),
            "images": len(images),
            "rows": rows,
            "summary": summary,
            "summary_text": summary_text,
            "matcher": "engine" if used_engine else "local",
            "note": note,
        }

    # -- render warnings detail (3.0.4 review #2) ----------------------
    def render_warnings_list(self) -> Tuple[bool, str, List[str]]:
        """Warnings captured during the most recent render this session.

        Stored by ``render_complete_model`` straight from the render
        result; an empty list honestly means no recorded warnings.
        """
        saved = getattr(self, "_last_render_warnings", None) or []
        warnings = [str(item) for item in saved]
        if warnings:
            message = f"{len(warnings)} warning(s) from the last render."
        else:
            message = "No render warnings recorded this session."
        return True, message, warnings

    # -- audio duration for the waveform axis (3.0.4 review #4) --------
    def audio_file_duration(self, path: str = "") -> Tuple[bool, str, float]:
        """Seconds of audio at ``path`` via the stdlib ``wave`` reader."""
        import wave
        target = Path(str(path or "")).expanduser()
        if not path or not target.is_file():
            return False, "Audio file not found.", 0.0
        try:
            with wave.open(str(target), "rb") as handle:
                frames = handle.getnframes()
                rate = handle.getframerate() or 1
            return True, "", frames / float(rate)
        except (wave.Error, EOFError, OSError) as exc:
            return False, f"Unreadable audio: {exc}", 0.0

    # -- render complete -------------------------------------------------
    def render_complete_model(
        self, result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Everything the render-complete dialog shows (file + Drive).

        Beyond the raw file facts this enriches from the DB (RULE 7:
        trouble simply leaves fields empty) — duration from
        render_history, thumbnail from the thumbnails stage (falling
        back to a scene image), and YouTube-ready chapter markers
        from scene start times.
        """
        data = result.get("data") or {}
        output = str(data.get("output_file_path") or "")
        exists = bool(output) and Path(output).is_file()
        size_text = ""
        if exists:
            try:
                size_text = _fmt_bytes(Path(output).stat().st_size)
            except OSError:
                size_text = ""
        status = self.drive_upload_status()
        project_id = str(data.get("project_id") or "")
        duration_value = data.get("video_duration_seconds")
        duration_text = (
            fmt_timecode(duration_value) if duration_value else ""
        )
        thumbnail_path: Optional[str] = None
        chapters: List[Dict[str, Any]] = []
        scene_rows: List[Dict[str, Any]] = []
        if self.container is not None:
            try:
                db = self.container.get("database").db
                if not project_id and output:
                    row = db.fetch_one(
                        "SELECT id FROM projects"
                        " WHERE last_render_output_path = ?",
                        (output,),
                    )
                    if row:
                        project_id = str(row.get("id") or "")
                if not duration_text and output:
                    row = db.fetch_one(
                        "SELECT video_duration_seconds FROM render_history"
                        " WHERE output_file_path = ?"
                        " ORDER BY completed_at DESC LIMIT 1",
                        (output,),
                    )
                    if row and row.get("video_duration_seconds"):
                        duration_text = fmt_timecode(
                            row["video_duration_seconds"]
                        )
                if project_id:
                    row = db.fetch_one(
                        "SELECT file_path FROM thumbnails"
                        " WHERE project_id = ?"
                        " ORDER BY is_selected DESC, variation_number"
                        " LIMIT 1",
                        (project_id,),
                    )
                    candidate = str((row or {}).get("file_path") or "")
                    if candidate and Path(candidate).is_file():
                        thumbnail_path = candidate
                    scene_rows = list(
                        db.fetch_all(
                            "SELECT scene_number, scene_title,"
                            " chapter_title, start_time,"
                            " proxy_image_path, image_file_path"
                            " FROM scenes WHERE project_id = ?"
                            " ORDER BY scene_number",
                            (project_id,),
                        )
                        or []
                    )
            except Exception:  # noqa: BLE001 - enrichment is advisory
                scene_rows = []
        if thumbnail_path is None:  # fall back to a real scene image
            for row in scene_rows:
                for key in ("proxy_image_path", "image_file_path"):
                    candidate = str(row.get(key) or "")
                    if candidate and Path(candidate).is_file():
                        thumbnail_path = candidate
                        break
                if thumbnail_path:
                    break
        first = True
        for row in scene_rows:
            seconds = float(row.get("start_time") or 0.0)
            if first:  # YouTube requires the first chapter at 0:00
                seconds = 0.0
                first = False
            title = (
                row.get("chapter_title") or row.get("scene_title")
                or f"Scene {row.get('scene_number')}"
            )
            chapters.append(
                {"time": fmt_timecode(seconds), "seconds": seconds,
                 "title": str(title)}
            )
        warnings_list = list(
            data.get("warnings") or result.get("warnings") or [])
        self._last_render_warnings = warnings_list
        return {
            "output": output,
            "exists": exists,
            "size_text": size_text,
            "drive_ready": bool(status.get("configured")),
            "drive_status_text": status.get("detail") or "",
            "warnings": warnings_list,
            "project_id": project_id,
            "duration_text": duration_text,
            "thumbnail_path": thumbnail_path,
            "chapters": chapters,
            "chapters_text": "\n".join(
                f"{c['time']} {c['title']}" for c in chapters
            ),
        }

    def upload_render_to_drive(self, path: str) -> Tuple[bool, str]:
        """Render-complete dialog button: one-off upload via the seam."""
        module = self._drive_module()
        if module is None:
            return False, "Drive upload module unavailable."
        try:
            reply = module.upload_file(path)
        except Exception as exc:  # noqa: BLE001
            return False, f"Upload failed: {exc}"
        if not reply.get("success"):
            return False, str(reply.get("error") or "Upload failed.")
        if reply.get("data", {}).get("skipped"):
            return False, f"Drive upload: {reply['data']['skipped']}."
        link = (reply.get("data") or {}).get("web_view_link")
        name = (reply.get("data") or {}).get("name") or path
        return True, f"Uploaded {name}" + (f" — {link}" if link else ".")

    # -- voice clone dialog (Tools menu, spec §15) ----------------------
    def voice_clone_model(self) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        if self.container is not None:
            try:
                fetched = self.container.get("database").db.fetch_all(
                    "SELECT voice_name, display_name, is_ready, engine,"
                    " sample_file_path FROM cloned_voices"
                    " ORDER BY created_at DESC"
                )
                for row in fetched or []:
                    rows.append(
                        {
                            "name": str(row.get("display_name") or "?"),
                            "voice_name": str(row.get("voice_name") or ""),
                            "ready": bool(row.get("is_ready")),
                            "engine": str(row.get("engine") or "xtts"),
                            "sample": str(row.get("sample_file_path")
                                          or ""),
                        }
                    )
            except Exception:  # noqa: BLE001
                rows = []
        return {"clones": rows, "count": len(rows)}

    def validate_voice_clone(
        self, display_name: str, sample_path: str
    ) -> Tuple[bool, str]:
        if not str(display_name or "").strip():
            return False, "Give the cloned voice a name."
        sample = Path(str(sample_path or "")).expanduser()
        if not sample.is_file():
            return False, "Choose an existing reference sample."
        if sample.suffix.lower() not in (".wav", ".mp3"):
            return False, "Reference sample must be a WAV or MP3 file."
        return True, ""

    def add_voice_clone(
        self, display_name: str, sample_path: str
    ) -> Tuple[bool, str]:
        """Queue a clone record (models train when the TTS engine adds
        cloning support — stored honestly as not-ready)."""
        ok, message = self.validate_voice_clone(display_name, sample_path)
        if not ok:
            return False, message
        if self.container is None:
            return False, "Database unavailable."
        name = str(display_name).strip()
        voice_name = slugify_title(name).replace("-", "_")
        now = _utcnow()
        try:
            self.container.get("database").db.execute(
                "INSERT INTO cloned_voices (id, voice_name,"
                " display_name, sample_file_path, engine, is_ready,"
                " notes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'xtts', 0, ?, ?, ?)",
                (
                    f"clone_{voice_name}_{now[:10]}",
                    voice_name,
                    name,
                    str(sample_path),
                    "Queued — cloning starts when an XTTS-class engine "
                    "is installed via Engine Manager.",
                    now,
                    now,
                ),
            )
            return True, (
                f"Cloned voice queued: {name} (status: awaiting engine)."
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not queue clone: {exc}"

    # -- admin key generator dialog (Tools menu, spec §15) --------------
    def key_generator_model(self) -> Dict[str, Any]:
        """HWID helper; key generation is an admin-only tool.

        User builds ship no key generator (they activate keys). The
        machine HWID IS shown — that is what a support channel needs
        to mint a key. Honest about the boundary.
        """
        hwid = ""
        if self.license_manager is not None:
            try:
                generate = getattr(self.license_manager, "generate_hwid",
                                   None)
                hwid = str(generate()) if callable(generate) else ""
            except Exception:  # noqa: BLE001
                hwid = ""
        summary = self.license_summary()
        return {
            "available": False,
            "hwid": hwid or "(unavailable without the engine)",
            "license_status": summary["status"],
            "message": (
                "Key generation lives in the separate admin build, not "
                "in the user app. Send the machine ID above to your "
                "license issuer; activate the returned key in Settings."
            ),
        }

    # -- import project ZIP (File menu, spec §5) -------------------------
    def import_zip(self, zip_path: str) -> Dict[str, Any]:
        """Extract a project ZIP and stage its files via apply_import.

        Returns render-form prefills {script_path, images_folder,
        project_folder, title} or {success: False, error}.
        """
        archive = Path(str(zip_path or "")).expanduser()
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            return {"success": False, "error": "Choose a .zip file."}
        destination = Path("projects") / f"{archive.stem}-import"
        try:
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(archive)) as bundle:
                bad = bundle.testzip()
                if bad is not None:
                    return {"success": False,
                            "error": f"ZIP is corrupt (member: {bad})."}
                bundle.extractall(str(destination))
        except (OSError, zipfile.BadZipFile) as exc:
            return {"success": False, "error": f"Cannot read ZIP: {exc}"}
        files = [
            str(p) for p in sorted(destination.rglob("*")) if p.is_file()
        ]
        staged = self.apply_import(self.import_plan(files),
                                   str(destination))
        if not staged.get("copied"):
            return {
                "success": False,
                "error": "ZIP had no supported files "
                "(scripts TXT/JSON/CSV/DOCX/PDF, images JPG/PNG, "
                "audio MP3/WAV).",
            }
        return {
            "success": True,
            "script_path": staged.get("script_path"),
            "images_folder": staged.get("images_folder"),
            "project_folder": str(destination),
            "title": archive.stem.replace("_", " ").replace("-", " "),
            "copied": staged.get("copied", 0),
            "errors": staged.get("errors") or [],
        }

    # -- backup / autosave (File menu + workflow_spec) --------------------
    def _database_file(self) -> Optional[Path]:
        """The LIVE database path: db service first, config fallback."""
        candidates: List[Path] = []
        try:
            live = getattr(
                self.container.get("database").db, "db_path", None
            )
            if live:
                candidates.append(Path(str(live)))
        except Exception:  # noqa: BLE001
            pass
        try:
            config = self.container.get("config")
            raw = str(config.get("database_path",
                                 "database/autopilot.db"))
            candidates.append(Path(raw))
            candidates.append(
                Path(str(config.get("config_folder", "config"))).parent
                / raw
            )
        except Exception:  # noqa: BLE001
            pass
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def backup_now(self) -> Tuple[bool, str]:
        """Zip the database + all config JSONs into backups/ (Ctrl+B)."""
        if self.container is None:
            return False, "Database unavailable."
        try:
            config = self.container.get("config")
            db_path = self._database_file()
            config_folder = Path(str(config.get("config_folder",
                                                "config")))
            backup_dir = Path(str(config.get("backup_folder", "backups")))
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            target = backup_dir / f"autopilot_backup_{stamp}.zip"
            with zipfile.ZipFile(str(target), "w",
                                 zipfile.ZIP_DEFLATED) as bundle:
                if db_path is not None:
                    bundle.write(str(db_path), "database/autopilot.db")
                for cfg in sorted(config_folder.glob("*.json")):
                    bundle.write(str(cfg), f"config/{cfg.name}")
            return True, f"Backup written: {target}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Backup failed: {exc}"

    def autosave_tick(self) -> Tuple[bool, str]:
        """Rotate backups/autosave_1..3.zip (workflow_spec auto-save).

        Called by the shell's QTimer; interval comes from
        app_settings.json auto_save_interval_seconds (default 300).
        """
        if self.container is None:
            return False, "Database unavailable."
        try:
            config = self.container.get("config")
            db_path = self._database_file()
            backup_dir = Path(str(config.get("backup_folder", "backups")))
            backup_dir.mkdir(parents=True, exist_ok=True)
            oldest = backup_dir / "autosave_3.zip"
            middle = backup_dir / "autosave_2.zip"
            newest = backup_dir / "autosave_1.zip"
            if oldest.is_file():
                oldest.unlink()
            if middle.is_file():
                middle.replace(oldest)
            if newest.is_file():
                newest.replace(middle)
            with zipfile.ZipFile(str(newest), "w",
                                 zipfile.ZIP_DEFLATED) as bundle:
                if db_path is not None:
                    bundle.write(str(db_path), "database/autopilot.db")
            return True, f"Auto-save snapshot: {newest.name}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Auto-save failed: {exc}"

    def autosave_interval_seconds(self) -> int:
        if self.container is None:
            return 300
        try:
            value = self.container.get("config").get(
                "auto_save_interval_seconds", 300
            )
            return max(60, int(value))
        except Exception:  # noqa: BLE001
            return 300

    # -- first-run setup wizard (workflow_spec) ----------------------------
    def first_run_model(self) -> Dict[str, Any]:
        """Wizard appears when engines are missing (first setup)."""
        engines = self.engine_install_model()
        done = False
        if self.container is not None:
            try:
                done = bool(self.container.get("config").get(
                    "first_run_wizard_done", False))
            except Exception:  # noqa: BLE001
                done = False
        return {
            "needs_wizard": bool(engines["missing"]) and not done,
            "missing": engines["missing"],
            "summary": engines["summary"],
            "rows": engines["rows"],
        }

    def mark_first_run_done(self) -> None:
        if self.container is None:
            return
        try:
            self.container.get("config").set("first_run_wizard_done", True)
        except Exception:  # noqa: BLE001
            pass


def _fmt_bytes(size: Any) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"



# ======================================================================
# Panel models: Grade / Audio / Voice Store / Batch (spec §10..§14)
# ======================================================================

# (key, label, min, max, default) — spec Section 10 grade sliders.
GRADE_SLIDERS: Tuple[Tuple[str, str, int, int, int], ...] = (
    ("brightness", "Brightness", -50, 50, 0),
    ("contrast", "Contrast", 50, 150, 100),
    ("saturation", "Saturation", 0, 200, 100),
    ("vignette", "Vignette", 0, 100, 0),
    ("film_grain", "Film Grain", 0, 100, 0),
)

NOTIFICATION_TYPES: Tuple[str, ...] = (
    "info", "success", "warning", "error",
)
_NOTIFICATION_ICON = {
    "info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗",
}


def notification_model(level: Any, text: Any) -> Dict[str, Any]:
    """Toast model (spec §17): slide-in top-right, 4s auto-dismiss."""
    key = str(level) if str(level) in NOTIFICATION_TYPES else "info"
    return {
        "level": key,
        "icon": _NOTIFICATION_ICON[key],
        "text": str(text or ""),
        "timeout_ms": 4000,
    }


def clamp(value: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(lo, min(hi, number))


class UiPanelsMixin:
    """Brains for the Grade/Audio/Voices/Batch pages (Qt-free)."""

    # -- grade (spec §10) -------------------------------------------------
    def color_presets(self) -> List[Dict[str, str]]:
        data: Any = None
        if self.container is not None:
            try:
                data = self.container.get("config").get_config(
                    "color_grade_presets"
                )
            except Exception:  # noqa: BLE001
                data = None
        if not isinstance(data, dict):
            return []
        default_id = str(data.get("default_preset") or "")
        out = []
        for preset in data.get("presets") or []:
            pid = str(preset.get("id") or "")
            if not pid:
                continue
            label = str(preset.get("name") or pid)
            if pid == default_id:
                label += "  (default)"
            out.append({"id": pid, "label": label})
        return out

    def grade_sliders(self) -> List[Dict[str, Any]]:
        return [
            {"key": k, "label": label, "min": lo, "max": hi,
             "default": default}
            for k, label, lo, hi, default in GRADE_SLIDERS
        ]

    def lut_files(self) -> List[str]:
        """*.cube LUTs from assets/luts (RULE 3: via config root)."""
        root: Optional[Path] = None
        if self.container is not None:
            try:
                folder = self.container.get("config").get(
                    "config_folder", "config"
                )
                root = Path(str(folder)).parent
            except Exception:  # noqa: BLE001
                root = None
        folder = (root or Path(".")) / "assets" / "luts"
        try:
            return sorted(
                p.name for p in folder.glob("*.cube") if p.is_file()
            )
        except OSError:
            return []

    @staticmethod
    def grade_override(values: Dict[str, Any], lut: str = "",
                       lut_opacity: Any = 0.8) -> Dict[str, Any]:
        """Slider values -> scene color_grade_override JSON payload."""
        return {
            "brightness": clamp(values.get("brightness"), -50, 50) / 100,
            "contrast": clamp(values.get("contrast"), 50, 150, 100) / 100,
            "saturation":
                clamp(values.get("saturation"), 0, 200, 100) / 100,
            "vignette_enabled":
                clamp(values.get("vignette"), 0, 100) > 0,
            "vignette_strength":
                clamp(values.get("vignette"), 0, 100) / 100,
            "film_grain_enabled":
                clamp(values.get("film_grain"), 0, 100) > 0,
            "film_grain_amount":
                clamp(values.get("film_grain"), 0, 100) / 500,
            "lut_file": str(lut or ""),
            "lut_opacity": clamp(lut_opacity, 0, 100, 80) / 100,
        }

    def apply_grade_preset(
        self, project_id: str, preset_id: str
    ) -> Tuple[bool, str]:
        if self.container is None:
            return False, "Database unavailable."
        try:
            self.container.get("database").db.execute(
                "UPDATE projects SET color_grade_preset = ?,"
                " updated_at = ? WHERE id = ?",
                (str(preset_id), _utcnow(), str(project_id)),
            )
            return True, f"Grade preset saved: {preset_id}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not save preset: {exc}"

    def apply_grade_to_all(
        self, project_id: str, override: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """'Apply to All Scenes': write the override to every scene."""
        if self.container is None:
            return False, "Database unavailable."
        payload = json.dumps(override, sort_keys=True)
        try:
            db = self.container.get("database").db
            db.execute(
                "UPDATE scenes SET color_grade_override = ?,"
                " updated_at = ? WHERE project_id = ?",
                (payload, _utcnow(), str(project_id)),
            )
            count = db.fetch_one(
                "SELECT COUNT(*) AS n FROM scenes WHERE project_id = ?",
                (str(project_id),),
            )
            n = int((count or {}).get("n") or 0)
            return True, f"Grade applied to {n} scene(s)."
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not apply grade: {exc}"

    def animation_options(self) -> Dict[str, List[str]]:
        data: Any = None
        if self.container is not None:
            try:
                data = self.container.get("config").get_config(
                    "animation_presets"
                )
            except Exception:  # noqa: BLE001
                data = None
        ids = [
            str(a.get("id"))
            for a in (data or {}).get("animations", []) or []
            if a.get("id")
        ]
        intensities = list(
            ((data or {}).get("intensity_multipliers") or {}).keys()
        )
        return {
            "animations": ids or ["ken_burns", "slow_zoom_in", "static"],
            "intensities": [str(i) for i in intensities]
            or ["subtle", "medium", "dramatic"],
        }

    def transition_options(self) -> List[Dict[str, Any]]:
        data: Any = None
        if self.container is not None:
            try:
                data = self.container.get("config").get_config(
                    "transition_presets"
                )
            except Exception:  # noqa: BLE001
                data = None
        out = []
        for preset in (data or {}).get("presets", []) or []:
            pid = str(preset.get("id") or "")
            if pid:
                out.append(
                    {"id": pid, "label": str(preset.get("name") or pid),
                     "duration": float(preset.get("duration") or 0.8)}
                )
        # v3.0: without a presets file the panel must never be empty.
        return out or [dict(p) for p in FALLBACK_TRANSITIONS]

    def apply_scene_animation(
        self, project_id: str, scene_number: Any, animation: str,
        intensity: str,
    ) -> Tuple[bool, str]:
        return self._update_scene_or_default(
            project_id, scene_number,
            {"animation_type": animation,
             "animation_intensity": intensity},
            {"default_animation": animation},
            f"animation={animation}",
        )

    def apply_scene_transition(
        self, project_id: str, scene_number: Any, in_id: str,
        out_id: str, duration: Any,
    ) -> Tuple[bool, str]:
        dur = clamp(duration, 0.1, 5.0, 0.8)
        return self._update_scene_or_default(
            project_id, scene_number,
            {"transition_in": in_id, "transition_out": out_id,
             "transition_duration": dur},
            {"default_transition": in_id},
            f"transition={in_id}",
        )

    def _update_scene_or_default(
        self, project_id: str, scene_number: Any,
        scene_values: Dict[str, Any],
        project_values: Dict[str, Any], label: str,
    ) -> Tuple[bool, str]:
        """Scene row when a scene is picked, project default otherwise."""
        if self.container is None:
            return False, "Database unavailable."
        db = self.container.get("database").db
        try:
            if scene_number:
                assignments = ", ".join(
                    f"{k} = ?" for k in scene_values
                )
                db.execute(
                    f"UPDATE scenes SET {assignments}, updated_at = ?"
                    " WHERE project_id = ? AND scene_number = ?",
                    tuple(scene_values.values())
                    + (_utcnow(), str(project_id), int(scene_number)),
                )
                return True, f"Scene {scene_number}: {label} saved."
            assignments = ", ".join(f"{k} = ?" for k in project_values)
            db.execute(
                f"UPDATE projects SET {assignments}, updated_at = ?"
                " WHERE id = ?",
                tuple(project_values.values())
                + (_utcnow(), str(project_id)),
            )
            return True, f"Project default: {label} saved."
        except Exception as exc:  # noqa: BLE001
            return False, f"Save failed: {exc}"

    # -- audio (spec §11) ---------------------------------------------------
    def audio_settings(self, project_id: str) -> Dict[str, Any]:
        """Volumes/ducking for the Audio page (real project columns)."""
        model: Dict[str, Any] = {
            "found": False, "narration_volume": 100, "music_volume": 40,
            "sfx_volume": 60, "music_file_path": "",
            "ducking_enabled": True, "ducking_depth": 60,
            "ducking_ceiling": 80, "ducking_attack": 50,
            "ducking_release": 50,
            "fade_in_seconds": 1.5, "fade_out_seconds": 2.0,
            "mute_narration": False, "mute_music": False,
            "mute_sfx": False, "master_volume": 1.0,
        }
        if self.container is None or not project_id:
            return model
        try:
            row = self.container.get("database").db.fetch_one(
                "SELECT narration_volume, music_volume, sfx_volume,"
                " music_file_path FROM projects WHERE id = ?",
                (str(project_id),),
            )
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            return model
        model["found"] = True
        for key in ("narration_volume", "music_volume", "sfx_volume"):
            value = row.get(key)
            if value is not None:
                model[key] = int(clamp(float(value) * 100, 0, 200))
        model["music_file_path"] = str(row.get("music_file_path") or "")
        try:
            config = self.container.get("config")
            for key in ("ducking_enabled", "ducking_depth",
                        "ducking_ceiling", "ducking_attack",
                        "ducking_release", "fade_in_seconds",
                        "fade_out_seconds", "mute_narration",
                        "mute_music", "mute_sfx", "master_volume"):
                value = config.get(key, model[key])
                model[key] = value
        except Exception:  # noqa: BLE001
            pass
        return model

    def save_audio_settings(
        self, project_id: str, values: Dict[str, Any]
    ) -> Tuple[bool, str]:
        if self.container is None:
            return False, "Database unavailable."
        try:
            db = self.container.get("database").db
            db.execute(
                "UPDATE projects SET narration_volume = ?,"
                " music_volume = ?, sfx_volume = ?,"
                " music_file_path = ?, updated_at = ? WHERE id = ?",
                (
                    clamp(values.get("narration_volume"), 0, 200, 100)
                    / 100,
                    clamp(values.get("music_volume"), 0, 200, 40) / 100,
                    clamp(values.get("sfx_volume"), 0, 200, 60) / 100,
                    str(values.get("music_file_path") or ""),
                    _utcnow(),
                    str(project_id),
                ),
            )
            config = self.container.get("config")
            for key in ("ducking_enabled", "ducking_depth",
                        "ducking_ceiling", "ducking_attack",
                        "ducking_release", "master_volume",
                        "fade_in_seconds", "fade_out_seconds",
                        "mute_narration", "mute_music", "mute_sfx"):
                if key in values:
                    config.set(key, values[key])
            return True, "Audio settings saved (apply from next render)."
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not save audio settings: {exc}"

    # -- voice store (spec §13) ---------------------------------------------
    def _voice_module(self) -> Optional[Any]:
        if self.engine is None:
            return None
        try:
            module_fn = getattr(self.engine, "module", None)
            if callable(module_fn):
                return module_fn("voice_store_manager")
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _norm_voice(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(row.get("id") or row.get("voice_name") or ""),
            "name": str(
                row.get("display_name") or row.get("voice_display_name")
                or row.get("voice_name") or row.get("name") or "Voice"
            ),
            "engine": str(row.get("engine") or "?"),
            "language": str(row.get("language") or "en"),
            "gender": str(row.get("gender") or "?"),
            "style": str(row.get("style") or ""),
            "quality": int(row.get("quality_rating") or 0),
            "size_mb": float(
                row.get("file_size_mb") or row.get("model_size_mb") or 0.0
            ),
            "installed": bool(row.get("is_installed")),
            "description": str(row.get("description") or ""),
        }

    def voice_store_model(
        self, query: str = "", gender: str = "", language: str = ""
    ) -> Dict[str, Any]:
        """Voice cards for the store page; DB-backed, engine-verified."""
        rows: List[Dict[str, Any]] = []
        available = False
        module = self._voice_module()
        if module is not None:
            try:
                reply = module.list_voices()
                data = (reply or {}).get("data") or {}
                items = data.get("voices", data if isinstance(data, list)
                                 else [])
                rows = [self._norm_voice(dict(v)) for v in items]
                available = True
            except Exception:  # noqa: BLE001
                rows, available = [], False
        if not rows and self.container is not None:
            try:
                fetched = self.container.get("database").db.fetch_all(
                    "SELECT * FROM voice_store_cache"
                    " ORDER BY quality_rating DESC"
                )
                rows = [self._norm_voice(dict(r)) for r in fetched or []]
            except Exception:  # noqa: BLE001
                rows = []
        needle = str(query or "").strip().lower()
        if needle:
            rows = [
                r for r in rows
                if needle in r["name"].lower()
                or needle in r["description"].lower()
                or needle in r["style"].lower()
            ]
        if gender:
            rows = [
                r for r in rows
                if r["gender"].lower() == str(gender).lower()
            ]
        if language:
            rows = [
                r for r in rows
                if r["language"].lower() == str(language).lower()
            ]
        installed = sum(1 for r in rows if r["installed"])
        return {
            "available": available or bool(rows),
            "voices": rows,
            "count": len(rows),
            "installed_count": installed,
            "summary_text": (
                f"{len(rows)} voice(s) · {installed} installed"
                if rows else
                "No voices in the catalog yet — refresh when engines run."
            ),
        }

    def voice_languages(self) -> List[str]:
        """Distinct catalog languages — data-driven store filter
        (review fix 3.0.3; replaces the old hardcoded 6-item list)."""
        found: set = set()
        try:
            rows = self.voice_store_model().get("voices") or []
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            language = str(row.get("language") or "").strip()
            if language:
                found.add(language)
        return sorted(found)

    def voice_install(self, voice_id: str) -> Tuple[bool, str]:
        module = self._voice_module()
        if module is None:
            return False, "Voice store module unavailable (engine off)."
        try:
            reply = module.install_voice(str(voice_id))
        except Exception as exc:  # noqa: BLE001
            return False, f"Install failed: {exc}"
        if not reply.get("success"):
            return False, str(reply.get("error") or "Install failed.")
        return True, f"Installed voice {voice_id}."

    def voice_remove(self, voice_id: str) -> Tuple[bool, str]:
        module = self._voice_module()
        if module is None:
            return False, "Voice store module unavailable (engine off)."
        try:
            reply = module.uninstall_voice(str(voice_id))
        except Exception as exc:  # noqa: BLE001
            return False, f"Remove failed: {exc}"
        if not reply.get("success"):
            return False, str(reply.get("error") or "Remove failed.")
        return True, f"Removed voice {voice_id}."

    # -- batch queue (spec §14) ----------------------------------------------
    def batch_model(self) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        if self.container is not None:
            try:
                fetched = self.container.get("database").db.fetch_all(
                    "SELECT id, project_title, project_folder_path,"
                    " priority, status, added_at, error_message, notes"
                    " FROM batch_queue ORDER BY priority, added_at"
                )
                for row in fetched or []:
                    notes: Dict[str, Any] = {}
                    try:
                        notes = json.loads(row.get("notes") or "{}")
                    except (ValueError, TypeError):
                        notes = {}
                    rows.append(
                        {
                            "id": str(row.get("id")),
                            "title": str(row.get("project_title") or "?"),
                            "folder": str(
                                row.get("project_folder_path") or ""
                            ),
                            "priority": int(row.get("priority") or 5),
                            "status": str(row.get("status") or "queued"),
                            "added_at": str(row.get("added_at") or ""),
                            "error": str(row.get("error_message") or ""),
                            "script_path": str(notes.get("script_path")
                                               or ""),
                            "images_folder": str(notes.get("images_folder")
                                                 or ""),
                            "channel": str(notes.get("channel") or ""),
                            "job_type": str(notes.get("job_type") or "full"),
                        }
                    )
            except Exception:  # noqa: BLE001
                rows = []
        queued = sum(1 for r in rows if r["status"] == "queued")
        # UI REDESIGN (v3.2.7): surface how many distinct channels are
        # represented in the queue — the whole point of tracking channel
        # per queued item is to make multi-channel batches legible at a
        # glance, not just per-row.
        channels = {r.get("channel") for r in rows if r.get("channel")}
        channel_note = (
            f" across {len(channels)} channels" if len(channels) > 1 else ""
        )
        return {
            "rows": rows, "count": len(rows), "queued": queued,
            "summary_text": (
                f"{queued} queued · {len(rows)} total{channel_note}"
                if rows else "Queue is empty — add the current project."
            ),
        }

    def batch_add(
        self, script_path: str, images_folder: str, project_folder: str,
        title: str = "", priority: Any = 5, channel: str = "",
        job_type: str = "full",
    ) -> Tuple[bool, str]:
        if self.container is None:
            return False, "Database unavailable."
        script = Path(str(script_path or ""))
        if not script.is_file():
            return False, "Add needs an existing script file."
        # FEATURE (v3.2.14): job_type lets a queued item be a full
        # render, or a narrower "audio only" pass (TTS + mix, no video
        # rendering — much faster to queue overnight for review before
        # committing to a full video render). Stored the same safe way
        # as "channel" (v3.2.7) — inside the existing notes JSON column,
        # no schema migration needed.
        job_type = job_type if job_type in ("full", "audio_only") else "full"
        notes = json.dumps(
            {"script_path": str(script),
             "images_folder": str(images_folder or ""),
             "channel": str(channel or ""),
             "job_type": job_type}
        )
        try:
            stamp = _utcnow()
            batch_id = (
                f"batch_{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
            )
            self.container.get("database").db.execute(
                "INSERT INTO batch_queue (id, project_folder_path,"
                " project_title, priority, status, added_at, notes)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (
                    batch_id,
                    str(project_folder or ""),
                    str(title or script.stem.replace("_", " ")),
                    int(clamp(priority, 1, 9, 5)),
                    stamp,
                    notes,
                ),
            )
            return True, f"Queued: {title or script.stem}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not queue: {exc}"

    def batch_remove(self, batch_id: str) -> Tuple[bool, str]:
        if self.container is None:
            return False, "Database unavailable."
        try:
            self.container.get("database").db.execute(
                "DELETE FROM batch_queue WHERE id = ?"
                " AND status = 'queued'",
                (str(batch_id),),
            )
            return True, "Removed from queue."
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not remove: {exc}"

    def batch_move(self, batch_id: str, delta: Any) -> Tuple[bool, str]:
        """Nudge priority up/down (1 = runs first)."""
        if self.container is None:
            return False, "Database unavailable."
        try:
            db = self.container.get("database").db
            row = db.fetch_one(
                "SELECT priority FROM batch_queue WHERE id = ?",
                (str(batch_id),),
            )
            if row is None:
                return False, "Queue item not found."
            priority = int(clamp(
                int(row.get("priority") or 5) + int(delta or 0), 1, 9, 5
            ))
            db.execute(
                "UPDATE batch_queue SET priority = ? WHERE id = ?",
                (priority, str(batch_id)),
            )
            return True, f"Priority set to {priority}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not reorder: {exc}"

    def batch_set_status(
        self, batch_id: str, status: str,
        output: str = "", error: str = "",
    ) -> None:
        if self.container is None:
            return
        try:
            self.container.get("database").db.execute(
                "UPDATE batch_queue SET status = ?,"
                " output_file_path = COALESCE(NULLIF(?, ''),"
                " output_file_path),"
                " error_message = COALESCE(NULLIF(?, ''), error_message)"
                " WHERE id = ?",
                (str(status), str(output), str(error), str(batch_id)),
            )
        except Exception:  # noqa: BLE001
            pass

    def set_export_preset(
        self, project_id: str, preset_id: str
    ) -> Tuple[bool, str]:
        """Grade panel Export tab -> projects.export_preset column."""
        return self._update_scene_or_default(
            project_id, 0, {},
            {"export_preset": str(preset_id or "")},
            f"export preset={preset_id}",
        )

    # -- channel profiles manager (Project menu, workflow_spec) --------
    def channel_profile_rows(self) -> List[Dict[str, Any]]:
        """Channel Profile Manager table rows (id + name + details)."""
        if self.container is None:
            return []
        try:
            fetched = self.container.get("database").db.fetch_all(
                "SELECT * FROM channel_profiles ORDER BY profile_name"
            )
            rows = []
            for row in fetched or []:
                data = dict(row)
                details = {
                    k: v for k, v in data.items()
                    if k not in ("id", "profile_name") and v not
                    in (None, "", "{}")
                }
                rows.append(
                    {
                        "id": str(data.get("id") or ""),
                        "name": str(data.get("profile_name") or "?"),
                        "details": json.dumps(details, indent=1,
                                              sort_keys=True)[:400],
                    }
                )
            return rows
        except Exception:  # noqa: BLE001
            return []

    def channel_profile_duplicate(
        self, profile_id: str
    ) -> Tuple[bool, str]:
        """Clone a profile row under '<name> copy' (no schema needed)."""
        if self.container is None:
            return False, "Database unavailable."
        try:
            db = self.container.get("database").db
            row = db.fetch_one(
                "SELECT * FROM channel_profiles WHERE id = ?",
                (str(profile_id),),
            )
            if row is None:
                return False, "Profile not found."
            data = dict(row)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            data["id"] = f"{data['id']}_copy_{stamp}"
            data["profile_name"] = f"{data.get('profile_name')} copy"
            cols = sorted(data.keys())
            db.execute(
                f"INSERT INTO channel_profiles ({', '.join(cols)})"
                f" VALUES ({', '.join('?' for _ in cols)})",
                tuple(data[c] for c in cols),
            )
            return True, f"Duplicated as '{data['profile_name']}'."
        except Exception as exc:  # noqa: BLE001
            return False, f"Duplicate failed: {exc}"

    def channel_profile_delete(
        self, profile_id: str
    ) -> Tuple[bool, str]:
        if self.container is None:
            return False, "Database unavailable."
        try:
            self.container.get("database").db.execute(
                "DELETE FROM channel_profiles WHERE id = ?",
                (str(profile_id),),
            )
            return True, "Profile deleted."
        except Exception as exc:  # noqa: BLE001
            return False, f"Delete failed: {exc}"

    def channel_profile_set_default(
        self, profile_id: str
    ) -> Tuple[bool, str]:
        if self.container is None:
            return False, "Database unavailable."
        try:
            self.container.get("config").set(
                "default_channel_profile", str(profile_id)
            )
            return True, f"Default channel profile: {profile_id}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not set default: {exc}"

    # -- engine manager + quality (Tools/Project menus) -------------------
    def engine_install_model(self) -> Dict[str, Any]:
        """Engine Install/Manager dialog rows (live-detected, honest)."""
        status = self.engines_status()
        rows = [
            {
                "name": "FFmpeg", "found": bool(status.get("ffmpeg")),
                "path": status.get("ffmpeg") or "",
                "needed_for": "video assembly + export",
            },
            {
                "name": "FFprobe", "found": bool(status.get("ffprobe")),
                "path": status.get("ffprobe") or "",
                "needed_for": "media probing + verification",
            },
            {
                "name": "Piper TTS", "found": bool(status.get("piper")),
                "path": status.get("piper") or "",
                "needed_for": "offline narration voices",
            },
        ]
        missing = [r["name"] for r in rows if not r["found"]]
        return {
            "rows": rows, "missing": missing,
            "summary": (
                "All engines installed."
                if not missing
                else "Missing: " + ", ".join(missing)
                + " — copy binaries into the engines\\ folder."
            ),
        }

    def quality_run(self, project_id: str) -> Dict[str, Any]:
        """Project -> Quality Check via the quality_checker seam."""
        rows: List[Dict[str, Any]] = []
        module = None
        if self.engine is not None:
            try:
                module_fn = getattr(self.engine, "module", None)
                if callable(module_fn):
                    module = module_fn("quality_checker")
            except Exception:  # noqa: BLE001
                module = None
        if module is None:
            return {
                "available": False, "rows": [],
                "summary": "Quality checker module unavailable.",
            }
        try:
            reply = module.run_full_check(str(project_id))
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "rows": [],
                    "summary": f"Quality check crashed: {exc}"}
        if not reply.get("success"):
            return {
                "available": False, "rows": [],
                "summary": str(reply.get("error") or "Check failed."),
            }
        data = reply.get("data") or {}
        items = (
            data.get("results") or data.get("checks")
            or (data if isinstance(data, list) else [])
        )
        for item in items or []:
            if not isinstance(item, dict):
                rows.append({"name": str(item), "status": "info",
                             "detail": ""})
                continue
            rows.append(
                {
                    "name": str(item.get("name") or item.get("check")
                              or "check"),
                    "status": str(item.get("status") or item.get("level")
                                  or "info"),
                    "detail": str(item.get("message") or item.get("detail")
                                  or ""),
                }
            )
        passed = sum(1 for r in rows if r["status"] in ("pass", "ok"))
        return {
            "available": True, "rows": rows,
            "summary": f"{passed}/{len(rows)} checks passed"
            if rows else "No checks returned.",
        }


def _pdf_escape(text: Any) -> str:
    out = str(text).encode("latin-1", errors="replace").decode(
        "latin-1")
    return out.replace("\\", r"\\").replace("(", r"\(").replace(
        ")", r"\)")


def write_storyboard_pdf(
    path: str, project_title: str, scenes: List[Dict[str, Any]]
) -> Tuple[bool, str]:
    """Stdlib-only storyboard PDF (no third-party dependency).

    A4 portrait, 5 scenes per page, 160x90 JPEG thumbnails embedded
    via DCTDecode; non-JPEG thumbs are skipped rather than faked.
    """
    page_w, page_h, per_page = 595, 842, 5
    total = max(len(scenes), 1)
    pages = [scenes[i:i + per_page]
             for i in range(0, total, per_page)] or [[]]
    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # patched after pages are built
    page_refs: List[int] = []
    for page_index, chunk in enumerate(pages):
        # Keep f-string expressions on ONE physical line: CPython 3.10/3.11
        # (the Windows target) rejects newlines inside f-string braces —
        # only 3.12+ (PEP 701) allows them. This was the 2829 crash.
        page_label = (
            f"Autopilot storyboard — page {page_index + 1}/"
            f"{len(pages)}")
        content: List[str] = [
            f"BT /F1 18 Tf 40 800 Td "
            f"({_pdf_escape(project_title)}) Tj ET",
            f"BT /F1 9 Tf 40 782 Td "
            f"({_pdf_escape(page_label)}) Tj ET",
        ]
        xobjects: List[Tuple[int, str]] = []
        y = 740.0
        for scene in chunk:
            x_text = 40
            thumb = str(scene.get("thumb_path") or "")
            src = Path(thumb) if thumb else None
            if (
                src is not None and src.is_file()
                and src.suffix.lower() in (".jpg", ".jpeg")
            ):
                try:
                    data = src.read_bytes()
                except OSError:
                    data = b""
                if data:
                    name = f"Im{len(xobjects) + 1}"
                    ref = add(
                        b"<< /Type /XObject /Subtype /Image "
                        b"/Width 1 /Height 1 /ColorSpace /DeviceRGB "
                        b"/BitsPerComponent 8 /Filter /DCTDecode "
                        b"/Length "
                        + str(len(data)).encode("ascii")
                        + b" >>\nstream\n" + data + b"\nendstream"
                    )
                    xobjects.append((ref, name))
                    content.append(
                        f"q 160 0 0 90 40 {y - 80:.1f} cm /{name} Do Q"
                    )
                    x_text = 212
            num = scene.get("number", "?")
            title = str(scene.get("title") or f"Scene {num}")
            meta = ""
            duration = scene.get("duration")
            if duration is not None:
                meta = f"{float(duration):.1f}s"
            chapter = str(scene.get("chapter") or "")
            if chapter:
                meta += (" · " if meta else "") + f"chapter: {chapter}"
            content.append(
                f"BT /F1 13 Tf {x_text} {y:.0f} Td "
                f"({_pdf_escape(f'Scene {num} — {title}')}) Tj ET")
            if meta:
                content.append(
                    f"BT /F1 10 Tf {x_text} {y - 16:.0f} Td "
                    f"({_pdf_escape(meta)}) Tj ET")
            y -= 125.0
        stream = "\n".join(content).encode("latin-1")
        contents_ref = add(
            b"<< /Length " + str(len(stream)).encode("ascii")
            + b" >>\nstream\n" + stream + b"\nendstream"
        )
        xo = " ".join(f"/{name} {ref} 0 R" for ref, name in
                      xobjects)
        xo_dict = f" /XObject << {xo} >>" if xobjects else ""
        page_ref = add(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox "
                f"[0 0 {page_w} {page_h}] /Resources << "
                f"/Font << /F1 << /Type /Font /Subtype /Type1 "
                f"/BaseFont /Helvetica >> >>{xo_dict} >> "
                f"/Contents {contents_ref} 0 R >>"
            ).encode("latin-1")
        )
        page_refs.append(page_ref)

    kids = " ".join(f"{ref} 0 R" for ref in page_refs)
    objects[1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_refs)} >>"
    ).encode("latin-1")
    out = bytearray(b"%PDF-1.4\n")
    offsets: List[int] = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    )
    out += trailer.encode("ascii")
    # PHASE 9: atomic — a truncated PDF is unopenable, and the previous
    # in-place write left exactly that behind on a full disk.
    if not atomic_write_bytes(Path(str(path)), bytes(out)):
        return False, "Could not write PDF."
    return True, f"Storyboard PDF written ({len(page_refs)} page(s))."


class UiExportsMixin:
    """Partial-workflow exports (File ▸ Export submenu, spec 35-39)."""

    def export_menu_model(self) -> List[Dict[str, str]]:
        rows = (
            ("export_audio_only", "Export &Audio Only…",
             "Narration WAV from a script — no video stages"),
            ("export_audio_mix", "Export Audio &Mix…",
             "Mix narration + music + SFX with FFmpeg"),
            ("burn_subtitles", "&Burn Subtitles to Video…",
             "Stamp an SRT file onto an existing video"),
            ("export_thumbnails", "Export &Thumbnails Only…",
             "Scaled JPG thumbnails for scenes with media"),
            ("export_storyboard_pdf", "Export Storyboard &PDF…",
             "Scene list with thumbnails as a PDF"),
        )
        return [
            {"id": rid, "label": label, "desc": desc}
            for rid, label, desc in rows
        ]

    # -- FFmpeg plumbing (RULE 4: exact command in every payload) ---
    def ffmpeg_path(self) -> Optional[str]:
        try:
            found = (self.engines_status() or {}).get("ffmpeg")
            if found:
                return str(found)
        except Exception:  # noqa: BLE001
            pass
        import shutil
        return shutil.which("ffmpeg")

    def run_ffmpeg(
        self, args: List[str], runner: Optional[Any] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        exe = self.ffmpeg_path()
        if not exe:
            return (
                False,
                "FFmpeg not found — install it or put it on PATH.",
                {"cmd": ""},
            )
        cmd = [exe, *[str(a) for a in args]]
        if runner is None:
            import subprocess

            def runner(command: List[str]) -> Tuple[int, str]:
                try:
                    proc = subprocess.run(
                        command, capture_output=True, text=True,
                        timeout=600,
                    )
                    merged = (proc.stdout or "") + (proc.stderr or "")
                    return proc.returncode, merged
                except OSError as exc:
                    return 127, str(exc)

        rc, output = runner(cmd)
        payload = {"cmd": " ".join(cmd), "log": str(output)[-2000:]}
        if int(rc) == 0:
            return True, "Export complete.", payload
        tail = str(output)[-240:]
        return False, f"FFmpeg exited with code {rc}: {tail}", payload

    # -- 35/37: audio exports ----------------------------------------
    def export_audio_only(
        self, script_text: str, out_path: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        text = str(script_text or "").strip()
        if not text:
            return False, "Paste a script to narrate.", {}
        if not str(out_path or "").strip():
            return False, "Choose an output .wav path.", {}
        module = None
        if self.engine is not None:
            try:
                module_fn = getattr(self.engine, "module", None)
                if callable(module_fn):
                    module = module_fn("tts_engine_manager")
            except Exception:  # noqa: BLE001
                module = None
        if module is not None:
            for method in ("synthesize_text", "synthesize", "speak",
                           "text_to_speech"):
                fn = getattr(module, method, None)
                if not callable(fn):
                    continue
                try:
                    result = fn(text=text, output_path=str(out_path))
                except TypeError:
                    continue  # different signature — try next seam
                except Exception as exc:  # noqa: BLE001
                    return False, f"TTS export failed: {exc}", {}
                payload = dict(result) if isinstance(result, dict) \
                    else {}
                message = str(
                    payload.get("message")
                    or f"Narration written to {out_path}")
                return True, message, payload
        return False, (
            "Standalone narration export needs the TTS module seam, "
            "which engine v1 does not expose — run a full render and "
            "find the same narration WAV in the project's audio "
            "folder."), {"module": "tts_engine_manager",
                         "available": False}

    def export_audio_mix(
        self, narration: str, music: str, sfx: str, out_path: str,
        runner: Optional[Any] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        candidates = [(str(narration or ""), "1.0"),
                      (str(music or ""), "0.35"),
                      (str(sfx or ""), "0.8")]
        sources = [(p, v) for p, v in candidates if p]
        if not sources:
            return False, "Pick at least a narration file to mix.", {}
        for src, _vol in sources:
            if not Path(src).is_file():
                return False, f"File not found: {src}", {}
        out = str(out_path or "").strip()
        if not out:
            return False, "Choose an output path (.wav or .mp3).", {}
        args: List[str] = ["-y"]
        for src, _vol in sources:
            args += ["-i", src]
        chain: List[str] = []
        labels = ""
        for index, (_src, vol) in enumerate(sources):
            chain.append(f"[{index}:a]volume={vol}[a{index}]")
            labels += f"[a{index}]"
        chain.append(
            f"{labels}amix=inputs={len(sources)}:duration=longest:"
            "dropout_transition=2[mix]")
        args += ["-filter_complex", ";".join(chain), "-map", "[mix]"]
        if out.lower().endswith(".mp3"):
            args += ["-c:a", "libmp3lame", "-q:a", "3"]
        else:
            args += ["-c:a", "pcm_s16le"]
        args.append(out)
        return self.run_ffmpeg(args, runner=runner)

    # -- 36: burn subtitles -------------------------------------------
    @staticmethod
    def _srt_filter_path(srt: str) -> str:
        # The subtitles filter needs Windows path escaping of its own.
        return (str(srt).replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\\'"))

    def burn_subtitles(
        self, video: str, srt: str, out_path: str,
        runner: Optional[Any] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if not Path(str(video)).is_file():
            return False, f"Video not found: {video}", {}
        if not Path(str(srt)).is_file():
            return False, f"Subtitle file not found: {srt}", {}
        out = str(out_path or "").strip()
        if not out:
            return False, "Choose an output .mp4 path.", {}
        # v3.0: burns honour the Subtitle Designer style and the
        # Export page codec profile — real consumption of both.
        style = self.subtitle_force_style()
        export = self.export_settings_model()
        vf_text = f"subtitles='{self._srt_filter_path(srt)}'"
        if style:
            vf_text += f":force_style='{style}'"
        args = [
            "-y", "-i", str(video), "-vf", vf_text,
            "-c:v", str(export.get("export_codec") or "libx264"),
            "-crf", str(export.get("export_crf") or 20),
            "-preset", str(export.get("export_preset") or "medium"),
            "-c:a", "copy", out,
        ]
        return self.run_ffmpeg(args, runner=runner)

    # -- 38: thumbnails (brains build jobs; the shell scales images) --
    def export_thumbnail_jobs(
        self, project_id: str, out_dir: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if not str(out_dir or "").strip():
            return False, "Choose an output folder.", {}
        model = self.timeline_model(project_id)
        if not model.get("found"):
            return False, (
                "No scenes for this project yet — run a render "
                "first."), {}
        jobs: List[Dict[str, Any]] = []
        for scene in model.get("scenes", []):
            src = str(scene.get("media_path")
                      or scene.get("thumb_path") or "")
            if src and Path(src).is_file():
                try:
                    number = int(scene.get("number"))
                except (TypeError, ValueError):
                    continue
                jobs.append({
                    "scene": number,
                    "src": src,
                    "dst": str(Path(str(out_dir))
                               / f"scene_{number:03d}.jpg"),
                })
        if not jobs:
            return False, (
                "Scenes have no image files to thumbnail "
                "(placeholders are not exported)."), {}
        return True, f"{len(jobs)} thumbnail job(s) ready.", {
            "jobs": jobs, "out_dir": str(out_dir)}

    # -- 39: storyboard PDF --------------------------------------------
    def export_storyboard_pdf(
        self, project_id: str, pdf_path: str
    ) -> Tuple[bool, str]:
        out = str(pdf_path or "").strip()
        if not out:
            return False, "Choose an output .pdf path."
        model = self.timeline_model(project_id)
        if not model.get("found") or not model.get("scenes"):
            return False, (
                "No scenes for this project yet — run a render "
                "first.")
        title = str(
            model.get("project_title") or model.get("title")
            or project_id or "Storyboard")
        return write_storyboard_pdf(out, title, model["scenes"])


# ---------------------------------------------------------------------------
# v3.0 master spec: control-panel brains (voice / transitions / export /
# subtitle / scene), workspaces and waveform peaks — all Qt-free.
# ---------------------------------------------------------------------------
VOICE_EMOTIONS: Tuple[str, ...] = (
    "Neutral", "Happy", "Sad", "Excited", "Calm", "Serious", "Whisper",
)
VOICE_REVERBS: Tuple[str, ...] = (
    "Off", "Small Room", "Large Hall", "Cathedral",
)
VOICE_DEFAULTS: Dict[str, Any] = {
    "voice_engine": "auto",
    "voice_name": "",
    "voice_speed": 1.0,
    "voice_pitch_st": 0,
    "voice_volume": 100,
    "voice_emotion": "Neutral",
    "voice_reverb": "Off",
    "voice_reverb_amount": 40,
    "voice_breathing": False,
    "voice_breath_volume": 30,
    "voice_pause_comma_ms": 250,
    "voice_pause_sentence_ms": 500,
    "voice_pause_paragraph_ms": 900,
    "voice_pause_chapter_ms": 1400,
    "voice_pronunciation": "",
    "voice_lock": False,
}
FALLBACK_TRANSITIONS: Tuple[Dict[str, Any], ...] = (
    {"id": "fade", "label": "Fade", "duration": 0.8},
    {"id": "crossfade", "label": "Crossfade", "duration": 0.8},
    {"id": "dissolve", "label": "Dissolve", "duration": 1.0},
    {"id": "dip_black", "label": "Dip to Black", "duration": 0.7},
    {"id": "dip_white", "label": "Dip to White", "duration": 0.7},
    {"id": "blur", "label": "Blur", "duration": 0.6},
    {"id": "zoom", "label": "Zoom", "duration": 0.7},
    {"id": "slide_left", "label": "Slide Left", "duration": 0.6},
    {"id": "slide_right", "label": "Slide Right", "duration": 0.6},
    {"id": "push", "label": "Push", "duration": 0.6},
    {"id": "wipe", "label": "Wipe", "duration": 0.6},
)
SUBTITLE_FONTS: Tuple[str, ...] = (
    "Montserrat", "Arial", "Roboto", "Open Sans", "Lato", "Oswald",
    "DejaVu Sans", "Liberation Sans",
)
SUBTITLE_WEIGHTS: Tuple[str, ...] = ("Regular", "Bold", "Light")
SUBTITLE_POSITIONS: Tuple[Tuple[str, int], ...] = (
    ("Bottom", 2), ("Middle", 5), ("Top", 8),
)
SUBTITLE_ANIMATIONS: Tuple[str, ...] = (
    "None", "Fade", "Pop", "Word Highlight",
)
SUBTITLE_DEFAULTS: Dict[str, Any] = {
    "subtitle_font": "Montserrat",
    "subtitle_size": 54,
    "subtitle_weight": "Bold",
    "subtitle_color": "#FFFFFF",
    "subtitle_outline_color": "#000000",
    "subtitle_outline": 3,
    "subtitle_shadow": 1,
    "subtitle_background": False,
    "subtitle_back_color": "#000000",
    "subtitle_back_opacity": 50,
    "subtitle_position": "Bottom",
    "subtitle_margin_v": 40,
    "subtitle_word_highlight": False,
    "subtitle_animation": "None",
    "subtitle_apply_burn": True,
}
EXPORT_RESOLUTIONS: Tuple[str, ...] = (
    "3840x2160", "2560x1440", "1920x1080", "1280x720", "1080x1920",
    "Custom",
)
EXPORT_FPS: Tuple[str, ...] = ("24", "25", "30", "50", "60")
EXPORT_CODECS: Tuple[Tuple[str, str], ...] = (
    ("libx264", "H.264 / AVC (MP4)"),
    ("libx265", "H.265 / HEVC (MP4)"),
    ("mpeg4", "MPEG-4 Part 2"),
    ("libvpx-vp9", "VP9 (WebM)"),
)
EXPORT_PRESETS: Tuple[str, ...] = (
    "ultrafast", "veryfast", "fast", "medium", "slow", "veryslow",
)
EXPORT_AUDIO_CODECS: Tuple[Tuple[str, str], ...] = (
    ("aac", "AAC"), ("libmp3lame", "MP3"),
    ("libopus", "Opus"), ("pcm_s16le", "PCM (WAV)"),
)
EXPORT_SAMPLE_RATES: Tuple[str, ...] = ("44100", "48000")
EXPORT_CHANNELS: Tuple[str, ...] = ("1", "2")
EXPORT_BITRATES: Tuple[str, ...] = ("128k", "192k", "256k", "320k")
EXPORT_DEFAULTS: Dict[str, Any] = {
    "export_resolution": "1920x1080",
    "export_width": 1920,
    "export_height": 1080,
    "export_fps": "30",
    "export_codec": "libx264",
    "export_crf": 20,
    "export_preset": "medium",
    "export_audio_codec": "aac",
    "export_audio_bitrate": "192k",
    "export_sample_rate": "48000",
    "export_channels": "2",
    "export_folder": "",
    "export_naming": "{project}_{date}",
    "export_open_folder": True,
    "export_play_after": False,
}
# Workspace name -> layout recipe: page = _NAV key in ui/app.py
# ("" = stay), inspector = dock visibility (None = leave as-is).
WORKSPACES: Dict[str, Dict[str, Any]] = {
    "Writing": {"page": "render", "inspector": True},
    "Editing": {"page": "studio", "inspector": True},
    "Voice": {"page": "voice", "inspector": False},
    "Color": {"page": "grade", "inspector": True},
    "Audio": {"page": "audio", "inspector": True},
    "Subtitles": {"page": "subtitles", "inspector": False},
    "Effects": {"page": "transitions", "inspector": True},
    "Rendering": {"page": "batch", "inspector": False},
    "Minimal": {"page": "render", "inspector": False},
    "Custom": {"page": "", "inspector": None},
}


class UiV3Mixin:
    """Brains behind the v3.0 pages — pure Python, panels only paint."""

    # -- shared settings access (RULE 3: settings own app prefs) ------
    def _v3_config(self) -> Optional[Any]:
        if self.container is None:
            return None
        try:
            return self.container.get("config")
        except Exception:  # noqa: BLE001
            return None

    def _v3_get(self, key: str, default: Any) -> Any:
        config = self._v3_config()
        if config is None:
            return default
        try:
            return config.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    def _v3_save(self, values: Dict[str, Any]) -> bool:
        config = self._v3_config()
        if config is None:
            return False
        for key, value in values.items():
            config.set(key, value)
        return True

    # -- 1. voice controls ---------------------------------------------
    # -- pronunciation dictionary manager (v3.2.13) ---------------------
    def pronunciation_presets(self) -> List[Dict[str, str]]:
        """Bundled starter dictionaries (Bible names, common acronyms)."""
        try:
            preset_dir = Path(__file__).resolve().parent.parent / "config" / "pronunciation"
        except Exception:  # noqa: BLE001
            return []
        presets = []
        if preset_dir.is_dir():
            for f in sorted(preset_dir.glob("*.json")):
                presets.append({
                    "id": f.stem,
                    "label": f.stem.replace("_", " ").title(),
                    "path": str(f),
                })
        return presets

    def load_pronunciation_entries(
        self, path: str
    ) -> List[Dict[str, str]]:
        """Word/pronunciation pairs from a dictionary file, sorted by word."""
        if not path:
            return []
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return []
            return [
                {"word": str(k), "pronunciation": str(v)}
                for k, v in sorted(data.items(), key=lambda kv: kv[0].lower())
            ]
        except (OSError, json.JSONDecodeError, ValueError):
            return []

    def save_pronunciation_entries(
        self, path: str, entries: List[Dict[str, str]]
    ) -> Tuple[bool, str]:
        """Write entries to `path` as JSON; empty words/pronunciations dropped.

        Does NOT change the active voice_pronunciation setting — the
        caller (dialog) decides whether to also point the app at this
        file, so "Save As" doesn't silently switch what's active.
        """
        if not path:
            return False, "Choose a file location first."
        clean = {
            str(e.get("word") or "").strip(): str(e.get("pronunciation") or "").strip()
            for e in entries
            if str(e.get("word") or "").strip()
            and str(e.get("pronunciation") or "").strip()
        }
        target = Path(path)
        # PHASE 9: atomic — an interrupted save used to leave a
        # half-written dictionary that the TTS stage then silently
        # ignored (load_pronunciation_dict swallows parse errors), so
        # the user's edits appeared to apply but never did.
        if not atomic_write_json(target, clean):
            return False, "Could not save the pronunciation file."
        return True, f"Saved {len(clean)} entries to {target.name}."

    def voice_controls_model(self) -> Dict[str, Any]:
        model = dict(VOICE_DEFAULTS)
        for key in model:
            model[key] = self._v3_get(key, model[key])
        model["speed_percent"] = int(clamp(
            float(model["voice_speed"]) * 100, 50, 200, 100))
        model["emotions"] = list(VOICE_EMOTIONS)
        model["reverbs"] = list(VOICE_REVERBS)
        model["engines"] = ["auto"] + self._voice_engine_names()
        model["presets"] = sorted(self._voice_presets())
        model["sample_text"] = (
            "This is Autopilot. Your documentary narration will "
            "sound like this.")
        if model["voice_emotion"] not in VOICE_EMOTIONS:
            model["voice_emotion"] = "Neutral"
        if model["voice_reverb"] not in VOICE_REVERBS:
            model["voice_reverb"] = "Off"
        return model

    def _voice_engine_names(self) -> List[str]:
        names: List[str] = []
        try:
            status = self.engines_status() or {}
            pool: Any = status
            if isinstance(status, dict):
                pool = status.get("engines", status)
            if isinstance(pool, dict):
                names = [str(k) for k in pool if k != "ffmpeg"]
            elif isinstance(pool, (list, tuple)):
                names = [str(x) for x in pool]
        except Exception:  # noqa: BLE001
            names = []
        return names

    def _voice_presets(self) -> Dict[str, Any]:
        raw = str(self._v3_get("voice_presets_json", "") or "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}  # RULE 8: corrupt JSON never breaks the panel
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _validate_voice_state(
        state: Dict[str, Any]
    ) -> Tuple[bool, str]:
        try:
            speed = float(state.get("voice_speed"))
        except (TypeError, ValueError):
            return False, "Speed must stay between 0.5x and 2.0x."
        if speed < 0.5 or speed > 2.0:
            return False, "Speed must stay between 0.5x and 2.0x."
        emotion = str(state.get("voice_emotion") or "Neutral")
        if emotion not in VOICE_EMOTIONS:
            return False, f"Unknown emotion preset: {emotion}"
        reverb = str(state.get("voice_reverb") or "Off")
        if reverb not in VOICE_REVERBS:
            return False, f"Unknown reverb type: {reverb}"
        return True, ""

    def save_voice_controls(
        self, state: Dict[str, Any]
    ) -> Tuple[bool, str]:
        if self._v3_config() is None:
            return False, "Settings store unavailable."
        ok, message = self._validate_voice_state(state)
        if not ok:
            return False, message
        values: Dict[str, Any] = {
            "voice_engine": str(state.get("voice_engine") or "auto"),
            "voice_name": str(state.get("voice_name") or ""),
            "voice_speed": round(clamp(
                state.get("voice_speed"), 0.5, 2.0, 1.0), 2),
            "voice_pitch_st": int(clamp(
                state.get("voice_pitch_st"), -6, 6, 0)),
            "voice_volume": int(clamp(
                state.get("voice_volume"), 0, 100, 100)),
            "voice_emotion": str(state.get("voice_emotion")),
            "voice_reverb": str(state.get("voice_reverb")),
            "voice_reverb_amount": int(clamp(
                state.get("voice_reverb_amount"), 0, 100, 40)),
            "voice_breathing": bool(state.get("voice_breathing")),
            "voice_breath_volume": int(clamp(
                state.get("voice_breath_volume"), 0, 100, 30)),
            "voice_pronunciation": str(
                state.get("voice_pronunciation") or ""),
            "voice_lock": bool(state.get("voice_lock")),
        }
        for key in ("comma", "sentence", "paragraph", "chapter"):
            values[f"voice_pause_{key}_ms"] = int(clamp(
                state.get(f"voice_pause_{key}_ms"), 0, 5000,
                VOICE_DEFAULTS[f"voice_pause_{key}_ms"]))
        self._v3_save(values)
        return True, ("Voice profile saved — applies to the next "
                      "preview and narration pass.")

    def save_voice_preset(
        self, name: str, state: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        name = str(name or "").strip()
        if not name:
            return False, "Name the preset first."
        if len(name) > 40:
            return False, "Preset names stay under 40 characters."
        if self._v3_config() is None:
            return False, "Settings store unavailable."
        source = dict(state) if isinstance(state, dict) \
            else self.voice_controls_model()
        snapshot = {
            key: source.get(key, default)
            for key, default in VOICE_DEFAULTS.items()
        }
        presets = self._voice_presets()
        presets[name] = snapshot
        self._v3_save({
            "voice_presets_json": json.dumps(presets, sort_keys=True),
        })
        return True, f"Voice preset '{name}' saved."

    def apply_voice_preset(
        self, name: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        preset = self._voice_presets().get(str(name or ""))
        if not isinstance(preset, dict):
            return False, f"No preset named '{name}'.", {}
        merged = dict(VOICE_DEFAULTS)
        merged.update(preset)
        return True, f"Preset '{name}' loaded.", merged

    def delete_voice_preset(self, name: str) -> Tuple[bool, str]:
        presets = self._voice_presets()
        if str(name or "") not in presets:
            return False, f"No preset named '{name}'."
        if self._v3_config() is None:
            return False, "Settings store unavailable."
        del presets[str(name)]
        self._v3_save({
            "voice_presets_json": json.dumps(presets, sort_keys=True),
        })
        return True, f"Preset '{name}' deleted."

    # -- 17. voice preview ---------------------------------------------
    def preview_voice(
        self, text: str = ""
    ) -> Tuple[bool, str, Dict[str, Any]]:
        sample = str(text or "").strip() or (
            "This is Autopilot. Your documentary will sound like this."
        )
        model = self.voice_controls_model()
        out_path = str(
            Path(tempfile.gettempdir()) / "autopilot_voice_preview.wav")
        params = {
            "speed": float(model["voice_speed"]),
            "pitch_st": int(model["voice_pitch_st"]),
            "volume": int(model["voice_volume"]),
            "emotion": str(model["voice_emotion"]),
            "reverb": str(model["voice_reverb"]),
        }
        module = None
        if self.engine is not None:
            try:
                module_fn = getattr(self.engine, "module", None)
                if callable(module_fn):
                    module = module_fn("tts_engine_manager")
            except Exception:  # noqa: BLE001
                module = None
        if module is not None:
            for method in ("synthesize_text", "synthesize", "speak",
                           "text_to_speech"):
                fn = getattr(module, method, None)
                if not callable(fn):
                    continue
                try:
                    result = fn(
                        text=sample, output_path=out_path, **params)
                except TypeError:
                    try:
                        result = fn(text=sample, output_path=out_path)
                    except TypeError:
                        continue  # different signature — next seam
                    except Exception as exc:  # noqa: BLE001
                        return False, \
                            f"Voice preview failed: {exc}", {}
                except Exception as exc:  # noqa: BLE001
                    return False, f"Voice preview failed: {exc}", {}
                payload = dict(result) if isinstance(result, dict) \
                    else {}
                payload["path"] = str(payload.get("path") or out_path)
                message = str(
                    payload.get("message")
                    or f"Preview written to {payload['path']}")
                return True, message, payload
        return False, (
            "Voice preview needs the TTS module seam, which engine "
            "v1 does not expose — the saved profile automatically "
            "applies the moment a TTS engine offers a preview hook."
        ), {"module": "tts_engine_manager", "available": False}

    # -- 2. transitions (writes existing scene columns) ----------------
    def _scene_rows_all(
        self, project_id: str
    ) -> List[Dict[str, Any]]:
        if self.container is None \
                or not str(project_id or "").strip():
            return []
        try:
            rows = self.container.get("database").db.fetch_all(
                "SELECT * FROM scenes WHERE project_id = ?"
                " ORDER BY scene_number", (str(project_id),))
            return [dict(r) for r in rows or []]
        except Exception:  # noqa: BLE001
            return []

    def transitions_model(self, project_id: str) -> Dict[str, Any]:
        scenes = []
        for row in self._scene_rows_all(project_id):
            scenes.append({
                "number": row.get("scene_number"),
                "title": str(row.get("scene_title") or ""),
                "duration": row.get("duration"),
                "transition_in": str(row.get("transition_in") or ""),
                "transition_out": str(row.get("transition_out") or ""),
                "transition_duration":
                    row.get("transition_duration") or 0.8,
            })
        return {
            "found": bool(scenes),
            "project_id": str(project_id or ""),
            "scenes": scenes,
            "types": self.transition_options(),
            "empty_text": (
                "No scenes yet — run a render, then dress the cuts "
                "here."),
        }

    def apply_transition(
        self, project_id: str, numbers: Any, type_id: str,
        duration: Any = None, apply_all: bool = False,
    ) -> Tuple[bool, str]:
        type_id = str(type_id or "fade")
        known = {t["id"]: t for t in self.transition_options()}
        if type_id not in known:
            return False, f"Unknown transition type: {type_id}"
        if duration is None:
            duration = known[type_id]["duration"]
        targets: List[int] = []
        if apply_all:
            targets = [
                int(r["scene_number"])
                for r in self._scene_rows_all(project_id)
                if r.get("scene_number") is not None
            ]
            if not targets:
                return False, "No scenes yet — run a render first."
        else:
            for raw in numbers or []:
                try:
                    targets.append(int(raw))
                except (TypeError, ValueError):
                    continue
            if not targets:
                return False, "Select at least one scene first."
        done = 0
        for number in targets:
            ok, _msg = self.apply_scene_transition(
                project_id, number, type_id, type_id, duration)
            done += int(ok)
        if not done:
            return False, "Database unavailable — transition not saved."
        label = known[type_id]["label"]
        return True, f"{label} applied to {done} scene(s)."

    # -- 4. scene controls (animation / intensity / duration) ----------
    def scene_controls_model(self, project_id: str) -> Dict[str, Any]:
        scenes = []
        for row in self._scene_rows_all(project_id):
            scenes.append({
                "number": row.get("scene_number"),
                "title": str(row.get("scene_title") or ""),
                "duration": row.get("duration"),
                "animation": str(row.get("animation_type") or ""),
                "intensity": str(row.get("animation_intensity") or ""),
                "transition_in": str(row.get("transition_in") or ""),
            })
        options = self.animation_options()
        return {
            "found": bool(scenes),
            "project_id": str(project_id or ""),
            "scenes": scenes,
            "animations": list(options.get("animations") or []),
            "intensities": list(options.get("intensities") or []),
            "empty_text": (
                "No scenes yet — run a render, then shape motion "
                "here."),
        }

    def apply_scene_animation_all(
        self, project_id: str, animation: str, intensity: str
    ) -> Tuple[bool, str]:
        rows = [
            r for r in self._scene_rows_all(project_id)
            if r.get("scene_number") is not None
        ]
        if not rows:
            return False, "No scenes yet — run a render first."
        done = 0
        for row in rows:
            ok, _msg = self.apply_scene_animation(
                project_id, row["scene_number"], animation, intensity)
            done += int(ok)
        if not done:
            return False, "Database unavailable — nothing saved."
        return True, f"Animation applied to {done} scene(s)."

    def apply_scene_duration(
        self, project_id: str, scene_number: Any, seconds: Any
    ) -> Tuple[bool, str]:
        if not scene_number:
            return False, ("Durations are per scene — pick a scene "
                           "first.")
        value = round(clamp(seconds, 0.5, 120.0, 4.0), 2)
        return self._update_scene_or_default(
            project_id, scene_number, {"duration": value}, {},
            f"duration={value}s")

    # -- 3. export settings (default export profile) -------------------
    def export_summary_text(
        self, state: Optional[Dict[str, Any]] = None
    ) -> str:
        model = dict(EXPORT_DEFAULTS)
        if state:
            for key in EXPORT_DEFAULTS:
                if key in state:
                    model[key] = state[key]
        resolution = str(model.get("export_resolution") or "")
        label = {
            "3840x2160": "4K", "2560x1440": "1440p",
            "1920x1080": "1080p", "1280x720": "720p",
            "1080x1920": "Vertical",
        }.get(
            resolution,
            f"{model.get('export_width')}x{model.get('export_height')}")
        codec = dict(EXPORT_CODECS).get(
            str(model.get("export_codec")), "H.264")
        audio = dict(EXPORT_AUDIO_CODECS).get(
            str(model.get("export_audio_codec")), "AAC")
        return (f"{label} · {model.get('export_fps')} fps · "
                f"{codec.split(' ')[0]} · CRF "
                f"{model.get('export_crf')} · {audio} "
                f"{model.get('export_audio_bitrate')}")

    def export_settings_model(self) -> Dict[str, Any]:
        model = dict(EXPORT_DEFAULTS)
        for key in model:
            model[key] = self._v3_get(key, model[key])
        model.update({
            "resolutions": list(EXPORT_RESOLUTIONS),
            "fps_values": list(EXPORT_FPS),
            "codecs": [
                {"id": cid, "label": label}
                for cid, label in EXPORT_CODECS
            ],
            "presets": list(EXPORT_PRESETS),
            "audio_codecs": [
                {"id": cid, "label": label}
                for cid, label in EXPORT_AUDIO_CODECS
            ],
            "sample_rates": list(EXPORT_SAMPLE_RATES),
            "channels": list(EXPORT_CHANNELS),
            "bitrates": list(EXPORT_BITRATES),
            "summary_text": self.export_summary_text(model),
        })
        return model

    def save_export_settings(
        self, state: Dict[str, Any]
    ) -> Tuple[bool, str]:
        if self._v3_config() is None:
            return False, "Settings store unavailable."
        resolution = str(
            state.get("export_resolution") or
            EXPORT_DEFAULTS["export_resolution"])
        if resolution not in EXPORT_RESOLUTIONS:
            return False, f"Unknown resolution: {resolution}"
        fps = str(state.get("export_fps") or "")
        if fps not in EXPORT_FPS:
            return False, "FPS must be one of " \
                + "/".join(EXPORT_FPS) + "."
        codec = str(state.get("export_codec") or "")
        if codec not in dict(EXPORT_CODECS):
            return False, f"Unknown video codec: {codec}"
        preset = str(state.get("export_preset") or "")
        if preset not in EXPORT_PRESETS:
            return False, f"Unknown encoder preset: {preset}"
        audio = str(state.get("export_audio_codec") or "")
        if audio not in dict(EXPORT_AUDIO_CODECS):
            return False, f"Unknown audio codec: {audio}"
        rate = str(state.get("export_sample_rate") or "")
        if rate not in EXPORT_SAMPLE_RATES:
            return False, f"Unknown sample rate: {rate}"
        channels = str(state.get("export_channels") or "")
        if channels not in EXPORT_CHANNELS:
            return False, "Channels must be 1 (mono) or 2 (stereo)."
        bitrate = str(state.get("export_audio_bitrate") or "")
        if bitrate not in EXPORT_BITRATES:
            return False, f"Unknown audio bitrate: {bitrate}"
        self._v3_save({
            "export_resolution": resolution,
            "export_width": int(clamp(
                state.get("export_width"), 16, 7680, 1920)),
            "export_height": int(clamp(
                state.get("export_height"), 16, 4320, 1080)),
            "export_fps": fps,
            "export_codec": codec,
            # 3.0.7: full codec CRF domain (Export panel spinbox)
            "export_crf": int(clamp(
                state.get("export_crf"), 0, 51, 20)),
            "export_preset": preset,
            "export_audio_codec": audio,
            "export_audio_bitrate": bitrate,
            "export_sample_rate": rate,
            "export_channels": channels,
            "export_folder": str(state.get("export_folder") or ""),
            "export_naming": str(
                state.get("export_naming") or "{project}_{date}"),
            "export_open_folder": bool(
                state.get("export_open_folder")),
            "export_play_after": bool(
                state.get("export_play_after")),
        })
        return True, ("Export settings saved — re-encode exports "
                      "(like Burn Subtitles) use them right away.")

    # -- 7. subtitle designer (style once, burn everywhere) ------------
    def subtitle_style_model(self) -> Dict[str, Any]:
        model = dict(SUBTITLE_DEFAULTS)
        for key in model:
            model[key] = self._v3_get(key, model[key])
        model.update({
            "fonts": list(SUBTITLE_FONTS),
            "weights": list(SUBTITLE_WEIGHTS),
            "positions": [p for p, _a in SUBTITLE_POSITIONS],
            "animations": list(SUBTITLE_ANIMATIONS),
            "preview_text": "Autopilot subtitles look like this.",
        })
        if model["subtitle_position"] not in model["positions"]:
            model["subtitle_position"] = "Bottom"
        if model["subtitle_weight"] not in SUBTITLE_WEIGHTS:
            model["subtitle_weight"] = "Bold"
        if model["subtitle_animation"] not in SUBTITLE_ANIMATIONS:
            model["subtitle_animation"] = "None"
        return model

    @staticmethod
    def _clean_hex(value: Any) -> Optional[str]:
        text = str(value or "").strip().upper()
        if text.startswith("#"):
            text = text[1:]
        if len(text) != 6:
            return None
        try:
            int(text, 16)
        except ValueError:
            return None
        return f"#{text}"

    @staticmethod
    def _ass_colour(value: Any, opacity: Any = 100) -> str:
        text = str(value or "").strip().lstrip("#")
        try:
            red = int(text[0:2], 16)
            green = int(text[2:4], 16)
            blue = int(text[4:6], 16)
        except (ValueError, IndexError):
            red, green, blue = 255, 255, 255
        alpha = int(round(
            255 * (100 - clamp(opacity, 0, 100, 100)) / 100))
        return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"

    def _validate_subtitle_state(
        self, state: Dict[str, Any]
    ) -> Tuple[bool, str]:
        for key, label in (
            ("subtitle_color", "Text colour"),
            ("subtitle_outline_color", "Outline colour"),
            ("subtitle_back_color", "Background colour"),
        ):
            if self._clean_hex(state.get(key)) is None:
                return False, f"{label} must look like #RRGGBB."
        positions = [p for p, _a in SUBTITLE_POSITIONS]
        position = str(state.get("subtitle_position") or "Bottom")
        if position not in positions:
            return False, f"Unknown position: {position}"
        weight = str(state.get("subtitle_weight") or "Bold")
        if weight not in SUBTITLE_WEIGHTS:
            return False, f"Unknown weight: {weight}"
        animation = str(state.get("subtitle_animation") or "None")
        if animation not in SUBTITLE_ANIMATIONS:
            return False, f"Unknown animation: {animation}"
        return True, ""

    def save_subtitle_style(
        self, state: Dict[str, Any]
    ) -> Tuple[bool, str]:
        if self._v3_config() is None:
            return False, "Settings store unavailable."
        ok, message = self._validate_subtitle_state(state)
        if not ok:
            return False, message
        self._v3_save({
            "subtitle_font": str(
                state.get("subtitle_font") or "Montserrat")[:60],
            "subtitle_size": int(clamp(
                state.get("subtitle_size"), 12, 120, 54)),
            "subtitle_weight": str(state.get("subtitle_weight")),
            "subtitle_color": self._clean_hex(
                state.get("subtitle_color")),
            "subtitle_outline_color": self._clean_hex(
                state.get("subtitle_outline_color")),
            "subtitle_outline": int(clamp(
                state.get("subtitle_outline"), 0, 8, 3)),
            "subtitle_shadow": int(clamp(
                state.get("subtitle_shadow"), 0, 6, 1)),
            "subtitle_background": bool(
                state.get("subtitle_background")),
            "subtitle_back_color": self._clean_hex(
                state.get("subtitle_back_color")),
            "subtitle_back_opacity": int(clamp(
                state.get("subtitle_back_opacity"), 0, 100, 50)),
            "subtitle_position": str(state.get("subtitle_position")),
            "subtitle_margin_v": int(clamp(
                state.get("subtitle_margin_v"), 0, 400, 40)),
            "subtitle_word_highlight": bool(
                state.get("subtitle_word_highlight")),
            "subtitle_animation": str(
                state.get("subtitle_animation")),
            "subtitle_apply_burn": bool(
                state.get("subtitle_apply_burn")),
        })
        return True, ("Subtitle style saved — File ▸ Export → Burn "
                      "Subtitles picks it up immediately.")

    def subtitle_force_style(self) -> str:
        """ASS force_style for the subtitles filter ("" = passthrough)."""
        model = self.subtitle_style_model()
        if not bool(model.get("subtitle_apply_burn")):
            return ""
        font = str(model["subtitle_font"])
        for char in (",", "'", '"', ":"):
            font = font.replace(char, "")
        alignment = dict(SUBTITLE_POSITIONS).get(
            str(model["subtitle_position"]), 2)
        bold = int(str(model["subtitle_weight"]) == "Bold")
        border = 3 if model["subtitle_background"] else 1
        back = self._ass_colour(
            model["subtitle_back_color"], model["subtitle_back_opacity"])
        parts = [
            f"FontName={font or 'Montserrat'}",
            f"FontSize={int(clamp(model['subtitle_size'], 12, 120, 54))}",
            "PrimaryColour="
            f"{self._ass_colour(model['subtitle_color'])}",
            "OutlineColour="
            f"{self._ass_colour(model['subtitle_outline_color'])}",
            f"BackColour={back}",
            f"Bold={bold}",
            f"BorderStyle={border}",
            f"Outline={int(clamp(model['subtitle_outline'], 0, 8, 3))}",
            f"Shadow={int(clamp(model['subtitle_shadow'], 0, 6, 1))}",
            f"Alignment={alignment}",
            f"MarginV={int(clamp(model['subtitle_margin_v'], 0, 400, 40))}",
        ]
        return ",".join(parts)

    # -- 18. waveform peaks (stdlib PCM reader for the Audio page) -----
    def waveform_peaks(
        self, path: str, buckets: int = 120
    ) -> Tuple[bool, str, List[float]]:
        target = Path(str(path or ""))
        if not target.is_file():
            return False, f"Audio file not found: {path}", []
        try:
            with wave.open(str(target), "rb") as handle:
                channels = max(1, handle.getnchannels())
                width = handle.getsampwidth()
                total = handle.getnframes()
                raw = handle.readframes(total)
        except (wave.Error, EOFError, OSError) as exc:
            return False, f"Cannot read waveform: {exc}", []
        if total <= 0:
            return False, "Audio file is empty.", []
        samples: List[int] = []
        if width == 1:
            samples = [b - 128 for b in raw[::channels]]
            scale = 128.0
        elif width == 2:
            count = total * channels
            samples = list(
                struct.unpack(f"<{count}h", raw[: count * 2]))
            samples = samples[::channels]
            scale = 32768.0
        elif width == 4:
            count = total * channels
            samples = list(
                struct.unpack(f"<{count}i", raw[: count * 4]))
            samples = samples[::channels]
            scale = 2147483648.0
        else:
            return False, (
                f"Unsupported sample width: {width} byte(s) — "
                "PCM WAV only."), []
        limit = int(clamp(buckets, 8, 960, 120))
        per = max(1, len(samples) // limit)
        peaks: List[float] = []
        for start in range(0, len(samples), per):
            chunk = samples[start:start + per]
            if not chunk:
                break
            peak = max(abs(min(chunk)), abs(max(chunk)))
            peaks.append(round(min(1.0, peak / scale), 3))
            if len(peaks) >= limit:
                break
        return True, f"{len(peaks)} peaks", peaks

    # -- 16. workspaces (saved layout recipes) --------------------------
    def workspace_model(self) -> Dict[str, Any]:
        current = str(
            self._v3_get("workspace_current", "Writing") or "Writing")
        if current not in WORKSPACES:
            current = "Writing"
        return {
            "current": current,
            "names": list(WORKSPACES),
            "layouts": {key: dict(value)
                        for key, value in WORKSPACES.items()},
        }

    @staticmethod
    def workspace_layout(name: str) -> Dict[str, Any]:
        return dict(WORKSPACES.get(str(name or ""))
                    or {"page": "", "inspector": None})

    def set_workspace(self, name: str) -> Tuple[bool, str]:
        name = str(name or "")
        if name not in WORKSPACES:
            return False, f"Unknown workspace: {name or '(blank)'}"
        if self._v3_config() is None:
            return False, "Settings store unavailable."
        self._v3_save({"workspace_current": name})
        return True, f"Workspace: {name}"


class UiViewModel(UiChromeMixin, UiStudioMixin, UiDialogsMixin,
                  UiPanelsMixin, UiExportsMixin, UiV3Mixin):
    """Bridges the boot ctx {container, engine, license} to the shell."""

    def __init__(self, ctx: Dict[str, Any]) -> None:
        self.ctx = ctx
        self.engine = ctx.get("engine")
        self.container = ctx.get("container")
        self.license_manager = ctx.get("license")
        self.scene_clipboard: Optional[Dict[str, Any]] = None
        self._undo_history: list = []
        self._redo_history: list = []

    # ------------------------------------------------------------------
    # Window chrome / status
    # ------------------------------------------------------------------
    def window_title(self) -> str:
        status = self.license_summary()["status"]
        return f"Autopilot — Documentary Video Automation (license: {status})"

    def engine_ready(self) -> bool:
        return self.engine is not None

    def module_count(self) -> int:
        if self.engine is None:
            return 0
        try:
            data = self.engine.get_module_status().get("data") or {}
            return len(data.get("loaded_modules") or [])
        except Exception:  # noqa: BLE001 - UI must never crash on this
            return 0

    def stage_names(self) -> List[str]:
        engine = self.engine
        if engine is None:
            return []
        try:
            return list(engine.stage_names())
        except Exception:  # noqa: BLE001
            return []

    def license_summary(self) -> Dict[str, Any]:
        """{status, days_remaining, message} — ctx first, manager refresh."""
        status: Dict[str, Any] = {}
        data = self.ctx.get("license_data")
        if isinstance(data, dict):
            raw = data.get("status")
            status = raw if isinstance(raw, dict) else {"status": raw}
        if not status and self.license_manager is not None:
            try:
                resp = self.license_manager.check_license()
                if resp.get("success"):
                    status = resp.get("data") or {}
            except Exception:  # noqa: BLE001 - license UI is advisory
                status = {}
        state = str(status.get("status") or "unknown")
        summary: Dict[str, Any] = {"status": state, "days_remaining": None}
        if state == "trial":
            days = status.get("days_remaining")
            if days is None and self.license_manager is not None:
                try:
                    days = self.license_manager.get_days_remaining()
                except Exception:  # noqa: BLE001
                    days = None
            summary["days_remaining"] = days
            summary["message"] = (
                f"Trial — {days} day(s) left"
                if days is not None
                else "Trial license"
            )
        elif state == "active":
            summary["message"] = "Licensed — thank you!"
        else:
            summary["message"] = "No valid license (engine runs in trial mode)"
        return summary

    # ------------------------------------------------------------------
    # Projects list (RULE 3: paths come from services, never hardcode)
    # ------------------------------------------------------------------
    def refresh_projects(self, limit: int = 25) -> List[Dict[str, Any]]:
        if self.container is None:
            return []
        try:
            db = self.container.get("database").db
            rows = db.fetch_all(
                "SELECT id, title, status, updated_at,"
                " last_render_output_path, project_folder_path"
                " FROM projects ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001 - empty list beats a dead UI
            return []

    # ------------------------------------------------------------------
    # Render form (RULE 7: validate gracefully, tell the user plainly)
    # ------------------------------------------------------------------
    def validate_render_inputs(
        self,
        script_path: str,
        images_folder: str,
        project_folder: str,
    ) -> Tuple[bool, str]:
        script = Path(script_path) if script_path else None
        if not script or not script.is_file():
            return False, "Choose an existing script file (.txt/.pdf/.docx)."
        if not images_folder or not Path(images_folder).is_dir():
            return False, "Choose an existing images folder."
        if not project_folder:
            return False, "Choose where the project should be stored."
        try:
            Path(project_folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"Cannot create project folder: {exc}"
        return True, ""

    def build_render_request(
        self,
        script_path: str,
        images_folder: str,
        project_folder: str,
        title: Optional[str] = None,
        export_preset: Optional[str] = None,
        channel_profile_id: Optional[str] = None,
        quality_gate: bool = False,
        enforce_license: bool = True,
    ) -> Dict[str, Any]:
        """Kwargs for CoreEngine.run_script_pipeline (exact contract)."""
        return {
            "script_path": script_path,
            "images_folder": images_folder,
            "project_folder": project_folder,
            "title": title or (Path(script_path).stem.replace("_", " ")),
            "export_preset": export_preset or None,
            "channel_profile_id": channel_profile_id or None,
            "quality_gate": bool(quality_gate),
            "enforce_license": bool(enforce_license),
        }

    # ------------------------------------------------------------------
    # D.6 data providers (presets / profiles / engines / license)
    # ------------------------------------------------------------------
    def export_presets(self) -> List[Dict[str, str]]:
        """[{id, label}] from config/export_presets.json, default flagged."""
        data: Any = None
        if self.container is not None:
            try:
                data = self.container.get("config").get_config("export_presets")
            except Exception:  # noqa: BLE001 - UI degrades to empty list
                data = None
        if not isinstance(data, dict):
            data = {}
        default_id = str(data.get("default_preset") or "")
        out: List[Dict[str, str]] = []
        for preset in data.get("presets") or []:
            pid = str(preset.get("id") or "")
            if not pid:
                continue
            label = str(preset.get("name") or pid)
            if pid == default_id:
                label += "  (default)"
            out.append({"id": pid, "label": label})
        return out

    def channel_profiles(self) -> List[Dict[str, str]]:
        """[{id, label}] from the channel_profiles table."""
        if self.container is None:
            return []
        try:
            db = self.container.get("database").db
            rows = db.fetch_all(
                "SELECT id, profile_name FROM channel_profiles"
                " ORDER BY profile_name"
            )
            return [
                {"id": str(r["id"]), "label": str(r["profile_name"])}
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return []

    def engines_status(self) -> Dict[str, Any]:
        """Engine availability for the settings page (advisory only)."""
        hardware = getattr(self.engine, "hardware", None)
        ffmpeg = ffprobe = None
        try:
            if hardware is not None:
                ffmpeg = hardware.find_ffmpeg()
                ffprobe = hardware.find_ffprobe()
        except Exception:  # noqa: BLE001
            ffmpeg = ffprobe = None
        piper = None
        if shutil.which("piper"):
            piper = shutil.which("piper")
        plugins = 0
        try:
            names_fn = getattr(self.engine, "plugin_names", None)
            if callable(names_fn):
                plugins = len(names_fn())
        except Exception:  # noqa: BLE001 - advisory only
            plugins = 0
        return {
            "ffmpeg": str(ffmpeg) if ffmpeg else None,
            "ffprobe": str(ffprobe) if ffprobe else None,
            "piper": piper,
            "modules_loaded": self.module_count(),
            "plugins_loaded": plugins,
        }

    def _drive_module(self) -> Optional[Any]:
        """The drive_upload_engine instance via the engine seam."""
        if self.engine is None:
            return None
        try:
            module_fn = getattr(self.engine, "module", None)
            return module_fn("drive_upload_engine") if module_fn else None
        except Exception:  # noqa: BLE001 - settings UI is advisory
            return None

    def drive_upload_status(self) -> Dict[str, Any]:
        """Settings-page snapshot: {available, enabled, configured,
        pending, detail}. Stable shape; never raises."""
        status: Dict[str, Any] = {
            "available": False, "enabled": False, "configured": False,
            "pending": 0, "detail": "module not loaded",
        }
        module = self._drive_module()
        if module is None:
            return status
        status["available"] = True
        status["detail"] = ""
        try:
            resp = module.upload_status()
            data = resp.get("data") or {}
            status["enabled"] = bool(data.get("enabled"))
            status["configured"] = bool(data.get("configured"))
            status["pending"] = int(data.get("pending_uploads") or 0)
            status["detail"] = str(data.get("reason") or "")
        except Exception as exc:  # noqa: BLE001
            status["detail"] = str(exc)
        return status

    def resume_drive_uploads(self) -> Tuple[bool, str]:
        """Settings button: resume persisted uploads; (ok, message)."""
        module = self._drive_module()
        if module is None:
            return False, "Drive upload module unavailable."
        try:
            resp = module.resume_pending()
        except Exception as exc:  # noqa: BLE001
            return False, f"Resume failed: {exc}"
        if not resp.get("success"):
            return False, str(resp.get("error") or "Resume failed.")
        data = resp.get("data") or {}
        attempted = int(data.get("attempted") or 0)
        if attempted == 0:
            if data.get("skipped"):
                return True, f"Drive upload: {data['skipped']}."
            return True, "No pending uploads."
        resumed = int(data.get("resumed") or 0)
        remaining = int(data.get("pending_remaining") or 0)
        return True, (
            f"Resumed {resumed} of {attempted} pending upload(s)"
            + (f" — {remaining} still pending." if remaining else ".")
        )

    def activate_license(self, key: str) -> Tuple[bool, str]:
        """Normalize LicenseManager.activate_license for the dialog."""
        key = (key or "").strip()
        if not key:
            return False, "Enter a license key."
        if self.license_manager is None:
            return False, "License module unavailable."
        try:
            resp = self.license_manager.activate_license(key)
            if resp.get("success"):
                self.ctx["license_data"] = {}  # force summary refresh
                return True, "License activated — thank you!"
            return False, str(resp.get("error") or "Activation failed.")
        except Exception as exc:  # noqa: BLE001
            return False, f"Activation error: {exc}"

    # ------------------------------------------------------------------
    # Pipeline events -> normalized UI records
    # ------------------------------------------------------------------
    def subscribe_pipeline(
        self, handler: Callable[[Dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Subscribe to all pipeline events; returns an unsubscribe fn.

        The handler receives normalize_event() dicts. Event callbacks may
        arrive from a worker thread; the Qt shell must forward them to
        the GUI thread (ui/app.py does this with a queued signal).
        """
        bus = self.engine.event_bus if self.engine is not None else None
        if bus is None:
            return lambda: None
        registrations = []
        for name in PIPELINE_EVENTS:
            def _forward(payload: Any = None, _n: str = name) -> None:
                handler(self.normalize_event(_n, payload))

            bus.subscribe(name, _forward)
            registrations.append((name, _forward))

        def _unsubscribe() -> None:
            for name, cb in registrations:
                try:
                    bus.unsubscribe(name, cb)
                except Exception:  # noqa: BLE001
                    pass

        return _unsubscribe

    @staticmethod
    def normalize_event(name: str, payload: Any) -> Dict[str, Any]:
        """Map a bus event to {event, stage, percent, text, level}."""
        data = payload if isinstance(payload, dict) else {}
        record: Dict[str, Any] = {
            "event": name, "stage": data.get("stage"),
            "percent": None, "level": "info", "text": name,
        }
        if name == "pipeline.started":
            record["text"] = "Pipeline started"
        elif name == "pipeline.stage_started":
            stage = str(data.get("stage") or "")
            record["text"] = _STAGE_VERB.get(stage, stage) + "…"
        elif name == "pipeline.stage_completed":
            record["text"] = f"Done: {data.get('stage')}"
        elif name == "pipeline.stage_skipped":
            reason = str(data.get("reason") or "")
            record["text"] = (
                f"Skipped: {data.get('stage')} — {reason}"
            ).rstrip(" —")
        elif name == "pipeline.render_progress":
            percent = data.get("progress")
            record["percent"] = round(float(percent), 1) if percent is not None else 0.0
            fps = data.get("fps") or 0.0
            eta = data.get("eta_seconds") or 0.0
            record["text"] = (
                f"Rendering — {record['percent']}% "
                f"({float(fps):.0f} fps, ETA {float(eta):.0f}s)"
            )
        elif name == "pipeline.completed":
            record["percent"] = 100.0
            output = data.get("output_file_path") or ""
            record["text"] = f"COMPLETE — {output}".rstrip(" —")
        elif name == "pipeline.failed":
            record["level"] = "error"
            record["text"] = f"FAILED at stage '{data.get('stage')}'"
        return record
