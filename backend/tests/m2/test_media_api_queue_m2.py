"""Milestone 2 media API, background queue, and probe tests."""

from __future__ import annotations

from io import BytesIO
import time

from PIL import Image


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 6), color="red").save(buf, format="PNG")
    return buf.getvalue()


def test_upload_asset_crud_download_and_delete(api_client, tmp_media_root):
    response = api_client.post(
        "/api/media/assets",
        files={"file": ("photo.png", _png_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 201
    asset = response.json()["asset"]
    assert asset["content_type"] == "image/png"
    assert asset["media_type"] == "image"

    listed = api_client.get("/api/media/assets").json()
    assert len(listed) == 1
    fetched = api_client.get(f"/api/media/assets/{asset['id']}").json()
    assert fetched["checksum_sha256"] == asset["checksum_sha256"]
    updated = api_client.put(f"/api/media/assets/{asset['id']}", json={"metadata_": {"alt": "red"}}).json()
    assert updated["metadata_"]["alt"] == "red"
    content = api_client.get(f"/api/media/assets/{asset['id']}/content")
    assert content.status_code == 200
    assert content.content.startswith(b"\x89PNG")
    assert api_client.delete(f"/api/media/assets/{asset['id']}").status_code == 204
    assert api_client.get(f"/api/media/assets/{asset['id']}").status_code == 404


def test_process_returns_202_and_progress_can_be_polled(api_client, tmp_media_root):
    asset = api_client.post(
        "/api/media/assets",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    ).json()["asset"]
    response = api_client.post(f"/api/media/{asset['id']}/process")
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    for _ in range(40):
        job = api_client.get(f"/api/media/jobs/{job_id}").json()
        if job["status"] == "COMPLETED":
            break
        time.sleep(0.02)
    assert job["progress"] == 100
    assert job["result"]["metadata"]["image"]["width"] == 8


def test_process_wait_true_compatibility_mode(api_client, tmp_media_root):
    asset = api_client.post(
        "/api/media/assets",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    ).json()["asset"]
    response = api_client.post(f"/api/media/{asset['id']}/process?wait=true")
    assert response.status_code == 202
    assert response.json()["job"]["status"] == "COMPLETED"


def test_upload_process_accepts_202(api_client, tmp_media_root):
    response = api_client.post(
        "/api/media/assets?process=true",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


def test_probe_endpoint_gracefully_falls_back_for_image(api_client, tmp_media_root):
    asset = api_client.post(
        "/api/media/assets",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    ).json()["asset"]
    # Images are safe to ask ffprobe about; without ffprobe or on unsupported
    # media the endpoint returns a structured fallback instead of crashing.
    response = api_client.post(f"/api/media/{asset['id']}/probe")
    assert response.status_code == 200
    assert "available" in response.json()


def test_ffmpeg_introspection(api_client):
    body = api_client.get("/api/media/ffmpeg").json()
    assert {"ffmpeg", "ffprobe", "ffmpeg_available", "ffprobe_available"} <= set(body)


def test_missing_media_resources_return_404(api_client):
    assert api_client.get("/api/media/assets/999").status_code == 404
    assert api_client.post("/api/media/999/process").status_code == 404
    assert api_client.get("/api/media/jobs/999").status_code == 404


def test_upload_rejects_empty_file(api_client, tmp_media_root):
    response = api_client.post("/api/media/assets", files={"file": ("empty.bin", b"", "application/octet-stream")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_register_existing_file_requires_media_root_path(api_client, tmp_media_root):
    response = api_client.post(
        "/api/media/assets/register",
        json={"filename": "escape", "file_path": "/tmp/escape", "media_type": "binary"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "security_policy_violation"
