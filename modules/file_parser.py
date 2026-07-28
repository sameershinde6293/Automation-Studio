"""File parser module: scripts, archives, and image filename matching."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.service_container import BaseModule, ServiceContainer
from modules.file_parser_helpers import (
    MODULE_NAME,
    SUPPORTED_AUDIO_FORMATS,
    SUPPORTED_IMAGE_FORMATS,
    SUPPORTED_SCRIPT_FORMATS,
    SUPPORTED_SUBTITLE_FORMATS,
    CHARACTER_TAG_RE,
    _elapsed_ms,
    _read_text,
)
from modules.file_parser_match import ImageMatchEngine
from modules.file_parser_structured import StructuredScriptEngine
from modules.file_parser_txt import TxtScriptEngine


class FileParser(BaseModule):
    """Parse documentary scripts and match scene images by filename."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize parser with injected services and sub-engines."""
        super().__init__(container, MODULE_NAME)
        self._txt = TxtScriptEngine()
        self._structured = StructuredScriptEngine()
        self._match = ImageMatchEngine()

    def parse_script(self, file_path: str | Path) -> Dict[str, Any]:
        """Auto-detect format and parse a script file."""
        started = time.perf_counter()
        path = Path(file_path)
        if not path.exists():
            return self.make_response(
                False,
                error=f"Script file not found: {path}",
                duration_ms=_elapsed_ms(started),
            )
        try:
            extension = path.suffix.lower().strip(".")
            if extension not in SUPPORTED_SCRIPT_FORMATS:
                detected = self.detect_format(path)
                if not detected["success"]:
                    return self.make_response(
                        False,
                        error=f"Unsupported script format: {extension or 'unknown'}",
                        duration_ms=_elapsed_ms(started),
                    )
                extension = detected["data"]["format"]
            parser = {
                "txt": self.parse_txt,
                "json": self.parse_json,
                "csv": self.parse_csv,
                "docx": self.parse_docx,
                "pdf": self.parse_pdf,
            }[extension]
            result = parser(path)
            result["duration_ms"] = _elapsed_ms(started)
            return result
        except Exception as exc:  # noqa: BLE001
            self.log.error("parse_script failed: %s", exc, exc_info=True)
            return self.make_response(
                False, error=str(exc), duration_ms=_elapsed_ms(started)
            )

    def parse_txt(self, file_path: str | Path) -> Dict[str, Any]:
        """Parse Autopilot/TXT documentary script format."""
        return self._txt.parse_txt_file(file_path, self.make_response)

    def parse_json(self, file_path: str | Path) -> Dict[str, Any]:
        """Parse JSON documentary script format."""
        return self._structured.parse_json_file(file_path, self.make_response, self.log)

    def parse_csv(self, file_path: str | Path) -> Dict[str, Any]:
        """Parse CSV documentary script format."""
        return self._structured.parse_csv_file(file_path, self.make_response, self.log)

    def parse_docx(self, file_path: str | Path) -> Dict[str, Any]:
        """Parse DOCX script (table or paragraph form)."""
        return self._structured.parse_docx_file(file_path, self.make_response, self.log)

    def parse_pdf(self, file_path: str | Path) -> Dict[str, Any]:
        """Parse PDF script (tables preferred, text fallback)."""
        return self._structured.parse_pdf_file(
            file_path, self.make_response, self.log, self._txt
        )

    def detect_format(self, file_path: str | Path) -> Dict[str, Any]:
        """Detect script format from extension and content."""
        path = Path(file_path)
        ext = path.suffix.lower().strip(".")
        if ext in SUPPORTED_SCRIPT_FORMATS:
            return self.make_response(True, {"format": ext})
        try:
            sample = _read_text(path)[:500].lstrip()
        except OSError as exc:
            return self.make_response(False, error=str(exc))
        if sample.startswith("{") or sample.startswith("["):
            return self.make_response(True, {"format": "json"})
        first = sample.splitlines()[0] if sample else ""
        if "//SCENE_START" in sample or CHARACTER_TAG_RE.search(first):
            return self.make_response(True, {"format": "txt"})
        if sample.count(",") >= 2 and "\n" in sample:
            return self.make_response(True, {"format": "csv"})
        return self.make_response(True, {"format": "txt"})

    def validate_parsed_data(self, parsed_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate required fields in parsed script data."""
        return self._structured.validate_parsed_data(parsed_data, self.make_response)

    def parse_time_value(self, time_string: str) -> Optional[float]:
        """Parse flexible time strings to seconds."""
        return self._structured.parse_time_value(time_string)

    def normalize_transition_name(self, name: str) -> str:
        """Normalize transition aliases to a standard id."""
        return self._txt.normalize_transition_name(name)

    def normalize_animation_name(self, name: str) -> str:
        """Normalize animation aliases to a standard id."""
        return self._txt.normalize_animation_name(name)

    def extract_zip(self, zip_path: str | Path, dest_dir: str | Path) -> Dict[str, Any]:
        """Extract a project ZIP and classify contained files."""
        started = time.perf_counter()
        source = Path(zip_path)
        dest = Path(dest_dir)
        if not source.exists():
            return self.make_response(
                False,
                error=f"ZIP not found: {source}",
                duration_ms=_elapsed_ms(started),
            )
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(source, "r") as archive:
                archive.extractall(dest)
            classified = self.classify_project_folder(dest)
            classified["duration_ms"] = _elapsed_ms(started)
            return classified
        except zipfile.BadZipFile as exc:
            return self.make_response(
                False, error=f"Invalid ZIP: {exc}", duration_ms=_elapsed_ms(started)
            )

    def classify_project_folder(self, folder: str | Path) -> Dict[str, Any]:
        """Classify files in a project folder by type."""
        root = Path(folder)
        scripts: List[str] = []
        images: List[str] = []
        audio: List[str] = []
        subtitles: List[str] = []
        if not root.exists():
            return self.make_response(False, error=f"Folder not found: {root}")
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower().strip(".")
            target = str(path)
            if ext in SUPPORTED_SCRIPT_FORMATS:
                scripts.append(target)
            elif ext in SUPPORTED_IMAGE_FORMATS:
                images.append(target)
            elif ext in SUPPORTED_AUDIO_FORMATS:
                audio.append(target)
            elif ext in SUPPORTED_SUBTITLE_FORMATS:
                subtitles.append(target)
        return self.make_response(
            True,
            {
                "scripts": scripts,
                "images": images,
                "audio": audio,
                "subtitles": subtitles,
                "root": str(root),
            },
        )

    def match_images(
        self,
        scenes: List[Dict[str, Any]],
        image_folder: str | Path,
    ) -> Dict[str, Any]:
        """Match scene image filenames to files (exact + fuzzy)."""
        return self._match.match_images(scenes, image_folder, self.make_response)
