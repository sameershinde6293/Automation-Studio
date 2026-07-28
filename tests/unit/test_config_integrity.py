"""Config integrity guards for the Autopilot data-driven catalogs.

B.9–B.12 replaced the last PLACEHOLDER_PHASE_B configs with real File 11/07
catalogs. These tests lock the inventory: every config JSON parses, key
catalogs are complete, and the module registry matches implemented files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Registered in modules_config.json but intentionally NOT implemented at the
# end of Phase B (Phase C/D scope: image processing, intro/outro, quality,
# licensing/UI-adjacent engines).
FUTURE_MODULES = {
    "image_processor",
    "intro_outro_engine",
    "quality_checker",
    "license_manager",
    "thumbnail_generator",
    "voice_store_manager",
    "channel_profile_manager",
    "batch_engine",
    "core_engine",
}

EXPECTED_CATALOGS = {
    "animation_presets.json": ("animations", 13, "default_animation"),
    "color_grade_presets.json": ("presets", 14, "default_preset"),
    "subtitle_style_presets.json": ("styles", 8, "default_style"),
    "export_presets.json": ("presets", 4, "default_preset"),
}


class TestAllConfigsParse:
    def test_no_placeholder_phase_b_remains(self, project_root: Path) -> None:
        config_dir = project_root / "config"
        offenders = []
        for path in sorted(config_dir.glob("*.json")):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic
                pytest.fail(f"{path.name} is not valid JSON: {exc}")
            if "PLACEHOLDER_PHASE_B" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        assert offenders == [], f"Placeholder configs left: {offenders}"

    def test_catalog_sizes(self, project_root: Path) -> None:
        config_dir = project_root / "config"
        for filename, (key, count, default_key) in EXPECTED_CATALOGS.items():
            data = json.loads((config_dir / filename).read_text(encoding="utf-8"))
            assert len(data[key]) == count, f"{filename}: expected {count} {key}"
            default_id = data.get(default_key)
            ids = {entry["id"] for entry in data[key]}
            assert (
                default_id in ids
            ), f"{filename}: default '{default_id}' not in catalog"


class TestModuleRegistry:
    def test_implemented_modules_have_registry_entries(
        self, project_root: Path
    ) -> None:
        registry = json.loads(
            (project_root / "config" / "modules_config.json").read_text(
                encoding="utf-8"
            )
        )
        registered = {m["name"] for m in registry["modules"]}
        implemented = {
            p.stem
            for p in (project_root / "modules").glob("*.py")
            if p.stem != "__init__" and not p.stem.startswith("file_parser_")
        }
        implemented.discard("_file_parser_monolith")
        # tts_presets is a constants module (data for tts_engine_manager),
        # not a registry module; file_parser split family counts as one.
        missing = implemented - registered - {"file_parser", "tts_presets"}
        assert missing == set(), f"Implemented modules missing from registry: {missing}"

    def test_registry_defaults_respected(self, project_root: Path) -> None:
        registry = json.loads(
            (project_root / "config" / "modules_config.json").read_text(
                encoding="utf-8"
            )
        )
        by_name = {m["name"]: m for m in registry["modules"]}
        assert by_name["export_engine"]["required"] is True  # CAN BE DISABLED NO
        for optional in ("animation_engine", "color_grade_engine", "subtitle_engine"):
            assert by_name[optional]["required"] is False
        implemented = {
            p.stem
            for p in (project_root / "modules").glob("*.py")
            if p.stem != "__init__"
        }
        implemented |= {"file_parser"}  # package split counts as one module
        for name in by_name:
            if name in FUTURE_MODULES:
                continue
            assert name in implemented or name.startswith(
                "core"
            ), f"Registry entry '{name}' has no implementation and is not whitelisted"
