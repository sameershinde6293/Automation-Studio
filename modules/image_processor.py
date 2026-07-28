"""Image processor: production image pipeline for video frames (MODULE 03).

Required BaseModule (CAN BE DISABLED: NO; registry priority 4). Processes
project images to exact 1920x1080 frames: EXIF rotation correction,
orientation detection, 16:9 resize/crop (blurred-background composite for
portrait and square sources), RGB conversion, low-res proxies, folder
batch import. Pure Pillow — no FFmpeg required for this stage.

Spec source: modules_specification.txt MODULE 03 IMAGE PROCESSOR. File 11
defines no image_processor config, so the File 07 constants are used.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.safe_io import LazyAttribute, atomic_write, ensure_directory
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

Image = LazyAttribute("PIL", "Image")

MODULE_NAME = "image_processor"

# Spec constants (File 07 MODULE 03)
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
PROXY_HEIGHT = 480
PROXY_WIDTH = 854  # 480 * 16/9 = 853.33 -> 854 (spec: 854x480)
SUPPORTED_FORMATS = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"]

# Orientation thresholds (spec detect_orientation)
LANDSCAPE_RATIO = 1.2
PORTRAIT_RATIO = 0.8

# Low resolution floor (spec detect_low_resolution)
LOW_RES_WIDTH = 800
LOW_RES_HEIGHT = 600

# Blurred background strength for portrait/square compositing (spec: 20)
BLUR_RADIUS = 20

# Proxy JPEG quality (spec: 70)
PROXY_JPEG_QUALITY = 70

# Display thumbnail for batch imports (UI preview; not a YouTube thumb)
THUMB_WIDTH = 320
THUMB_HEIGHT = 180

# EXIF Orientation tag id (0x0112)
_EXIF_ORIENTATION_TAG = 274


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _aspect_ratio_string(width: int, height: int) -> str:
    """Reduced aspect ratio like '16:9' (or 'W:H' when degenerate)."""
    if width <= 0 or height <= 0:
        return f"{width}:{height}"
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


class ImageProcessor(BaseModule):
    """Process all images for video production."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize the image processor."""
        super().__init__(container, MODULE_NAME)
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cancellation of image processing."""
        self._cancel_requested = True

    def is_optional_module(self) -> bool:
        """Image processing is required (CAN BE DISABLED: NO)."""
        return False

    # ------------------------------------------------------------------
    # Project folder helpers
    # ------------------------------------------------------------------
    def _project_folder(self, project_id: str) -> Optional[Path]:
        """Project root folder from the projects table (None if unknown)."""
        row = self.db.db.fetch_one(
            "SELECT project_folder_path FROM projects WHERE id = ?", (project_id,)
        )
        if not row or not row.get("project_folder_path"):
            return None
        return Path(str(row["project_folder_path"]))

    def _pipeline_folders(
        self, project_id: str, settings: Optional[Dict[str, Any]]
    ) -> Tuple[Path, Path]:
        """Resolved (processed, proxy) folders; settings may override."""
        settings = settings or {}
        base = self._project_folder(project_id)
        if base is None:
            base = Path(".")
        processed = Path(
            settings.get("processed_folder", base / "images" / "processed")
        )
        proxy = Path(settings.get("proxy_folder", base / "images" / "proxy"))
        return processed, proxy

    # ------------------------------------------------------------------
    # Single-image pipeline
    # ------------------------------------------------------------------
    def process_all_images(
        self,
        project_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Process every image_assets row for a project (spec method).

        Rows already processed are skipped while the source file is
        unchanged (same size on disk and processed file still present).
        Progress is published on the event bus as 'images.progress'.
        PHASE 10: bounded parallel worker pool with safe cancellation
        and deterministic output order.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="image_processor is disabled")

        assets = self.db.db.fetch_all(
            "SELECT * FROM image_assets WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        )
        total = len(assets)
        processed = skipped = failed = 0
        failures: List[str] = []

        jobs_to_run: List[Tuple[int, Dict[str, Any], Path]] = []
        skipped_indices: set[int] = set()

        for index, asset in enumerate(assets, start=1):
            source = Path(str(asset.get("original_file_path") or ""))
            if self._is_unchanged(asset, source):
                skipped_indices.add(index)
            else:
                jobs_to_run.append((index, asset, source))

        outcomes: Dict[int, Dict[str, Any]] = {}
        cancelled = False

        if jobs_to_run:
            workers = max(1, min(4, os.cpu_count() or 2))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self.process_single_image, source, project_id, None): index
                    for (index, asset, source) in jobs_to_run
                }
                for future in as_completed(futures):
                    if getattr(self, "_cancel_requested", False) or (
                        cancel_check and cancel_check()
                    ):
                        cancelled = True
                        for pending in futures:
                            pending.cancel()
                        break
                    idx = futures[future]
                    outcomes[idx] = future.result()

        if cancelled:
            return self.make_response(
                False,
                error="pipeline cancelled during image processing",
                warnings=failures[:5],
                duration_ms=_ms(started),
            )

        # Process results in deterministic original order & publish progress sequentially
        success_updates: List[Tuple[str, Dict[str, Any]]] = []
        warning_updates: List[Tuple[str, str]] = []

        for index, asset in enumerate(assets, start=1):
            asset_id = str(asset["id"])
            self.event_bus.publish(
                "images.progress",
                {
                    "project_id": project_id,
                    "current": index,
                    "total": total,
                    "filename": asset.get("original_filename"),
                },
            )

            if index in skipped_indices:
                skipped += 1
                continue

            result = outcomes.get(index)
            if result and result["success"]:
                data = result["data"]
                success_updates.append((asset_id, data))
                processed += 1
            else:
                failed += 1
                err_msg = result["error"] if result else "processing failed"
                failures.append(f"{asset.get('original_filename')}: {err_msg}")
                warning_updates.append((f"processing failed: {err_msg}", asset_id))

        if success_updates or warning_updates:
            try:
                with self.db.db.transaction():
                    for asset_id, data in success_updates:
                        self._record_processed(asset_id, data)
                    for warn_msg, asset_id in warning_updates:
                        self.db.db.execute(
                            "UPDATE image_assets SET warning_message = ? WHERE id = ?",
                            (warn_msg, asset_id),
                        )
            except AttributeError:
                for asset_id, data in success_updates:
                    self._record_processed(asset_id, data)
                for warn_msg, asset_id in warning_updates:
                    self.db.db.execute(
                        "UPDATE image_assets SET warning_message = ? WHERE id = ?",
                        (warn_msg, asset_id),
                    )

        return self.make_response(
            True,
            {
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
                "total": total,
                "failures": failures,
            },
            warnings=failures[:5],
            duration_ms=_ms(started),
        )

    def _is_unchanged(self, asset: Dict[str, Any], source: Path) -> bool:
        """Skip rule: processed before, outputs exist, source size same."""
        if not asset.get("is_processed"):
            return False
        processed_path = asset.get("processed_file_path")
        if not processed_path or not Path(str(processed_path)).exists():
            return False
        try:
            current_size = source.stat().st_size
        except OSError:
            return False
        return int(asset.get("file_size_bytes") or 0) == current_size

    def process_single_image(
        self,
        image_path: Any,
        project_id: str,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Full processing pipeline for one image (spec method).

        Steps: open -> EXIF rotation -> orientation -> 16:9 prepare ->
        RGB convert -> save processed PNG -> save 854x480 proxy JPG.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="image_processor is disabled")

        source = Path(str(image_path))
        # PHASE 9: `is_file()` rejects a directory handed in as an image
        # path (Pillow's error for that case is unhelpful), and absorbs
        # the OSError a malformed path can raise on Windows.
        try:
            is_file = source.is_file()
        except OSError:
            is_file = False
        if not is_file:
            return self.make_response(
                False,
                error=f"Image file not found: {source}",
                duration_ms=_ms(started),
            )
        if source.suffix.lower() not in SUPPORTED_FORMATS:
            return self.make_response(
                False,
                error=f"Unsupported image format: {source.suffix}",
                duration_ms=_ms(started),
            )

        try:
            image = Image.open(source)
            # `load()` is what actually decodes the pixels, so a
            # truncated or corrupt file fails HERE rather than later
            # inside a resize — this is the real validation step.
            image.load()
        except Exception as exc:
            return self.make_response(
                False,
                error=f"Cannot open image {source.name}: {exc}",
                duration_ms=_ms(started),
            )

        try:
            file_size = source.stat().st_size
        except OSError:
            file_size = 0
        applied: List[str] = []

        image = self.correct_exif_rotation(image)
        applied.append("exif_rotate")
        orientation = self.detect_orientation(image)
        is_lowres, lowres_msg = self.detect_low_resolution(image)

        frame = self.resize_to_16_9(image, orientation)
        if orientation in ("portrait", "square"):
            applied.append("blur_background")
        else:
            applied.append("crop")
        applied.append("resize")
        if frame.mode != "RGB":
            frame = frame.convert("RGB")
            applied.append("convert_rgb")

        processed_folder, proxy_folder = self._pipeline_folders(project_id, settings)
        # PHASE 9: an uncreatable output folder is a clean, explained
        # failure rather than an OSError escaping into the image stage.
        if ensure_directory(processed_folder) is None or (
            ensure_directory(proxy_folder) is None
        ):
            return self.make_response(
                False,
                error=f"Cannot create image output folders under {processed_folder}",
                duration_ms=_ms(started),
            )
        processed_path = processed_folder / f"{source.stem}_processed.png"
        proxy_path = proxy_folder / f"{source.stem}_proxy.jpg"
        # PHASE 8 (rendering & export optimization): PNG is lossless at
        # every compression level, so the stored pixels are unchanged —
        # level 1 just stops Pillow spending most of the per-image time
        # deflating a full-HD frame that ffmpeg decodes moments later.
        # PHASE 9: written atomically — the export stage validates every
        # scene image up front by existence, so a frame truncated by an
        # interrupted write would pass that check and only fail hours
        # later inside ffmpeg.
        if not atomic_write(
            processed_path,
            lambda temp: frame.save(temp, format="PNG", compress_level=1),
        ):
            return self.make_response(
                False,
                error=f"Could not write processed image: {processed_path}",
                duration_ms=_ms(started),
            )

        # PHASE 8: reuse the in-memory frame just saved as PNG rather than
        # decoding that same lossless file straight back off disk.
        proxy_result = self.generate_proxy(
            processed_path, proxy_path, source_image=frame
        )
        if not proxy_result["success"]:
            return self.make_response(
                False,
                error=f"Proxy generation failed: {proxy_result['error']}",
                duration_ms=_ms(started),
            )

        width, height = frame.size
        warnings: List[str] = []
        if lowres_msg:
            warnings.append(lowres_msg)

        return self.make_response(
            True,
            {
                "processed_path": str(processed_path),
                "proxy_path": str(proxy_path),
                "width": width,
                "height": height,
                "orientation": orientation,
                "aspect_ratio": _aspect_ratio_string(width, height),
                "processing_applied": ",".join(applied),
                "is_low_resolution": is_lowres,
                "warning_message": lowres_msg,
                "file_size_bytes": file_size,
                "format": image.format or source.suffix.lstrip(".").upper(),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def correct_exif_rotation(self, image: Image.Image) -> Image.Image:
        """Fix image rotation from EXIF Orientation (spec: 3/6/8)."""
        try:
            getter = getattr(image, "_getexif", None)
            exif = None
            if callable(getter):
                exif = getter()
            elif hasattr(image, "getexif"):
                exif = image.getexif()
            if exif:
                orientation = exif.get(_EXIF_ORIENTATION_TAG)
                if orientation == 3:
                    image = image.rotate(180, expand=True)
                elif orientation == 6:
                    image = image.rotate(270, expand=True)
                elif orientation == 8:
                    image = image.rotate(90, expand=True)
        except Exception:  # EXIF may be malformed; keep the image as-is
            pass
        return image

    def detect_orientation(self, image: Image.Image) -> str:
        """landscape (>1.2) / portrait (<0.8) / square (spec thresholds)."""
        width, height = image.size
        if height == 0:
            return "square"
        ratio = width / height
        if ratio > LANDSCAPE_RATIO:
            return "landscape"
        if ratio < PORTRAIT_RATIO:
            return "portrait"
        return "square"

    def resize_to_16_9(self, image: Image.Image, orientation: str) -> Image.Image:
        """Prepare an exact 1920x1080 frame (spec branch rules).

        landscape: cover-scale and center-crop. portrait/square: blurred
        cover background with the fitted image pasted centered.
        """
        target_w, target_h = TARGET_WIDTH, TARGET_HEIGHT
        src = image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image

        if orientation == "landscape":
            cover = self._resize_cover(src, target_w, target_h)
            return self._center_crop(cover, target_w, target_h)

        # portrait | square: blurred background composite
        background = self._center_crop(
            self._resize_cover(src, target_w, target_h), target_w, target_h
        )
        from PIL import ImageFilter

        background = background.convert("RGB").filter(
            ImageFilter.GaussianBlur(radius=BLUR_RADIUS)
        )
        foreground = src.convert("RGBA")
        scale = target_h / foreground.size[1]
        fg_w = int(foreground.size[0] * scale)
        foreground = foreground.resize((fg_w, target_h), Image.LANCZOS)
        x = (target_w - foreground.size[0]) // 2
        composite = background.convert("RGBA")
        composite.paste(foreground, (x, 0), foreground)
        return composite.convert("RGB")

    @staticmethod
    def _resize_cover(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """Scale so both dimensions are >= target (cover strategy)."""
        scale = max(target_w / image.size[0], target_h / image.size[1])
        new_size = (
            max(target_w, int(round(image.size[0] * scale))),
            max(target_h, int(round(image.size[1] * scale))),
        )
        return image.resize(new_size, Image.LANCZOS)

    @staticmethod
    def _center_crop(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """Center-crop to exactly target size."""
        width, height = image.size
        left = max(0, (width - target_w) // 2)
        top = max(0, (height - target_h) // 2)
        return image.crop((left, top, left + target_w, top + target_h))

    def generate_proxy(
        self,
        processed_image_path: Any,
        proxy_path: Any,
        source_image: Optional[Image.Image] = None,
    ) -> Dict[str, Any]:
        """Generate the 854x480 LANCZOS JPEG preview proxy (spec method).

        PHASE 8 (rendering & export optimization): ``source_image`` lets
        a caller that already holds the processed frame in memory hand it
        over instead of forcing a re-read and re-decode of the full-size
        PNG that was just written. PNG is lossless, so the pixels are the
        same either way and the proxy is byte-identical. Omitting the
        argument keeps the original read-from-disk behavior exactly.
        """
        started = time.perf_counter()
        source = Path(str(processed_image_path))
        if source_image is None and not source.exists():
            return self.make_response(
                False,
                error=f"Processed image not found: {source}",
                duration_ms=_ms(started),
            )
        try:
            if source_image is not None:
                image = source_image
                if image.mode != "RGB":
                    image = image.convert("RGB")
            else:
                image = Image.open(source).convert("RGB")
            # Keep the 16:9 geometry: scale by height, derive width.
            width = int(round(image.size[0] * (PROXY_HEIGHT / image.size[1])))
            proxy = image.resize((width, PROXY_HEIGHT), Image.LANCZOS)
            proxy = self._center_crop(proxy, PROXY_WIDTH, PROXY_HEIGHT)
            dest = Path(str(proxy_path))
            # PHASE 9: atomic — a half-written proxy is what the UI
            # timeline reads back as a scene thumbnail.
            if not atomic_write(
                dest,
                lambda temp: proxy.save(
                    temp, format="JPEG", quality=PROXY_JPEG_QUALITY
                ),
            ):
                return self.make_response(
                    False,
                    error=f"Could not write proxy image: {dest}",
                    duration_ms=_ms(started),
                )
        except Exception as exc:
            return self.make_response(
                False,
                error=f"Proxy generation failed: {exc}",
                duration_ms=_ms(started),
            )
        return self.make_response(
            True, {"proxy_path": str(proxy_path)}, duration_ms=_ms(started)
        )

    def detect_low_resolution(self, image: Image.Image) -> Tuple[bool, Optional[str]]:
        """Below 800x600 -> (True, warning message) per spec."""
        width, height = image.size
        if width < LOW_RES_WIDTH or height < LOW_RES_HEIGHT:
            return True, (
                f"Image is {width}x{height}"
                f" (minimum {LOW_RES_WIDTH}x{LOW_RES_HEIGHT} recommended)"
            )
        return False, None

    def get_image_info(self, image_path: Any) -> Dict[str, Any]:
        """Full information about one image file (spec method)."""
        started = time.perf_counter()
        path = Path(str(image_path))
        if not path.exists():
            return self.make_response(
                False,
                error=f"Image file not found: {path}",
                duration_ms=_ms(started),
            )
        try:
            image = Image.open(path)
            image.load()
        except Exception as exc:
            return self.make_response(
                False,
                error=f"Cannot open image {path.name}: {exc}",
                duration_ms=_ms(started),
            )
        width, height = image.size
        is_lowres, _msg = self.detect_low_resolution(image)
        exif_date, exif_gps = self._extract_exif_summary(image)
        return self.make_response(
            True,
            {
                "width": width,
                "height": height,
                "format": image.format,
                "mode": image.mode,
                "size_bytes": path.stat().st_size,
                "orientation": self.detect_orientation(image),
                "aspect_ratio": _aspect_ratio_string(width, height),
                "is_low_res": is_lowres,
                "exif_date": exif_date,
                "exif_gps": exif_gps,
            },
            duration_ms=_ms(started),
        )

    @staticmethod
    def _extract_exif_summary(
        image: Image.Image,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Light EXIF summary: capture date and GPS presence string."""
        exif_date: Optional[str] = None
        exif_gps: Optional[str] = None
        try:
            getter = getattr(image, "_getexif", None)
            exif = getter() if callable(getter) else image.getexif()
            if exif:
                date_raw = exif.get(306) or exif.get(36867)  # DateTime / Original
                if date_raw:
                    exif_date = str(date_raw)
                if exif.get(34853):  # GPSInfo
                    exif_gps = "present"
        except Exception:
            pass
        return exif_date, exif_gps

    # ------------------------------------------------------------------
    # Batch import
    # ------------------------------------------------------------------
    def batch_import_folder(self, folder_path: Any, project_id: str) -> Dict[str, Any]:
        """Recursively import all supported images from a folder.

        Copies into <project>/images/imports, inserts image_assets rows,
        and generates a small display thumbnail per image (UI preview —
        distinct from the YouTube thumbnail_generator output).
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="image_processor is disabled")

        source_folder = Path(str(folder_path))
        if not source_folder.exists() or not source_folder.is_dir():
            return self.make_response(
                False,
                error=f"Import folder not found: {source_folder}",
                duration_ms=_ms(started),
            )
        project_folder = self._project_folder(project_id)
        if project_folder is None:
            return self.make_response(
                False,
                error=f"Project not found or has no folder: {project_id}",
                duration_ms=_ms(started),
            )

        candidates = sorted(
            p
            for p in source_folder.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_FORMATS
        )
        imports_folder = project_folder / "images" / "imports"
        thumbs_folder = project_folder / "images" / "thumbnails"
        imports_folder.mkdir(parents=True, exist_ok=True)
        thumbs_folder.mkdir(parents=True, exist_ok=True)

        imported: List[Dict[str, Any]] = []
        for candidate in candidates:
            record = self._import_one(
                candidate, imports_folder, thumbs_folder, project_id
            )
            if record is not None:
                imported.append(record)

        return self.make_response(
            True,
            {"images": imported, "count": len(imported)},
            duration_ms=_ms(started),
        )

    def _import_one(
        self,
        candidate: Path,
        imports_folder: Path,
        thumbs_folder: Path,
        project_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Copy one image into the project and insert its asset row."""
        try:
            info = self.get_image_info(candidate)
            if not info["success"]:
                return None
            data = info["data"]
            destination = self._collision_free(imports_folder / candidate.name)
            import shutil

            shutil.copy2(candidate, destination)
            thumb_path = thumbs_folder / f"{destination.stem}_thumb.jpg"
            self._write_display_thumbnail(destination, thumb_path)

            asset_id = f"img_{uuid.uuid4().hex[:12]}"
            self.db.db.execute(
                "INSERT INTO image_assets"
                " (id, project_id, original_file_path, original_filename,"
                "  width, height, aspect_ratio, orientation, file_size_bytes,"
                "  format, is_low_resolution, warning_message, exif_date,"
                "  exif_gps, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    asset_id,
                    project_id,
                    str(destination),
                    destination.name,
                    int(data["width"]),
                    int(data["height"]),
                    data.get("aspect_ratio"),
                    data.get("orientation"),
                    int(data["size_bytes"]),
                    data.get("format"),
                    1 if data.get("is_low_res") else 0,
                    None,
                    data.get("exif_date"),
                    data.get("exif_gps"),
                    utc_now_str(),
                ),
            )
            return {
                "id": asset_id,
                "file_path": str(destination),
                "filename": destination.name,
                "thumbnail_path": str(thumb_path) if thumb_path.exists() else None,
                "width": data["width"],
                "height": data["height"],
                "orientation": data["orientation"],
                "is_low_res": data["is_low_res"],
            }
        except (OSError, ValueError) as exc:
            self.log.warning("import failed for %s: %s", candidate, exc)
            return None

    @staticmethod
    def _collision_free(path: Path) -> Path:
        """Append a counter when the destination name already exists."""
        if not path.exists():
            return path
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}")

    @staticmethod
    def _write_display_thumbnail(source: Path, thumb_path: Path) -> None:
        """Small JPEG display thumbnail (contain + LANCZOS)."""
        image = Image.open(source).convert("RGB")
        image.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS)
        image.save(thumb_path, format="JPEG", quality=80)

    def _record_processed(self, asset_id: str, data: Dict[str, Any]) -> None:
        """Persist a successful single-image result into image_assets."""
        self.db.db.execute(
            "UPDATE image_assets SET"
            " processed_file_path = ?, proxy_file_path = ?, width = ?,"
            " height = ?, aspect_ratio = ?, orientation = ?, format = ?,"
            " file_size_bytes = ?, is_processed = 1, is_proxy_generated = 1,"
            " processing_applied = ?, is_low_resolution = ?,"
            " warning_message = ?"
            " WHERE id = ?",
            (
                data["processed_path"],
                data["proxy_path"],
                int(data["width"]),
                int(data["height"]),
                data["aspect_ratio"],
                data["orientation"],
                data.get("format"),
                int(data.get("file_size_bytes") or 0),
                data["processing_applied"],
                1 if data.get("is_low_resolution") else 0,
                data.get("warning_message"),
                asset_id,
            ),
        )
