"""Channel profile manager: CRUD + project application for profiles.

Optional BaseModule (registry priority 17, CAN BE DISABLED: YES). Has no
File 07 module spec; built from the surrounding contract pieces:
the ``channel_profiles`` table (schema.sql: full intro/outro/thumbnail
columns, UNIQUE profile_name, is_default seed row ``profile_default``),
``config/default_channel_profile.json`` (creation defaults), the catalog
configs used for validation (documentary_genres, export_presets,
transition_presets, animation_presets, subtitle_style_presets,
color_grade_presets), and File 12 "VERSION 1.0.0 / Channel profile
system".

RULE 1: no other module is imported; catalog validation reads configs
directly through the shared config service. RULE 8: every incoming
profile payload is validated against catalogs before touching the DB.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "channel_profile_manager"

_NAME_MAX_LEN = 60
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\- ]+$")
_MIN_DURATION = 0.5
_MAX_DURATION = 60.0
_MIN_FONT = 8
_MAX_FONT = 120

# Columns managed via update/create payloads (id/created_at/is_default
# are internal: is_default only changes via set_default_profile()).
_EDITABLE_COLUMNS = {
    "profile_name", "channel_name", "channel_logo_path", "genre",
    "color_primary", "color_secondary", "color_accent", "default_font",
    "default_color_grade", "default_animation", "default_transition",
    "default_export_preset", "default_subtitle_style", "subtitle_font",
    "subtitle_font_size", "subtitle_color", "subtitle_outline_color",
    "subtitle_outline_size", "subtitle_bg_enabled", "subtitle_bg_color",
    "subtitle_bg_opacity", "subtitle_position", "watermark_enabled",
    "watermark_path", "watermark_position", "watermark_opacity",
    "watermark_size", "music_folder_path", "music_volume",
    "narration_volume", "sfx_volume", "ducking_depth", "ducking_ceiling",
    "ducking_attack", "ducking_release", "intro_enabled", "intro_template",
    "intro_duration", "intro_custom_path", "outro_enabled", "outro_template",
    "outro_duration", "outro_custom_path", "thumbnail_style",
    "social_youtube", "social_instagram", "social_twitter",
    "patreon_link", "copyright_text",
}
_COLOR_COLUMNS = {
    "color_primary", "color_secondary", "color_accent", "subtitle_color",
    "subtitle_outline_color", "subtitle_bg_color",
}
_FLOAT_0_1_COLUMNS = {
    "music_volume", "narration_volume", "sfx_volume", "ducking_depth",
    "ducking_ceiling", "ducking_attack", "ducking_release",
    "watermark_opacity", "watermark_size", "subtitle_bg_opacity",
}
_INT_COLUMNS = {
    "subtitle_font_size", "subtitle_outline_size", "subtitle_bg_enabled",
    "watermark_enabled", "intro_enabled", "outro_enabled",
}
# Column -> (config short name, list key) for catalog validation.
_CATALOG_VALIDATION = {
    "genre": ("documentary_genres", "genres"),
    "default_color_grade": ("color_grade_presets", "presets"),
    "default_animation": ("animation_presets", "animations"),
    "default_transition": ("transition_presets", "presets"),
    "default_export_preset": ("export_presets", "presets"),
    "default_subtitle_style": ("subtitle_style_presets", "styles"),
}
# Projects table columns copied by apply_profile_to_project().
_APPLY_COPY_MAP = {
    "genre": "genre",
    "default_export_preset": "export_preset",
    "default_color_grade": "color_grade_preset",
    "default_transition": "default_transition",
    "default_animation": "default_animation",
    "default_subtitle_style": "default_subtitle_style",
    "music_volume": "music_volume",
    "narration_volume": "narration_volume",
    "sfx_volume": "sfx_volume",
    "intro_enabled": "has_intro",
    "outro_enabled": "has_outro",
    "watermark_enabled": "has_watermark",
}


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _slugify(name: str) -> str:
    """Lowercase slug for profile ids ('True Crime' -> 'true_crime')."""
    text = re.sub(r"[^A-Za-z0-9_\- ]", "", str(name)).strip().lower()
    return re.sub(r"[\s-]+", "_", text) or "profile"


class ChannelProfileManager(BaseModule):
    """Manage channel_profiles rows and apply them to projects."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize module and load validation catalogs (RULE 8)."""
        super().__init__(container, MODULE_NAME)
        self._catalogs: Dict[str, set] = {}
        self._profile_config: Dict[str, Any] = {}
        self._load_configs()

    def is_optional_module(self) -> bool:
        """Channel profile management is optional (required: false)."""
        return True

    def _load_configs(self) -> None:
        """Load catalog id sets for validation + creation defaults."""
        for column, (config_name, list_key) in _CATALOG_VALIDATION.items():
            try:
                data = self.config.get_config(config_name) or {}
                ids = {
                    str(item.get("id"))
                    for item in (data.get(list_key) or [])
                    if isinstance(item, dict) and item.get("id")
                }
                self._catalogs[column] = ids
            except (OSError, ValueError, KeyError) as exc:
                self.log.warning("%s catalog unavailable: %s", config_name, exc)
                self._catalogs[column] = set()
        try:
            self._profile_config = (
                self.config.get_config("default_channel_profile") or {}
            )
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("default_channel_profile config unavailable: %s", exc)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def _fetch_profile(self, ref: str) -> Optional[Dict[str, Any]]:
        """Profile row by id, then profile_name, then is_default fallback.

        projects.channel_profile_id stores either the id
        (``profile_default``) or the name (``default``) - the schema
        uses both; resolution mirrors intro_outro_engine (documented
        RULE 1 duplication).
        """
        row = self.db.db.fetch_one(
            "SELECT * FROM channel_profiles WHERE id = ?", (ref,)
        )
        if row is None:
            row = self.db.db.fetch_one(
                "SELECT * FROM channel_profiles WHERE profile_name = ?", (ref,)
            )
        if row is None:
            row = self.db.db.fetch_one(
                "SELECT * FROM channel_profiles WHERE is_default = 1"
                " ORDER BY created_at LIMIT 1"
            )
        return row

    @staticmethod
    def _ref_matches(row: Optional[Dict[str, Any]], ref: str) -> bool:
        """True when the row is exactly the one named by the ref (no
        is_default fallback - used by destructive operations)."""
        if row is None:
            return False
        return ref in (str(row.get("id")), str(row.get("profile_name")))

    # ------------------------------------------------------------------
    # Validation (RULE 8)
    # ------------------------------------------------------------------
    def _validate(
        self, data: Dict[str, Any], partial: bool
    ) -> Tuple[Dict[str, Any], List[str], List[str]]:
        """Validate/normalize a payload.

        Returns (cleaned, errors, warnings). Unknown columns are dropped
        with warnings; catalog mismatches are errors; out-of-range
        floats/durations are clamped with warnings. Catalog validation is
        skipped (silently) when the catalog config failed to load - an
        offline app must stay usable with a partial install.
        """
        cleaned: Dict[str, Any] = {}
        errors: List[str] = []
        warnings: List[str] = []

        for key, value in data.items():
            if key not in _EDITABLE_COLUMNS:
                if key in ("id", "created_at", "updated_at", "is_default"):
                    warnings.append(f"column '{key}' is managed internally; ignored")
                else:
                    warnings.append(f"unknown column ignored: {key}")
                continue
            cleaned[key] = value

        if not partial or "profile_name" in cleaned:
            name = str(cleaned.get("profile_name") or "").strip()
            if not name:
                errors.append("profile_name is required")
            elif len(name) > _NAME_MAX_LEN:
                errors.append(f"profile_name too long (max {_NAME_MAX_LEN})")
            elif not _NAME_PATTERN.match(name):
                errors.append(
                    "profile_name allows letters, digits, space, _, - only"
                )
            else:
                cleaned["profile_name"] = name

        for column, ids in self._catalogs.items():
            if column in cleaned and ids:
                value = str(cleaned[column] or "")
                if value and value not in ids:
                    errors.append(
                        f"{column} '{value}' not in catalog "
                        f"(valid: {', '.join(sorted(ids))})"
                    )

        for column in _COLOR_COLUMNS & set(cleaned):
            text = str(cleaned[column] or "").strip().lstrip("#")
            if len(text) != 6:
                errors.append(f"{column} must be #RRGGBB, got '{cleaned[column]}'")
                continue
            try:
                int(text, 16)
            except ValueError:
                errors.append(f"{column} must be #RRGGBB, got '{cleaned[column]}'")
                continue
            cleaned[column] = f"#{text.upper()}"

        for column in _FLOAT_0_1_COLUMNS & set(cleaned):
            try:
                value = float(cleaned[column])
            except (TypeError, ValueError):
                errors.append(f"{column} must be a number")
                continue
            clamped = min(1.0, max(0.0, value))
            if clamped != value:
                warnings.append(f"{column} clamped {value} -> {clamped}")
            cleaned[column] = clamped

        for column in {"intro_duration", "outro_duration"} & set(cleaned):
            try:
                value = float(cleaned[column])
            except (TypeError, ValueError):
                errors.append(f"{column} must be a number")
                continue
            clamped = min(_MAX_DURATION, max(_MIN_DURATION, value))
            if clamped != value:
                warnings.append(f"{column} clamped {value} -> {clamped}")
            cleaned[column] = clamped

        for column in {"subtitle_font_size", "subtitle_outline_size"} & set(cleaned):
            try:
                value = int(cleaned[column])
            except (TypeError, ValueError):
                errors.append(f"{column} must be an integer")
                continue
            clamped = min(_MAX_FONT, max(_MIN_FONT, value))
            if clamped != value:
                warnings.append(f"{column} clamped {value} -> {clamped}")
            cleaned[column] = clamped

        for column in {"intro_enabled", "outro_enabled", "watermark_enabled",
                       "subtitle_bg_enabled"} & set(cleaned):
            cleaned[column] = 1 if cleaned[column] else 0

        return cleaned, errors, warnings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def list_profiles(self) -> Dict[str, Any]:
        """List all profiles (default first, then by name)."""
        started = time.perf_counter()
        rows = self.db.db.fetch_all(
            "SELECT * FROM channel_profiles"
            " ORDER BY is_default DESC, profile_name"
        )
        return self.make_response(
            True, {"count": len(rows), "profiles": rows}, duration_ms=_ms(started)
        )

    def get_profile(self, ref: str) -> Dict[str, Any]:
        """Get one profile by id or name (falls back to the default)."""
        started = time.perf_counter()
        row = self._fetch_profile(str(ref))
        if row is None:
            return self.make_response(False, error=f"Profile not found: {ref}")
        return self.make_response(
            True,
            {"profile": row, "resolved": self._ref_matches(row, str(ref))},
            duration_ms=_ms(started),
        )

    def get_default_profile(self) -> Dict[str, Any]:
        """Return the is_default profile (first created as fallback)."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT * FROM channel_profiles WHERE is_default = 1"
            " ORDER BY created_at LIMIT 1"
        )
        warnings: List[str] = []
        if row is None:
            row = self.db.db.fetch_one(
                "SELECT * FROM channel_profiles ORDER BY created_at LIMIT 1"
            )
            if row is not None:
                warnings.append("no is_default profile; using oldest row")
        if row is None:
            return self.make_response(False, error="No channel profiles exist")
        return self.make_response(
            True, {"profile": row}, warnings=warnings, duration_ms=_ms(started)
        )

    def create_profile(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a profile; unprovided columns use JSON defaults."""
        started = time.perf_counter()
        if not isinstance(data, dict):
            return self.make_response(False, error="payload must be a dict")
        cleaned, errors, warnings = self._validate(dict(data), partial=False)
        existing = self.db.db.fetch_one(
            "SELECT id FROM channel_profiles WHERE profile_name = ?",
            (str(cleaned.get("profile_name") or ""),),
        )
        if existing:
            errors.append(
                f"profile_name already exists: {cleaned.get('profile_name')}"
            )
        if errors:
            return self.make_response(
                False, error="; ".join(errors), warnings=warnings,
                duration_ms=_ms(started),
            )

        base_id = f"profile_{_slugify(str(cleaned['profile_name']))}"
        profile_id = base_id
        suffix = 2
        while self.db.db.fetch_one(
            "SELECT id FROM channel_profiles WHERE id = ?", (profile_id,)
        ):
            profile_id = f"{base_id}_{suffix}"
            suffix += 1

        now = utc_now_str()
        merged = self._creation_defaults()
        merged.update(cleaned)
        merged["id"] = profile_id
        merged["created_at"] = now
        merged["updated_at"] = now

        columns = [c for c in merged if c in _EDITABLE_COLUMNS
                   or c in ("id", "created_at", "updated_at")]
        placeholders = ", ".join("?" for _ in columns)
        self.db.db.execute(
            f"INSERT INTO channel_profiles ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            tuple(merged[c] for c in columns),
        )
        self.log.info(
            "Created channel profile %s (%s)", profile_id, merged["profile_name"]
        )
        return self.make_response(
            True,
            {"id": profile_id, "profile": self._fetch_profile(profile_id)},
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def _creation_defaults(self) -> Dict[str, Any]:
        """Map default_channel_profile.json onto table column names."""
        cfg = self._profile_config
        defaults: Dict[str, Any] = {}

        def take(json_key: str, column: str) -> None:
            if json_key in cfg:
                defaults[column] = cfg[json_key]

        take("channel_name", "channel_name")
        take("genre", "genre")
        take("default_color_grade", "default_color_grade")
        take("default_animation", "default_animation")
        take("default_transition", "default_transition")
        take("default_export_preset", "default_export_preset")
        take("default_subtitle_style", "default_subtitle_style")
        take("music_volume", "music_volume")
        take("narration_volume", "narration_volume")
        take("sfx_volume", "sfx_volume")
        take("ducking_depth", "ducking_depth")
        take("ducking_ceiling", "ducking_ceiling")
        take("ducking_attack", "ducking_attack")
        take("ducking_release", "ducking_release")
        take("intro_template", "intro_template")
        take("outro_template", "outro_template")
        if "has_intro" in cfg:
            defaults["intro_enabled"] = 1 if cfg["has_intro"] else 0
        if "has_outro" in cfg:
            defaults["outro_enabled"] = 1 if cfg["has_outro"] else 0
        if "has_watermark" in cfg:
            defaults["watermark_enabled"] = 1 if cfg["has_watermark"] else 0
        intro = cfg.get("intro") or {}
        outro = cfg.get("outro") or {}
        if isinstance(intro, dict) and intro.get("duration") is not None:
            defaults["intro_duration"] = intro.get("duration")
        if isinstance(outro, dict) and outro.get("duration") is not None:
            defaults["outro_duration"] = outro.get("duration")
        return defaults

    def update_profile(self, ref: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update editable columns of one profile (bumps updated_at)."""
        started = time.perf_counter()
        if not isinstance(data, dict) or not data:
            return self.make_response(False, error="payload must be a non-empty dict")
        row = self._fetch_profile(str(ref))
        if row is None or not self._ref_matches(row, str(ref)):
            return self.make_response(False, error=f"Profile not found: {ref}")

        cleaned, errors, warnings = self._validate(dict(data), partial=True)
        name = cleaned.get("profile_name")
        if name:
            clash = self.db.db.fetch_one(
                "SELECT id FROM channel_profiles WHERE profile_name = ? AND id != ?",
                (str(name), row["id"]),
            )
            if clash:
                errors.append(f"profile_name already exists: {name}")
        if errors:
            return self.make_response(
                False, error="; ".join(errors), warnings=warnings,
                duration_ms=_ms(started),
            )
        if not cleaned:
            return self.make_response(
                False, error="nothing to update", warnings=warnings,
                duration_ms=_ms(started),
            )

        assignments = ", ".join(f"{c} = ?" for c in cleaned)
        params = list(cleaned.values()) + [utc_now_str(), row["id"]]
        self.db.db.execute(
            f"UPDATE channel_profiles SET {assignments}, updated_at = ?"
            " WHERE id = ?",
            tuple(params),
        )
        return self.make_response(
            True,
            {
                "id": row["id"],
                "updated_columns": sorted(cleaned),
                "profile": self._fetch_profile(row["id"]),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def duplicate_profile(self, ref: str, new_name: str) -> Dict[str, Any]:
        """Copy an existing profile under a new profile_name."""
        started = time.perf_counter()
        row = self._fetch_profile(str(ref))
        if row is None or not self._ref_matches(row, str(ref)):
            return self.make_response(False, error=f"Profile not found: {ref}")
        payload = {
            column: row.get(column)
            for column in _EDITABLE_COLUMNS
            if column in row and row.get(column) is not None
        }
        payload["profile_name"] = new_name
        result = self.create_profile(payload)
        result["duration_ms"] = _ms(started)
        return result

    def delete_profile(self, ref: str) -> Dict[str, Any]:
        """Delete a profile; the is_default row cannot be deleted.

        Projects referencing the deleted profile are reassigned to the
        default profile (their channel_profile_id is never left dangling).
        """
        started = time.perf_counter()
        row = self._fetch_profile(str(ref))
        if row is None or not self._ref_matches(row, str(ref)):
            return self.make_response(False, error=f"Profile not found: {ref}")
        if int(row.get("is_default") or 0):
            return self.make_response(
                False, error="cannot delete the default profile"
            )

        references = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM projects"
            " WHERE channel_profile_id IN (?, ?)",
            (row["id"], row["profile_name"]),
        )
        reassigned = int((references or {}).get("n") or 0)
        default_row = self.db.db.fetch_one(
            "SELECT id FROM channel_profiles WHERE is_default = 1 LIMIT 1"
        )
        warnings: List[str] = []
        if default_row and reassigned:
            self.db.db.execute(
                "UPDATE projects SET channel_profile_id = ?, updated_at = ?"
                " WHERE channel_profile_id IN (?, ?)",
                (default_row["id"], utc_now_str(), row["id"], row["profile_name"]),
            )
            warnings.append(
                f"{reassigned} project(s) reassigned to the default profile"
            )
        self.db.db.execute(
            "DELETE FROM channel_profiles WHERE id = ?", (row["id"],)
        )
        self.log.info(
            "Deleted channel profile %s (%s)", row["id"], row["profile_name"]
        )
        return self.make_response(
            True,
            {"deleted_id": row["id"], "projects_reassigned": reassigned},
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def set_default_profile(self, ref: str) -> Dict[str, Any]:
        """Mark one profile as the default (exclusive flag)."""
        started = time.perf_counter()
        row = self._fetch_profile(str(ref))
        if row is None or not self._ref_matches(row, str(ref)):
            return self.make_response(False, error=f"Profile not found: {ref}")
        self.db.db.execute("UPDATE channel_profiles SET is_default = 0")
        self.db.db.execute(
            "UPDATE channel_profiles SET is_default = 1, updated_at = ?"
            " WHERE id = ?",
            (utc_now_str(), row["id"]),
        )
        return self.make_response(
            True, {"default_id": row["id"]}, duration_ms=_ms(started)
        )

    def apply_profile_to_project(
        self, project_id: str, ref: str
    ) -> Dict[str, Any]:
        """Apply a profile to a project.

        Copies the profile's render-relevant defaults onto the projects
        row (genre, presets, volumes, intro/outro/watermark switches)
        and stores the canonical profile id in channel_profile_id. The
        projects table has no subtitle-enable column, so has_subtitles
        is intentionally left untouched (documented schema limit).
        """
        started = time.perf_counter()
        project = self.db.db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        if project is None:
            return self.make_response(
                False, error=f"Project not found: {project_id}"
            )
        profile = self._fetch_profile(str(ref))
        if profile is None or not self._ref_matches(profile, str(ref)):
            return self.make_response(False, error=f"Profile not found: {ref}")

        updates: Dict[str, Any] = {"channel_profile_id": profile["id"]}
        for source, target in _APPLY_COPY_MAP.items():
            if profile.get(source) is not None:
                updates[target] = profile[source]

        assignments = ", ".join(f"{c} = ?" for c in updates)
        params = list(updates.values()) + [utc_now_str(), project_id]
        self.db.db.execute(
            f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
            tuple(params),
        )
        self.log.info(
            "Applied profile %s to project %s", profile["id"], project_id
        )
        return self.make_response(
            True,
            {
                "project_id": project_id,
                "profile_id": profile["id"],
                "updated_columns": sorted(updates),
            },
            duration_ms=_ms(started),
        )

    def get_project_profile(self, project_id: str) -> Dict[str, Any]:
        """Return the profile currently referenced by a project."""
        started = time.perf_counter()
        project = self.db.db.fetch_one(
            "SELECT channel_profile_id FROM projects WHERE id = ?",
            (project_id,),
        )
        if project is None:
            return self.make_response(
                False, error=f"Project not found: {project_id}"
            )
        profile = self._fetch_profile(
            str(project.get("channel_profile_id") or "")
        )
        if profile is None:
            return self.make_response(
                False, error="No profile resolves for this project"
            )
        return self.make_response(
            True, {"project_id": project_id, "profile": profile},
            duration_ms=_ms(started),
        )
