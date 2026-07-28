"""Voice profile manager: auto-create and manage character voices.

Optional BaseModule. Reads voice_setup / voice_instructions from parsed
scripts and persists profiles in the voice_profiles table.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "voice_profile_manager"

VALID_ENGINES = frozenset({"piper", "kokoro", "xtts"})
SPEED_MIN, SPEED_MAX = 0.5, 2.0
PITCH_MIN, PITCH_MAX = -12.0, 12.0
VOLUME_MIN, VOLUME_MAX = 0.0, 2.0

AUTO_ALIAS_RULES: Dict[str, Tuple[str, ...]] = {
    "NARRATOR": ("NARR", "N", "NARRATOR"),
    "HISTORIAN": ("HIST", "H", "HISTORIAN"),
    "WITNESS": ("WIT", "W", "WITNESS"),
    "EXPERT": ("EXP", "E", "EXPERT"),
    "HOST": ("HOST", "HST"),
    "VICTIM": ("VIC", "V"),
    "DETECTIVE": ("DET", "D"),
}


class VoiceProfileManager(BaseModule):
    """Auto-create and manage per-project character voice profiles."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize manager with injected services."""
        super().__init__(container, MODULE_NAME)

    def create_profiles_from_script(
        self, script_data: Dict[str, Any], project_id: str
    ) -> Dict[str, Any]:
        """Create or update voice profiles from parsed script data."""
        started = time.perf_counter()
        if not project_id:
            return self._error("project_id is required", started)
        if not isinstance(script_data, dict):
            return self._error("script_data must be a dict", started)
        warnings: List[str] = []
        seen = self._collect_instructions(script_data, warnings)
        if not seen:
            seen, auto_warnings = self._default_characters(script_data)
            warnings.extend(auto_warnings)
        created, updated, err = self._upsert_all(project_id, seen, warnings)
        if err:
            return self._error(err, started, warnings)
        return self.make_response(
            True,
            {
                "profiles_created": created,
                "profiles_updated": updated,
                "profile_ids": [p["id"] for p in created + updated],
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def save_profile(
        self,
        project_id: str,
        character_data: Dict[str, Any],
        character_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save a single character profile (insert or update)."""
        started = time.perf_counter()
        if not project_id:
            return self._error("project_id is required", started)
        data = dict(character_data or {})
        name = self._resolve_input_name(data, character_name)
        if not name:
            return self._error("character_name is required", started)
        profile_data = self._instruction_to_profile(name, data)
        valid, err = self._validate_profile_data(profile_data)
        if not valid:
            return self._error(err or "Invalid profile", started)
        profile_id = self._save_validated(project_id, name, profile_data)
        if not profile_id:
            return self._error("Failed to save profile", started)
        return self.make_response(
            True,
            {"profile_id": profile_id, "profile": self._fetch_by_id(profile_id)},
            duration_ms=_ms(started),
        )

    def load_profile(self, project_id: str, character_name: str) -> Dict[str, Any]:
        """Load profile by character name with alias resolution."""
        started = time.perf_counter()
        if not project_id or not character_name:
            return self.make_response(
                True,
                {"profile": None},
                warnings=["project_id and character_name required"],
                duration_ms=_ms(started),
            )
        canonical = self.resolve_character_alias(project_id, character_name)
        name = canonical or str(character_name).strip().upper()
        row = self._fetch_by_name(project_id, name)
        return self.make_response(
            True,
            {"profile": row, "resolved_name": name if row else None},
            duration_ms=_ms(started),
        )

    def get_all_profiles(self, project_id: str) -> Dict[str, Any]:
        """Return all voice profiles for a project."""
        started = time.perf_counter()
        if not project_id:
            return self._error("project_id is required", started)
        rows = self.db.db.fetch_all(
            "SELECT * FROM voice_profiles WHERE project_id = ? "
            "ORDER BY character_name ASC",
            (project_id,),
        )
        return self.make_response(
            True, {"profiles": rows, "count": len(rows)}, duration_ms=_ms(started)
        )

    def resolve_character_alias(self, project_id: str, alias: str) -> Optional[str]:
        """Resolve any alias to the canonical character name."""
        if not project_id or not alias:
            return None
        needle = str(alias).strip().upper()
        rows = self.db.db.fetch_all(
            "SELECT character_name, character_aliases FROM voice_profiles "
            "WHERE project_id = ?",
            (project_id,),
        )
        for row in rows:
            canonical = str(row.get("character_name") or "").upper()
            if canonical == needle:
                return canonical
            aliases = self._parse_aliases(str(row.get("character_aliases") or ""))
            if needle in aliases:
                return canonical
        return None

    def update_profile(
        self, profile_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Modify an existing profile by id."""
        started = time.perf_counter()
        if not profile_id:
            return self._error("profile_id is required", started)
        existing = self._fetch_by_id(profile_id)
        if not existing:
            return self._error("Profile not found", started)
        profile_data = self._merge_updates(existing, updates or {})
        valid, err = self._validate_profile_data(profile_data)
        if not valid:
            return self._error(err or "Invalid update", started)
        ok = self._write_profile(
            profile_id, str(existing["project_id"]), profile_data, False
        )
        if not ok:
            return self._error("Failed to update profile", started)
        return self.make_response(
            True,
            {
                "updated": True,
                "profile_id": profile_id,
                "profile": self._fetch_by_id(profile_id),
            },
            duration_ms=_ms(started),
        )

    def delete_profile(self, profile_id: str) -> Dict[str, Any]:
        """Remove a profile from the database."""
        started = time.perf_counter()
        if not profile_id:
            return self._error("profile_id is required", started)
        existing = self._fetch_by_id(profile_id)
        if not existing:
            return self.make_response(
                True,
                {"deleted": False, "profile_id": profile_id},
                warnings=["Profile not found"],
                duration_ms=_ms(started),
            )
        try:
            self.db.db.execute("DELETE FROM voice_profiles WHERE id = ?", (profile_id,))
            return self.make_response(
                True,
                {"deleted": True, "profile_id": profile_id},
                duration_ms=_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("delete_profile failed: %s", exc)
            return self._error(str(exc), started)

    def get_default_profile(self) -> Dict[str, Any]:
        """Return default NARRATOR profile used when script has no voice_setup."""
        return {
            "character_name": "NARRATOR",
            "character_aliases": "NARR,N,NARRATOR",
            "voice_model": None,
            "engine": "kokoro",
            "default_emotion": "neutral",
            "speed": 1.0,
            "pitch": 0.0,
            "volume": 1.0,
            "reverb_preset": "none",
            "echo_preset": "none",
            "breathing_enabled": False,
            "breathing_volume": 0.15,
            "pause_sentence": 0.6,
            "pause_paragraph": 1.8,
            "pause_comma": 0.2,
            "eq_preset": "documentary_male",
            "compression_enabled": True,
            "noise_gate_enabled": True,
            "de_esser_enabled": True,
            "special_effect": "none",
            "is_auto_created": True,
            "color_label": "#4A90D9",
            "role_description": "",
        }

    def generate_auto_aliases(
        self,
        character_name: str,
        custom_aliases: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Build alias list for a character."""
        canonical = str(character_name).strip().upper()
        if custom_aliases:
            aliases = [str(a).strip().upper() for a in custom_aliases if str(a).strip()]
            if canonical not in aliases:
                aliases.insert(0, canonical)
            return list(dict.fromkeys(aliases))
        if canonical in AUTO_ALIAS_RULES:
            return list(AUTO_ALIAS_RULES[canonical])
        short = re.sub(r"[^A-Z0-9]", "", canonical)[:4] or canonical
        return list(dict.fromkeys([canonical, short]))

    # ------------------------------------------------------------------
    # Instruction collection / upsert helpers
    # ------------------------------------------------------------------

    def _collect_instructions(
        self, script_data: Dict[str, Any], warnings: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Build character -> instruction map, tracking duplicates."""
        seen: Dict[str, Dict[str, Any]] = {}
        for instruction in script_data.get("voice_instructions") or []:
            if not isinstance(instruction, dict):
                warnings.append("Skipped non-dict voice instruction")
                continue
            name = str(instruction.get("character") or "").strip().upper()
            if not name:
                warnings.append("Skipped voice instruction with empty character")
                continue
            if name in seen:
                warnings.append(
                    f"Duplicate voice definition for {name}; later entry overwrites earlier"
                )
                self.log.warning("Duplicate voice definition for %s", name)
            seen[name] = instruction
        return seen

    def _default_characters(
        self, script_data: Dict[str, Any]
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """When no voice_setup, invent defaults from dialogue characters."""
        warnings: List[str] = []
        characters = self._characters_from_scenes(script_data)
        seen: Dict[str, Dict[str, Any]] = {}
        if characters:
            for character in characters:
                seen[character] = {"character": character}
            warnings.append(
                "No voice_setup section; auto-created defaults for characters: "
                + ", ".join(sorted(seen.keys()))
            )
        else:
            seen["NARRATOR"] = {"character": "NARRATOR"}
            warnings.append(
                "No voice_setup and no dialogue characters; created default NARRATOR"
            )
        return seen, warnings

    def _upsert_all(
        self,
        project_id: str,
        seen: Dict[str, Dict[str, Any]],
        warnings: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
        """Upsert every character instruction; return created, updated, error."""
        created: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        for name, instruction in seen.items():
            outcome = self._upsert_from_instruction(
                project_id, name, instruction, warnings
            )
            if outcome is None:
                return created, updated, "Profile upsert failed"
            kind, entry = outcome
            if kind == "error":
                return created, updated, str(entry)
            if kind == "created":
                created.append(entry)  # type: ignore[arg-type]
            else:
                updated.append(entry)  # type: ignore[arg-type]
        return created, updated, None

    def _upsert_from_instruction(
        self,
        project_id: str,
        name: str,
        instruction: Dict[str, Any],
        warnings: List[str],
    ) -> Optional[Tuple[str, Any]]:
        """Validate and insert/update one profile. Returns (kind, payload)."""
        profile_data = self._instruction_to_profile(name, instruction)
        valid, err = self._validate_profile_data(profile_data)
        if not valid:
            return ("error", err or "Invalid profile data")
        existing = self._fetch_by_name(project_id, name)
        if existing:
            profile_id = str(existing["id"])
            if not self._write_profile(profile_id, project_id, profile_data, False):
                return ("error", f"Failed to update profile for {name}")
            return ("updated", {"id": profile_id, "character_name": name})
        profile_id = self.db.new_id()
        if not self._write_profile(profile_id, project_id, profile_data, True):
            return ("error", f"Failed to create profile for {name}")
        model = profile_data.get("voice_model")
        if model and not self._voice_model_installed(
            str(model), str(profile_data.get("engine"))
        ):
            warnings.append(
                f"Voice model '{model}' for {name} not in installed_voices; "
                "mark for later install"
            )
        return ("created", {"id": profile_id, "character_name": name})

    def _resolve_input_name(
        self, data: Dict[str, Any], character_name: Optional[str]
    ) -> str:
        """Extract uppercase character name from args/dict."""
        name = (
            character_name or data.get("character_name") or data.get("character") or ""
        )
        return str(name).strip().upper()

    def _save_validated(
        self, project_id: str, name: str, profile_data: Dict[str, Any]
    ) -> Optional[str]:
        """Insert or update a validated profile; return profile id."""
        existing = self._fetch_by_name(project_id, name)
        if existing:
            profile_id = str(existing["id"])
            ok = self._write_profile(profile_id, project_id, profile_data, False)
            return profile_id if ok else None
        profile_id = self.db.new_id()
        ok = self._write_profile(profile_id, project_id, profile_data, True)
        return profile_id if ok else None

    def _merge_updates(
        self, existing: Dict[str, Any], updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge DB row with update dict into profile_data."""
        profile_data = self._row_to_profile_data(existing)
        for key, value in updates.items():
            if key in ("id", "project_id", "created_at"):
                continue
            mapped = self._map_field_name(key) or key
            profile_data[mapped] = value
        return profile_data

    # ------------------------------------------------------------------
    # Profile mapping / validation
    # ------------------------------------------------------------------

    def _instruction_to_profile(
        self, character_name: str, instruction: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Map script voice instruction fields to profile columns."""
        defaults = self.get_default_profile()
        name = character_name.upper()
        aliases = self.generate_auto_aliases(
            name, self._extract_custom_aliases(instruction)
        )
        profile = dict(defaults)
        profile["character_name"] = name
        profile["character_aliases"] = ",".join(aliases)
        self._apply_core_voice_fields(profile, instruction, defaults)
        self._apply_effect_fields(profile, instruction, defaults)
        self._apply_optional_meta_fields(profile, instruction)
        return profile

    def _extract_custom_aliases(
        self, instruction: Dict[str, Any]
    ) -> Optional[List[str]]:
        """Parse aliases field from instruction if present."""
        custom = instruction.get("aliases") or instruction.get("character_aliases")
        if isinstance(custom, str):
            return [p.strip() for p in custom.split(",") if p.strip()]
        if isinstance(custom, (list, tuple)):
            return list(custom)
        return None

    def _apply_core_voice_fields(
        self,
        profile: Dict[str, Any],
        instruction: Dict[str, Any],
        defaults: Dict[str, Any],
    ) -> None:
        """Apply engine/emotion/speed/pitch/volume fields."""
        profile["voice_model"] = (
            instruction.get("voice")
            or instruction.get("voice_model")
            or defaults["voice_model"]
        )
        profile["engine"] = str(instruction.get("engine") or defaults["engine"]).lower()
        profile["default_emotion"] = str(
            instruction.get("emotion")
            or instruction.get("default_emotion")
            or defaults["default_emotion"]
        ).lower()
        profile["speed"] = _as_float(instruction.get("speed"), defaults["speed"])
        profile["pitch"] = _as_float(instruction.get("pitch"), defaults["pitch"])
        profile["volume"] = _as_float(instruction.get("volume"), defaults["volume"])

    def _apply_effect_fields(
        self,
        profile: Dict[str, Any],
        instruction: Dict[str, Any],
        defaults: Dict[str, Any],
    ) -> None:
        """Apply reverb/echo/breathing/pause/eq fields."""
        profile["reverb_preset"] = str(
            instruction.get("reverb")
            or instruction.get("reverb_preset")
            or defaults["reverb_preset"]
        )
        profile["echo_preset"] = str(
            instruction.get("echo")
            or instruction.get("echo_preset")
            or defaults["echo_preset"]
        )
        breathing = instruction.get("breathing", instruction.get("breathing_enabled"))
        profile["breathing_enabled"] = _as_bool(
            breathing, defaults["breathing_enabled"]
        )
        profile["breathing_volume"] = _as_float(
            instruction.get("breathing_volume"), defaults["breathing_volume"]
        )
        profile["pause_sentence"] = _as_float(
            instruction.get("pause_sentence"), defaults["pause_sentence"]
        )
        profile["pause_paragraph"] = _as_float(
            instruction.get("pause_paragraph"), defaults["pause_paragraph"]
        )
        profile["pause_comma"] = _as_float(
            instruction.get("pause_comma"), defaults["pause_comma"]
        )
        profile["eq_preset"] = str(
            instruction.get("eq_preset") or defaults["eq_preset"]
        )
        profile["special_effect"] = str(
            instruction.get("special_effect") or defaults["special_effect"]
        )

    def _apply_optional_meta_fields(
        self, profile: Dict[str, Any], instruction: Dict[str, Any]
    ) -> None:
        """Apply auto-created flag and optional metadata."""
        profile["is_auto_created"] = bool(instruction.get("is_auto_created", True))
        if instruction.get("color_label"):
            profile["color_label"] = str(instruction["color_label"])
        if instruction.get("role_description"):
            profile["role_description"] = str(instruction["role_description"])

    def _validate_profile_data(
        self, profile_data: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Validate engine, speed, pitch, volume ranges."""
        engine = str(profile_data.get("engine") or "").lower()
        if engine not in VALID_ENGINES:
            return (
                False,
                f"Invalid engine '{engine}'. Must be piper, kokoro, or xtts.",
            )
        speed = float(profile_data.get("speed", 1.0))
        if speed < SPEED_MIN or speed > SPEED_MAX:
            return (
                False,
                f"speed must be between {SPEED_MIN} and {SPEED_MAX} (got {speed})",
            )
        pitch = float(profile_data.get("pitch", 0.0))
        if pitch < PITCH_MIN or pitch > PITCH_MAX:
            return (
                False,
                f"pitch must be between {PITCH_MIN} and {PITCH_MAX} (got {pitch})",
            )
        volume = float(profile_data.get("volume", 1.0))
        if volume < VOLUME_MIN or volume > VOLUME_MAX:
            return (
                False,
                f"volume must be between {VOLUME_MIN} and {VOLUME_MAX} (got {volume})",
            )
        name = str(profile_data.get("character_name") or "").strip()
        if not name:
            return False, "character_name is required"
        return True, None

    # ------------------------------------------------------------------
    # Database write helpers
    # ------------------------------------------------------------------

    def _write_profile(
        self,
        profile_id: str,
        project_id: str,
        profile_data: Dict[str, Any],
        is_new: bool,
    ) -> bool:
        """Insert or update a voice_profiles row."""
        now = utc_now_str()
        try:
            if is_new:
                self.db.db.execute(
                    self._insert_sql(),
                    self._insert_params(profile_id, project_id, profile_data, now),
                )
            else:
                self.db.db.execute(
                    self._update_sql(),
                    self._update_params(profile_id, profile_data, now),
                )
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.error("write_profile failed: %s", exc)
            return False

    def _insert_sql(self) -> str:
        """Return INSERT SQL for voice_profiles."""
        return """
            INSERT INTO voice_profiles (
                id, project_id, character_name, character_aliases,
                voice_model, engine, default_emotion, speed, pitch, volume,
                reverb_preset, echo_preset, breathing_enabled, breathing_volume,
                pause_sentence, pause_paragraph, pause_comma, eq_preset,
                compression_enabled, noise_gate_enabled, de_esser_enabled,
                special_effect, is_auto_created, color_label, avatar_path,
                role_description, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """

    def _update_sql(self) -> str:
        """Return UPDATE SQL for voice_profiles."""
        return """
            UPDATE voice_profiles SET
                character_name=?, character_aliases=?, voice_model=?,
                engine=?, default_emotion=?, speed=?, pitch=?, volume=?,
                reverb_preset=?, echo_preset=?, breathing_enabled=?,
                breathing_volume=?, pause_sentence=?, pause_paragraph=?,
                pause_comma=?, eq_preset=?, compression_enabled=?,
                noise_gate_enabled=?, de_esser_enabled=?, special_effect=?,
                is_auto_created=?, color_label=?, avatar_path=?,
                role_description=?, updated_at=?
            WHERE id=?
        """

    def _core_field_tuple(self, profile_data: Dict[str, Any]) -> tuple:
        """Ordered core profile fields shared by insert/update."""
        return (
            str(profile_data["character_name"]).upper(),
            str(profile_data.get("character_aliases") or ""),
            profile_data.get("voice_model"),
            str(profile_data.get("engine") or "kokoro").lower(),
            str(profile_data.get("default_emotion") or "neutral").lower(),
            float(profile_data.get("speed", 1.0)),
            float(profile_data.get("pitch", 0.0)),
            float(profile_data.get("volume", 1.0)),
            str(profile_data.get("reverb_preset") or "none"),
            str(profile_data.get("echo_preset") or "none"),
            1 if profile_data.get("breathing_enabled") else 0,
            float(profile_data.get("breathing_volume", 0.15)),
            float(profile_data.get("pause_sentence", 0.6)),
            float(profile_data.get("pause_paragraph", 1.8)),
            float(profile_data.get("pause_comma", 0.2)),
            str(profile_data.get("eq_preset") or "documentary_male"),
            1 if profile_data.get("compression_enabled", True) else 0,
            1 if profile_data.get("noise_gate_enabled", True) else 0,
            1 if profile_data.get("de_esser_enabled", True) else 0,
            str(profile_data.get("special_effect") or "none"),
            1 if profile_data.get("is_auto_created", True) else 0,
            str(profile_data.get("color_label") or "#4A90D9"),
            profile_data.get("avatar_path"),
            str(profile_data.get("role_description") or ""),
        )

    def _insert_params(
        self,
        profile_id: str,
        project_id: str,
        profile_data: Dict[str, Any],
        now: str,
    ) -> tuple:
        """Build INSERT parameter tuple."""
        return (
            (profile_id, project_id) + self._core_field_tuple(profile_data) + (now, now)
        )

    def _update_params(
        self, profile_id: str, profile_data: Dict[str, Any], now: str
    ) -> tuple:
        """Build UPDATE parameter tuple."""
        return self._core_field_tuple(profile_data) + (now, profile_id)

    def _fetch_by_name(
        self, project_id: str, character_name: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch profile row by project + character name."""
        return self.db.db.fetch_one(
            "SELECT * FROM voice_profiles WHERE project_id = ? AND character_name = ?",
            (project_id, character_name.upper()),
        )

    def _fetch_by_id(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Fetch profile row by id."""
        return self.db.db.fetch_one(
            "SELECT * FROM voice_profiles WHERE id = ?", (profile_id,)
        )

    def _voice_model_installed(self, voice_name: str, engine: Optional[str]) -> bool:
        """Return True if voice is installed, or catalog empty (warn-only)."""
        try:
            if engine:
                row = self.db.db.fetch_one(
                    "SELECT id FROM installed_voices "
                    "WHERE voice_name = ? AND engine = ?",
                    (voice_name, engine),
                )
            else:
                row = self.db.db.fetch_one(
                    "SELECT id FROM installed_voices WHERE voice_name = ?",
                    (voice_name,),
                )
            if row:
                return True
            return self.db.db.get_table_row_count("installed_voices") == 0
        except Exception:  # noqa: BLE001
            return False

    def _characters_from_scenes(self, script_data: Dict[str, Any]) -> List[str]:
        """Collect unique dialogue character names from parsed scenes."""
        names: List[str] = []
        for scene in script_data.get("scenes") or []:
            for line in scene.get("dialogue") or []:
                char = str(line.get("character") or "").strip().upper()
                if char and char not in names:
                    names.append(char)
        return names

    @staticmethod
    def _parse_aliases(raw: str) -> List[str]:
        """Split comma-separated aliases to uppercase list."""
        return [part.strip().upper() for part in raw.split(",") if part.strip()]

    @staticmethod
    def _map_field_name(key: str) -> Optional[str]:
        """Map alternate update keys to profile field names."""
        mapping = {
            "character": "character_name",
            "character_name": "character_name",
            "voice": "voice_model",
            "voice_model": "voice_model",
            "engine": "engine",
            "emotion": "default_emotion",
            "default_emotion": "default_emotion",
            "speed": "speed",
            "pitch": "pitch",
            "volume": "volume",
            "reverb": "reverb_preset",
            "reverb_preset": "reverb_preset",
            "echo": "echo_preset",
            "echo_preset": "echo_preset",
            "breathing": "breathing_enabled",
            "breathing_enabled": "breathing_enabled",
            "breathing_volume": "breathing_volume",
            "pause_sentence": "pause_sentence",
            "pause_paragraph": "pause_paragraph",
            "pause_comma": "pause_comma",
            "eq_preset": "eq_preset",
            "aliases": "character_aliases",
            "character_aliases": "character_aliases",
            "special_effect": "special_effect",
            "color_label": "color_label",
            "role_description": "role_description",
        }
        return mapping.get(key)

    def _row_to_profile_data(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB row into profile_data for validation/write."""
        return {
            "character_name": row.get("character_name"),
            "character_aliases": row.get("character_aliases") or "",
            "voice_model": row.get("voice_model"),
            "engine": row.get("engine") or "kokoro",
            "default_emotion": row.get("default_emotion") or "neutral",
            "speed": float(row.get("speed") or 1.0),
            "pitch": float(row.get("pitch") or 0.0),
            "volume": float(row.get("volume") or 1.0),
            "reverb_preset": row.get("reverb_preset") or "none",
            "echo_preset": row.get("echo_preset") or "none",
            "breathing_enabled": bool(row.get("breathing_enabled")),
            "breathing_volume": float(row.get("breathing_volume") or 0.15),
            "pause_sentence": float(row.get("pause_sentence") or 0.6),
            "pause_paragraph": float(row.get("pause_paragraph") or 1.8),
            "pause_comma": float(row.get("pause_comma") or 0.2),
            "eq_preset": row.get("eq_preset") or "documentary_male",
            "compression_enabled": bool(row.get("compression_enabled", 1)),
            "noise_gate_enabled": bool(row.get("noise_gate_enabled", 1)),
            "de_esser_enabled": bool(row.get("de_esser_enabled", 1)),
            "special_effect": row.get("special_effect") or "none",
            "is_auto_created": bool(row.get("is_auto_created", 1)),
            "color_label": row.get("color_label") or "#4A90D9",
            "avatar_path": row.get("avatar_path"),
            "role_description": row.get("role_description") or "",
        }

    def _error(
        self,
        message: str,
        started: float,
        warnings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build a recoverable error response."""
        return self.make_response(
            False,
            data={
                "error_code": "VOICE_PROFILE_ERROR",
                "user_message": message,
                "is_recoverable": True,
            },
            error=message,
            warnings=warnings or [],
            duration_ms=_ms(started),
        )


def _as_float(value: Any, default: float) -> float:
    """Coerce value to float with default."""
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce common truthy script values to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("on", "true", "1", "yes", "y"):
        return True
    if text in ("off", "false", "0", "no", "n"):
        return False
    return default


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)
