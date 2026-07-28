"""SFX engine: auto and manual sound-effect placement on the timeline.

Optional BaseModule. Loads config/sfx_config.json, uses keyword analysis
and word timestamps, and prepares placements for audio_processor.mix_tracks.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.safe_io import read_json
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "sfx_engine"

WORD_RE = re.compile(r"\b[\w']+\b", re.UNICODE)

# Placement timing bias (seconds) for dramatic effect
KEYWORD_OFFSET_SEC = -0.05  # slightly before the keyword word

DEFAULT_FADES = {
    "transitions": (0.05, 0.20),
    "impact": (0.01, 0.25),
    "dramatic": (0.01, 0.30),
    "stings": (0.02, 0.40),
    "ambient": (0.50, 0.80),
    "atmospheric": (0.40, 0.70),
    "emotional": (0.10, 0.40),
    "historical": (0.05, 0.35),
    "horror": (0.05, 0.50),
    "nature": (0.30, 0.60),
    "mechanical": (0.05, 0.20),
    "crowd": (0.10, 0.40),
}


class SFXEngine(BaseModule):
    """Place and manage timeline sound effects."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize without loading audio into memory."""
        super().__init__(container, MODULE_NAME)
        self._project_root = Path.cwd()
        try:
            cfg = getattr(self.config, "config_folder", None)
            if cfg is not None:
                self._project_root = Path(cfg).resolve().parent
        except Exception:  # noqa: BLE001
            pass
        self._catalog: Dict[str, Any] = {}
        self._loaded = False

    def load_sfx_library(self) -> Dict[str, Any]:
        """Load SFX catalog from config and verify files on disk."""
        started = time.perf_counter()
        raw = self._load_config()
        file_map = dict(raw.get("file_map") or {})
        categories = dict(raw.get("categories") or {})
        keyword_triggers = {
            str(k).lower(): str(v)
            for k, v in (raw.get("keyword_triggers") or {}).items()
        }
        volume_defaults = dict(raw.get("volume_defaults") or {})
        entries: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []
        present = 0
        for name, rel in file_map.items():
            path = self._resolve_path(str(rel))
            exists = path.exists()
            if exists:
                present += 1
            else:
                missing.append(name)
            cat = self._category_for(name, categories)
            entries[name] = {
                "name": name,
                "path": str(path),
                "relative_path": str(rel),
                "category": cat,
                "exists": exists,
                "volume": float(
                    volume_defaults.get(cat, raw.get("default_volume", 0.6))
                ),
                "fade_in": DEFAULT_FADES.get(cat, (0.1, 0.3))[0],
                "fade_out": DEFAULT_FADES.get(cat, (0.1, 0.3))[1],
            }
        # Also index by keyword for reverse lookup
        self._catalog = {
            "enabled": bool(raw.get("enabled", True)),
            "entries": entries,
            "categories": categories,
            "keyword_triggers": keyword_triggers,
            "volume_defaults": volume_defaults,
            "default_volume": float(raw.get("default_volume", 0.6)),
            "present": present,
            "missing": missing,
        }
        self._loaded = True
        return self.make_response(
            True,
            {
                "catalog": self._catalog,
                "count": len(entries),
                "present": present,
                "missing": missing,
                "categories": list(categories.keys()),
            },
            warnings=(
                [f"Missing SFX files: {', '.join(missing[:10])}"] if missing else []
            ),
            duration_ms=_ms(started),
        )

    def auto_place_sfx(self, project_id: str) -> Dict[str, Any]:
        """Auto-place SFX from scene keywords and optional transitions."""
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(
                True,
                {"placements": [], "count": 0, "disabled": True},
                duration_ms=_ms(started),
            )
        self._ensure_catalog()
        if not project_id:
            return self._err("project_id is required", started)

        scenes = self.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number ASC",
            (project_id,),
        )
        placements: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for scene in scenes:
            scene_id = str(scene["id"])
            text = self._scene_text(project_id, scene_id)
            hits = self._keywords_in_text(text)
            # Prefer keyword_analyzer if available
            analyzer_hits = self._analyzer_sfx_names(text)
            for name in analyzer_hits:
                if name not in hits:
                    hits.append(name)
            word_ts = self._word_timestamps_for_scene(project_id, scene_id)
            scene_start = float(scene.get("start_time") or 0.0)
            for sfx_name in hits:
                entry = self._catalog["entries"].get(sfx_name)
                if not entry:
                    # map via keyword_triggers
                    mapped = self._catalog["keyword_triggers"].get(sfx_name)
                    entry = self._catalog["entries"].get(mapped or "")
                    sfx_name = mapped or sfx_name
                if not entry:
                    warnings.append(f"No library entry for SFX '{sfx_name}'")
                    continue
                if not entry.get("exists"):
                    warnings.append(f"SFX file missing for '{sfx_name}'")
                    continue
                ts = self._timestamp_for_keyword(text, sfx_name, word_ts, scene_start)
                row = self._insert_placement(
                    project_id=project_id,
                    scene_id=scene_id,
                    sfx_name=entry["name"],
                    sfx_path=entry["path"],
                    timestamp=ts,
                    volume=float(entry["volume"]),
                    fade_in=float(entry["fade_in"]),
                    fade_out=float(entry["fade_out"]),
                    placement_type="auto_keyword",
                    trigger_keyword=sfx_name,
                )
                if row:
                    placements.append(row)

            # Transition SFX at scene boundary
            if float(scene.get("scene_number") or 0) > 1:
                whoosh = self._catalog["entries"].get("dark_whoosh_left")
                if whoosh and whoosh.get("exists"):
                    row = self._insert_placement(
                        project_id=project_id,
                        scene_id=scene_id,
                        sfx_name=whoosh["name"],
                        sfx_path=whoosh["path"],
                        timestamp=max(0.0, scene_start - 0.15),
                        volume=float(whoosh["volume"]),
                        fade_in=0.05,
                        fade_out=0.2,
                        placement_type="auto_transition",
                        trigger_keyword="transition",
                    )
                    if row:
                        placements.append(row)

        return self.make_response(
            True,
            {
                "placements": placements,
                "count": len(placements),
                "project_id": project_id,
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def place_sfx_manually(
        self,
        project_id: str,
        scene_id: Optional[str],
        sfx_name: str,
        timestamp: float,
        volume: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Manually place an SFX at a timestamp."""
        started = time.perf_counter()
        self._ensure_catalog()
        if not project_id or not sfx_name:
            return self._err("project_id and sfx_name are required", started)
        entry = self._catalog["entries"].get(sfx_name)
        if not entry:
            return self._err(f"SFX '{sfx_name}' not found in library", started)
        if not entry.get("exists"):
            return self._err(
                f"SFX file missing on disk for '{sfx_name}'",
                started,
            )
        vol = float(volume) if volume is not None else float(entry["volume"])
        row = self._insert_placement(
            project_id=project_id,
            scene_id=scene_id,
            sfx_name=entry["name"],
            sfx_path=entry["path"],
            timestamp=float(timestamp),
            volume=vol,
            fade_in=float(entry["fade_in"]),
            fade_out=float(entry["fade_out"]),
            placement_type="manual",
            trigger_keyword=None,
        )
        if not row:
            return self._err("Failed to save placement", started)
        return self.make_response(True, {"placement": row}, duration_ms=_ms(started))

    def get_all_placements(self, project_id: str) -> Dict[str, Any]:
        """Return all placements ordered by timestamp."""
        started = time.perf_counter()
        if not project_id:
            return self._err("project_id is required", started)
        rows = self.db.db.fetch_all(
            "SELECT * FROM sfx_placements WHERE project_id = ? "
            "ORDER BY timestamp_seconds ASC",
            (project_id,),
        )
        return self.make_response(
            True,
            {"placements": rows, "count": len(rows)},
            duration_ms=_ms(started),
        )

    def remove_placement(self, placement_id: str) -> Dict[str, Any]:
        """Delete a placement by id."""
        started = time.perf_counter()
        if not placement_id:
            return self._err("placement_id is required", started)
        existing = self.db.db.fetch_one(
            "SELECT id FROM sfx_placements WHERE id = ?", (placement_id,)
        )
        if not existing:
            return self.make_response(
                True,
                {"deleted": False, "placement_id": placement_id},
                warnings=["Placement not found"],
                duration_ms=_ms(started),
            )
        self.db.db.execute("DELETE FROM sfx_placements WHERE id = ?", (placement_id,))
        return self.make_response(
            True,
            {"deleted": True, "placement_id": placement_id},
            duration_ms=_ms(started),
        )

    def update_placement(
        self, placement_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update timestamp and/or volume for a placement."""
        started = time.perf_counter()
        if not placement_id:
            return self._err("placement_id is required", started)
        existing = self.db.db.fetch_one(
            "SELECT * FROM sfx_placements WHERE id = ?", (placement_id,)
        )
        if not existing:
            return self._err("Placement not found", started)
        ts = float(
            updates.get(
                "timestamp",
                updates.get("timestamp_seconds", existing["timestamp_seconds"]),
            )
        )
        vol = float(updates.get("volume", existing["volume"]))
        fade_in = float(updates.get("fade_in", existing["fade_in"]))
        fade_out = float(updates.get("fade_out", existing["fade_out"]))
        self.db.db.execute(
            "UPDATE sfx_placements SET timestamp_seconds = ?, volume = ?, "
            "fade_in = ?, fade_out = ? WHERE id = ?",
            (ts, vol, fade_in, fade_out, placement_id),
        )
        row = self.db.db.fetch_one(
            "SELECT * FROM sfx_placements WHERE id = ?", (placement_id,)
        )
        return self.make_response(
            True, {"updated": True, "placement": row}, duration_ms=_ms(started)
        )

    def suggest_sfx_for_scene(self, project_id: str, scene_id: str) -> Dict[str, Any]:
        """Return top 5 SFX suggestions with relevance scores."""
        started = time.perf_counter()
        self._ensure_catalog()
        text = self._scene_text(project_id, scene_id)
        scores: Dict[str, float] = {}
        text_l = text.lower()
        for keyword, sfx_name in self._catalog["keyword_triggers"].items():
            if keyword in text_l:
                scores[sfx_name] = (
                    scores.get(sfx_name, 0.0) + 1.0 + text_l.count(keyword) * 0.25
                )
        # Analyzer boost
        for name in self._analyzer_sfx_names(text):
            scores[name] = scores.get(name, 0.0) + 1.5
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        suggestions = []
        for name, score in ranked:
            entry = self._catalog["entries"].get(name, {})
            suggestions.append(
                {
                    "sfx_name": name,
                    "score": round(score, 3),
                    "exists": bool(entry.get("exists")),
                    "path": entry.get("path"),
                    "category": entry.get("category"),
                }
            )
        return self.make_response(
            True,
            {"suggestions": suggestions, "scene_id": scene_id},
            duration_ms=_ms(started),
        )

    def prepare_sfx_for_mixing(self, project_id: str) -> Dict[str, Any]:
        """Convert DB placements into audio_processor.mix_tracks sfx_list."""
        started = time.perf_counter()
        if not project_id:
            return self._err("project_id is required", started)
        rows = self.db.db.fetch_all(
            "SELECT * FROM sfx_placements WHERE project_id = ? "
            "ORDER BY timestamp_seconds ASC",
            (project_id,),
        )
        prepared: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for row in rows:
            path = str(row.get("sfx_file_path") or "")
            if not path or not Path(path).exists():
                warnings.append(f"Missing file for placement {row.get('id')}: {path}")
                continue
            prepared.append(
                {
                    "path": path,
                    "timestamp": float(row["timestamp_seconds"]),
                    "volume": float(row.get("volume") or 0.6),
                    "fade_in": float(row.get("fade_in") or 0.1),
                    "fade_out": float(row.get("fade_out") or 0.3),
                    "sfx_name": row.get("sfx_name"),
                    "placement_id": row.get("id"),
                }
            )
        return self.make_response(
            True,
            {"sfx_list": prepared, "count": len(prepared)},
            warnings=warnings,
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_catalog(self) -> None:
        """Load catalog once."""
        if not self._loaded:
            self.load_sfx_library()

    def _load_config(self) -> Dict[str, Any]:
        """Load sfx_config.json via ConfigService or disk."""
        try:
            data = self.config.get_config("sfx_config")
            if isinstance(data, dict) and data:
                return data
        except Exception:  # noqa: BLE001
            pass
        path = self._project_root / "config" / "sfx_config.json"
        # PHASE 9: a corrupt or unreadable sfx_config used to raise out
        # of load_sfx_library and fail the (optional) SFX stage. SFX is
        # a garnish — an empty catalog degrades the render gracefully,
        # which is exactly what a missing file already did.
        if path.is_file():
            data = read_json(path)
            if isinstance(data, dict):
                return data
            self.log.warning("sfx_config.json unusable — continuing without SFX")
        return {"file_map": {}, "categories": {}, "keyword_triggers": {}}

    def _resolve_path(self, rel: str) -> Path:
        """Resolve relative asset path against project root."""
        path = Path(rel)
        return path if path.is_absolute() else self._project_root / path

    def _category_for(self, name: str, categories: Dict[str, Any]) -> str:
        """Find first category listing this SFX name."""
        for cat, names in categories.items():
            if name in (names or []):
                return str(cat)
        return "atmospheric"

    def _scene_text(self, project_id: str, scene_id: str) -> str:
        """Join dialogue text for a scene."""
        rows = self.db.db.fetch_all(
            "SELECT text_content FROM dialogue_lines "
            "WHERE project_id = ? AND scene_id = ? ORDER BY line_number ASC",
            (project_id, scene_id),
        )
        return " ".join(
            str(r.get("text_content") or "").strip()
            for r in rows
            if r.get("text_content")
        )

    def _keywords_in_text(self, text: str) -> List[str]:
        """Return SFX names triggered by keyword_triggers in text."""
        text_l = (text or "").lower()
        hits: List[str] = []
        for keyword, sfx_name in self._catalog.get("keyword_triggers", {}).items():
            if keyword in text_l and sfx_name not in hits:
                hits.append(sfx_name)
        return hits

    def _analyzer_sfx_names(self, text: str) -> List[str]:
        """Optional integration with KeywordAnalyzer."""
        try:
            from modules.keyword_analyzer import KeywordAnalyzer

            analyzer = KeywordAnalyzer(self.container)
            if not analyzer.enabled:
                return []
            resp = analyzer.detect_sfx_keywords(text)
            if resp.get("success"):
                return list(resp.get("data", {}).get("sfx") or [])
        except Exception as exc:  # noqa: BLE001
            self.log.debug("keyword_analyzer SFX integration skipped: %s", exc)
        return []

    def _word_timestamps_for_scene(
        self, project_id: str, scene_id: str
    ) -> List[Dict[str, Any]]:
        """Load word timestamps for dialogue lines in a scene."""
        lines = self.db.db.fetch_all(
            "SELECT id, word_timestamps_json FROM dialogue_lines "
            "WHERE project_id = ? AND scene_id = ? ORDER BY line_number ASC",
            (project_id, scene_id),
        )
        words: List[Dict[str, Any]] = []
        for line in lines:
            raw = line.get("word_timestamps_json")
            if not raw:
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                words.extend(data)
        # Fallback table word_timestamps
        if not words:
            rows = self.db.db.fetch_all(
                "SELECT word_text, start_time_ms, end_time_ms FROM word_timestamps "
                "WHERE project_id = ? AND dialogue_line_id IN "
                "(SELECT id FROM dialogue_lines WHERE scene_id = ?) "
                "ORDER BY word_index ASC",
                (project_id, scene_id),
            )
            for row in rows:
                words.append(
                    {
                        "word": row.get("word_text"),
                        "start": float(row.get("start_time_ms") or 0) / 1000.0,
                        "end": float(row.get("end_time_ms") or 0) / 1000.0,
                    }
                )
        return words

    def _timestamp_for_keyword(
        self,
        text: str,
        sfx_or_keyword: str,
        word_ts: List[Dict[str, Any]],
        scene_start: float,
    ) -> float:
        """Find timestamp for first keyword occurrence."""
        # Find which keyword maps to this sfx
        keywords = [
            k
            for k, v in self._catalog.get("keyword_triggers", {}).items()
            if v == sfx_or_keyword or k == sfx_or_keyword
        ]
        if not keywords:
            keywords = [sfx_or_keyword]
        text_l = (text or "").lower()
        # Prefer word timestamps
        for kw in keywords:
            for item in word_ts:
                word = str(item.get("word") or "").lower().strip(".,!?;:")
                if word == kw or kw in word:
                    return max(
                        0.0,
                        float(item.get("start", 0.0))
                        + KEYWORD_OFFSET_SEC
                        + scene_start,
                    )
        # Fallback: proportional position in text
        for kw in keywords:
            idx = text_l.find(kw)
            if idx >= 0 and text_l:
                ratio = idx / max(1, len(text_l))
                # Estimate ~ scene local time if timestamps empty
                if word_ts:
                    total = float(word_ts[-1].get("end", 1.0))
                    return max(0.0, scene_start + ratio * total + KEYWORD_OFFSET_SEC)
                return max(0.0, scene_start + ratio * 5.0)
        return max(0.0, scene_start)

    def _insert_placement(
        self,
        project_id: str,
        scene_id: Optional[str],
        sfx_name: str,
        sfx_path: str,
        timestamp: float,
        volume: float,
        fade_in: float,
        fade_out: float,
        placement_type: str,
        trigger_keyword: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Insert sfx_placements row and return it."""
        placement_id = self.db.new_id()
        now = utc_now_str()
        try:
            self.db.db.execute(
                """
                INSERT INTO sfx_placements (
                    id, project_id, scene_id, sfx_name, sfx_file_path,
                    placement_type, timestamp_seconds, volume, fade_in, fade_out,
                    trigger_keyword, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    placement_id,
                    project_id,
                    scene_id,
                    sfx_name,
                    sfx_path,
                    placement_type,
                    float(timestamp),
                    float(volume),
                    float(fade_in),
                    float(fade_out),
                    trigger_keyword,
                    now,
                ),
            )
            return self.db.db.fetch_one(
                "SELECT * FROM sfx_placements WHERE id = ?", (placement_id,)
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("insert placement failed: %s", exc)
            return None

    def _err(self, message: str, started: float) -> Dict[str, Any]:
        """Error response."""
        return self.make_response(
            False,
            data={
                "error_code": "SFX_ERROR",
                "user_message": message,
                "is_recoverable": True,
            },
            error=message,
            duration_ms=_ms(started),
        )


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)
