"""Unit tests for modules.image_processor.ImageProcessor.

Pure Pillow pipeline: EXIF rotation, orientation thresholds, 16:9 framing
(cover-crop vs blurred composite), proxies, low-res detection, folder
batch import, and image_assets persistence. No FFmpeg involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core.service_container import ServiceContainer
from modules.image_processor import (
    PROXY_HEIGHT,
    PROXY_WIDTH,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    ImageProcessor,
)

PROJECT = "proj-img-1"
NOW = "2026-07-16 00:00:00"
ORIENTATION_TAG = 274


@pytest.fixture
def ip(project_root: Path, tmp_path: Path) -> ImageProcessor:
    container = ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=project_root,
    )
    return ImageProcessor(container)


@pytest.fixture
def project(ip: ImageProcessor, tmp_path: Path) -> str:
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    ip.db.db.execute(
        "INSERT INTO projects (id, title, created_at, updated_at,"
        " project_folder_path) VALUES (?, ?, ?, ?, ?)",
        (PROJECT, "Image Test", NOW, NOW, str(folder)),
    )
    return PROJECT


def _solid(path: Path, size, color=(200, 30, 30), fmt=None) -> Path:
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


def _with_exif_orientation(path: Path, size, orientation: int) -> Path:
    image = Image.new("RGB", size, (10, 90, 30))
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    image.save(path, format="JPEG", exif=exif)
    return path


def _seed_asset(ip: ImageProcessor, path: Path, name: str) -> str:
    asset_id = f"asset_{name}"
    ip.db.db.execute(
        "INSERT INTO image_assets (id, project_id, original_file_path,"
        " original_filename, created_at) VALUES (?, ?, ?, ?, ?)",
        (asset_id, PROJECT, str(path), name, NOW),
    )
    return asset_id


class TestOrientationAndRotation:
    def test_detect_orientation_thresholds(self, ip: ImageProcessor) -> None:
        assert ip.detect_orientation(Image.new("RGB", (1210, 1000))) == "landscape"
        assert ip.detect_orientation(Image.new("RGB", (1190, 1000))) == "square"
        assert ip.detect_orientation(Image.new("RGB", (1000, 1000))) == "square"
        assert ip.detect_orientation(Image.new("RGB", (800, 1000))) == "square"
        assert ip.detect_orientation(Image.new("RGB", (790, 1000))) == "portrait"

    def test_exif_orientation_applied(self, ip: ImageProcessor, tmp_path: Path) -> None:
        # 600x400 on disk, Orientation 6 -> rotate 270 -> 400x600.
        path = _with_exif_orientation(tmp_path / "rot.jpg", (600, 400), 6)
        with Image.open(path) as loaded:
            rotated = ip.correct_exif_rotation(loaded)
        assert rotated.size == (400, 600)

    def test_exif_orientation_3_and_8(self, ip: ImageProcessor, tmp_path: Path) -> None:
        for orientation, expected in ((3, (400, 300)), (8, (300, 400))):
            path = _with_exif_orientation(
                tmp_path / f"o{orientation}.jpg", (400, 300), orientation
            )
            with Image.open(path) as loaded:
                rotated = ip.correct_exif_rotation(loaded)
            assert rotated.size == expected

    def test_exif_missing_is_graceful(self, ip: ImageProcessor, tmp_path: Path) -> None:
        path = _solid(tmp_path / "plain.png", (300, 200))
        with Image.open(path) as loaded:
            rotated = ip.correct_exif_rotation(loaded)
        assert rotated.size == (300, 200)


class TestFraming:
    def test_landscape_exact_169(self, ip: ImageProcessor) -> None:
        result = ip.resize_to_16_9(Image.new("RGB", (2560, 1440)), "landscape")
        assert result.size == (TARGET_WIDTH, TARGET_HEIGHT)
        assert result.mode == "RGB"

    def test_landscape_wide_center_crop(self, ip: ImageProcessor) -> None:
        result = ip.resize_to_16_9(Image.new("RGB", (4000, 1080)), "landscape")
        assert result.size == (TARGET_WIDTH, TARGET_HEIGHT)

    def test_portrait_blurred_composite(self, ip: ImageProcessor) -> None:
        # Left half blue / right half red: the pasted foreground must stay
        # sharp (pure colours), surrounding background must be blurred.
        image = Image.new("RGB", (1200, 1800), (0, 0, 255))
        for x in range(600, 1200):
            for y in range(1800):
                image.putpixel((x, y), (255, 0, 0))
        result = ip.resize_to_16_9(image, "portrait")
        assert result.size == (TARGET_WIDTH, TARGET_HEIGHT)
        # fg is src scaled to height 1080 -> 720 wide, centered at x=600.
        assert result.getpixel((700, 540)) == (0, 0, 255)  # fg blue, sharp
        assert result.getpixel((1220, 540)) == (255, 0, 0)  # fg red, sharp
        left_bg = result.getpixel((8, 540))  # blurred blue background zone
        assert left_bg[2] > left_bg[0]  # blue channel dominates there

    def test_square_blurred_composite(self, ip: ImageProcessor) -> None:
        result = ip.resize_to_16_9(Image.new("RGB", (1000, 1000)), "square")
        assert result.size == (TARGET_WIDTH, TARGET_HEIGHT)

    def test_low_res_detection(self, ip: ImageProcessor) -> None:
        assert ip.detect_low_resolution(Image.new("RGB", (800, 600))) == (False, None)
        low, msg = ip.detect_low_resolution(Image.new("RGB", (400, 300)))
        assert low is True and "400x300" in (msg or "")


class TestSingleImagePipeline:
    def test_full_single_image(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        source = _solid(tmp_path / "hero.png", (2000, 1200))
        result = ip.process_single_image(source, project, None)
        assert result["success"] is True
        data = result["data"]
        processed = Path(data["processed_path"])
        proxy = Path(data["proxy_path"])
        assert processed.exists() and proxy.exists()
        with Image.open(processed) as img:
            assert img.size == (TARGET_WIDTH, TARGET_HEIGHT)
        with Image.open(proxy) as img:
            assert img.size == (PROXY_WIDTH, PROXY_HEIGHT)
            assert img.format == "JPEG"
        assert data["orientation"] == "landscape"
        assert "resize" in data["processing_applied"]
        assert data["aspect_ratio"] == "16:9"

    def test_missing_file_graceful(self, ip: ImageProcessor, project: str) -> None:
        result = ip.process_single_image(project + "-none.png", project, None)
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_unsupported_format_graceful(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        gif = _solid(tmp_path / "anim.gif", (100, 100), fmt="GIF")
        result = ip.process_single_image(gif, project, None)
        assert result["success"] is False
        assert "unsupported" in (result["error"] or "").lower()

    def test_exif_routed_through_pipeline(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        source = _with_exif_orientation(tmp_path / "rot.jpg", (1600, 900), 6)
        result = ip.process_single_image(source, project, None)
        assert result["success"] is True
        data = result["data"]
        assert data["orientation"] == "portrait"  # rotated 270 -> 900x1600
        assert "blur_background" in data["processing_applied"]
        assert "exif_rotate" in data["processing_applied"]

    def test_corrupted_file_graceful(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        bad = tmp_path / "broken.jpg"
        bad.write_bytes(b"definitely not a jpeg")
        result = ip.process_single_image(bad, project, None)
        assert result["success"] is False
        assert "cannot open" in (result["error"] or "").lower()


class TestImageInfoAndProxy:
    def test_get_image_info_with_exif(self, ip: ImageProcessor, tmp_path: Path) -> None:
        path = tmp_path / "meta.jpg"
        image = Image.new("RGB", (640, 480), (1, 2, 3))
        exif = Image.Exif()
        exif[306] = "2020:01:02 03:04:05"  # DateTime
        image.save(path, format="JPEG", exif=exif)
        data = ip.get_image_info(path)["data"]
        assert data["width"] == 640 and data["height"] == 480
        assert data["format"] == "JPEG"
        assert data["orientation"] == "landscape"  # 640/480 = 1.33 > 1.2
        assert data["is_low_res"] is True  # below 800x600
        assert data["exif_date"] == "2020:01:02 03:04:05"
        assert data["size_bytes"] > 0

    def test_proxy_from_processed(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        processed = _solid(tmp_path / "frame.png", (1920, 1080))
        proxy = tmp_path / "out" / "frame_proxy.jpg"
        result = ip.generate_proxy(processed, proxy)
        assert result["success"] is True
        with Image.open(proxy) as img:
            assert img.size == (854, 480)
            assert img.format == "JPEG"


class TestBatchAndPersistence:
    def test_process_all_images_flow(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        progress: list = []
        ip.event_bus.subscribe("images.progress", lambda data: progress.append(data))
        sources = [_solid(tmp_path / f"src{i}.png", (2000, 1200)) for i in range(3)]
        asset_ids = [
            _seed_asset(ip, path, f"src{i}.png") for i, path in enumerate(sources)
        ]

        first = ip.process_all_images(project)["data"]
        assert (first["processed"], first["skipped"], first["failed"]) == (3, 0, 0)
        assert len(progress) == 3  # one event per image
        row = ip.db.db.fetch_one(
            "SELECT * FROM image_assets WHERE id = ?", (asset_ids[0],)
        )
        assert row["is_processed"] == 1 and row["is_proxy_generated"] == 1
        assert Path(row["processed_file_path"]).exists()
        assert Path(row["proxy_file_path"]).exists()
        assert row["width"] == 1920 and row["height"] == 1080
        assert "resize" in row["processing_applied"]

        second = ip.process_all_images(project)["data"]
        assert (second["processed"], second["skipped"]) == (0, 3)

        # Changing the source file (different size) triggers reprocessing.
        _solid(sources[0], (2100, 1200))
        third = ip.process_all_images(project)["data"]
        assert (third["processed"], third["skipped"]) == (1, 2)

        # A deleted source fails gracefully and is counted.
        sources[1].unlink()
        fourth = ip.process_all_images(project)["data"]
        assert fourth["failed"] >= 1
        row2 = ip.db.db.fetch_one(
            "SELECT warning_message FROM image_assets WHERE id = ?",
            (asset_ids[1],),
        )
        assert "processing failed" in (row2["warning_message"] or "").lower()

    def test_batch_import_folder(
        self, ip: ImageProcessor, project: str, tmp_path: Path
    ) -> None:
        incoming = tmp_path / "incoming"
        (incoming / "sub").mkdir(parents=True)
        (incoming / "sub2").mkdir()
        _solid(incoming / "a.jpg", (2000, 1200))
        _solid(incoming / "b.PNG", (900, 1400))  # uppercase suffix
        _solid(incoming / "sub" / "c.bmp", (1000, 1000))
        _solid(incoming / "sub2" / "e_low.jpg", (400, 300))
        (incoming / "d.txt").write_text("not an image", encoding="utf-8")

        result = ip.batch_import_folder(incoming, project)
        assert result["success"] is True
        data = result["data"]
        assert data["count"] == 4  # txt ignored; subfolders included
        images = data["images"]
        low = next(i for i in images if i["filename"] == "e_low.jpg")
        assert low["is_low_res"] is True
        portrait = next(i for i in images if i["filename"] == "b.PNG")
        assert portrait["orientation"] == "portrait"
        for item in images:
            assert Path(item["file_path"]).exists()
            assert "imports" in item["file_path"]
            assert item["thumbnail_path"] and Path(item["thumbnail_path"]).exists()

        rows = ip.db.db.fetch_all(
            "SELECT * FROM image_assets WHERE project_id = ?", (project,)
        )
        assert len(rows) == 4

        # Importing the same folder again must not overwrite (collisions).
        again = ip.batch_import_folder(incoming, project)["data"]
        assert again["count"] == 4
        rows = ip.db.db.fetch_all(
            "SELECT * FROM image_assets WHERE project_id = ?", (project,)
        )
        assert len(rows) == 8

    def test_missing_import_folder(self, ip: ImageProcessor, project: str) -> None:
        result = ip.batch_import_folder(project + "-missing", project)
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_required_module(self, ip: ImageProcessor) -> None:
        assert ip.is_optional_module() is False
