"""Unit tests for modules.voice_store_manager.VoiceStoreManager.

Catalog refresh/upsert into voice_store_cache, browsing/search,
install/uninstall lifecycle against installed_voices (files under
engines/<engine>/models/), RAM/size warnings, and graceful network
failure handling. HTTP is always injected - no real network access.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from core.service_container import ServiceContainer
from modules.voice_store_manager import VoiceStoreManager

NOW = "2026-07-16 00:00:00"

CATALOG = {
    "version": "1.0",
    "voices": [
        {
            "voice_name": "alan",
            "display_name": "Alan (Deep Narrator)",
            "engine": "piper",
            "language": "en",
            "accent": "gb",
            "gender": "male",
            "quality_rating": 4,
            "download_url": "https://example.invalid/voices/alan.onnx",
            "file_size_mb": 10.0,
            "ram_required_mb": 512,
            "supported_emotions": "neutral,serious",
            "tags": "deep,narrator",
            "description": "Deep documentary narrator voice",
            "is_featured": 1,
        },
        {
            "voice_name": "bella",
            "display_name": "Bella (Warm)",
            "engine": "piper",
            "language": "en",
            "gender": "female",
            "quality_rating": 5,
            "download_url": "https://example.invalid/voices/bella.onnx",
            "file_size_mb": 9.0,
            "ram_required_mb": 512,
            "tags": "warm",
        },
        {
            "voice_name": "narratore",
            "display_name": "Narratore X",
            "engine": "xtts",
            "language": "en",
            "gender": "male",
            "quality_rating": 5,
            "download_url": "https://example.invalid/voices/narratore.gguf",
            "file_size_mb": 2048.0,
            "ram_required_mb": 4096,
            "tags": "premium",
        },
        {
            "voice_name": "ghost",
            "display_name": "Ghost (skipped: bad engine)",
            "engine": "weird",
            "download_url": "https://example.invalid/voices/ghost.bin",
        },
        {
            "voice_name": "broken",
            "display_name": "Broken (skipped: no url)",
            "engine": "piper",
        },
    ],
}


def _container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    return ServiceContainer.create_production_container(
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


@pytest.fixture
def vsm(project_root: Path, tmp_path: Path) -> VoiceStoreManager:
    return VoiceStoreManager(_container(project_root, tmp_path))


@pytest.fixture
def catalog(vsm: VoiceStoreManager, tmp_path: Path) -> VoiceStoreManager:
    vsm._project_root = tmp_path  # keep model files out of the repo
    result = vsm.refresh_catalog(catalog_data=CATALOG)
    assert result["success"] is True
    return vsm


class _FakeResponse:
    def __init__(self, chunks, status: int = 200) -> None:
        self._chunks = list(chunks)
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 65536):
        yield from self._chunks


class _FakeSession:
    def __init__(self, chunks, status: int = 200) -> None:
        self._response = _FakeResponse(chunks, status)
        self.urls: list = []

    def get(self, url, stream: bool = False, timeout: int = 0):
        self.urls.append(url)
        return self._response


def _model_root(vsm: VoiceStoreManager) -> Path:
    return vsm._project_root / "engines"


# ------------------------------------------------------------------
# Catalog refresh
# ------------------------------------------------------------------
def test_optional_module_flag(vsm: VoiceStoreManager) -> None:
    assert vsm.is_optional_module() is True


def test_refresh_bundled_empty_catalog(vsm: VoiceStoreManager) -> None:
    result = vsm.refresh_catalog()
    assert result["success"] is True
    assert result["data"]["source"] == "bundled"
    assert result["data"]["total_cached"] == 0


def test_refresh_upserts_and_skips_invalid(catalog: VoiceStoreManager) -> None:
    result = catalog.refresh_catalog(catalog_data=CATALOG)
    data = result["data"]
    assert data["added"] == 0  # second load = updates
    assert data["updated"] == 3  # ghost (engine) + broken (no url) skipped
    assert data["total_cached"] == 3


def test_refresh_first_load_counts_added(vsm: VoiceStoreManager) -> None:
    result = vsm.refresh_catalog(catalog_data=CATALOG)
    assert result["data"]["added"] == 3
    assert result["data"]["updated"] == 0


def test_refresh_invalid_catalog_rejected(vsm: VoiceStoreManager) -> None:
    result = vsm.refresh_catalog(catalog_data={"voices": "not-a-list"})
    assert result["success"] is False
    assert "invalid catalog" in result["error"]


def test_remote_failure_falls_back_to_bundled(
    vsm: VoiceStoreManager, monkeypatch
) -> None:
    def _boom(url, timeout=0):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", _boom)
    result = vsm.refresh_catalog(catalog_url="https://example.invalid/cat.json")
    assert result["success"] is True
    assert result["data"]["source"] == "bundled"
    assert any("bundled" in w for w in result["warnings"])


# ------------------------------------------------------------------
# Browsing
# ------------------------------------------------------------------
def test_list_voices_filters(catalog: VoiceStoreManager) -> None:
    piper = catalog.list_voices(engine="piper")["data"]
    assert piper["count"] == 2
    names = [v["voice_name"] for v in piper["voices"]]
    # ORDER BY is_featured DESC, quality_rating DESC: alan (featured) first
    assert names == ["alan", "bella"]
    featured = catalog.list_voices(featured=True)["data"]
    assert featured["count"] == 1
    assert featured["voices"][0]["voice_name"] == "alan"
    female = catalog.list_voices(gender="female")["data"]
    assert female["count"] == 1


def test_list_voices_lazy_bundled_load(vsm: VoiceStoreManager) -> None:
    result = vsm.list_voices()
    assert result["success"] is True
    assert result["data"]["count"] == 0  # bundled catalog ships empty


def test_search_voices(catalog: VoiceStoreManager) -> None:
    hits = catalog.search_voices("narrator")["data"]
    assert hits["count"] >= 1
    assert all("narrator" in (v["voice_name"] + v["display_name"]
               + v["tags"] + v["description"]).lower() for v in hits["voices"])
    assert catalog.search_voices("zzz-none")["data"]["count"] == 0


def test_get_voice(catalog: VoiceStoreManager) -> None:
    row = catalog.get_voice("piper:alan")
    assert row["success"] is True
    assert row["data"]["voice"]["ram_required_mb"] == 512
    assert catalog.get_voice("piper:none")["success"] is False


# ------------------------------------------------------------------
# Install lifecycle
# ------------------------------------------------------------------
def _install_alan(vsm: VoiceStoreManager, chunks=(b"\x00" * 4096,) * 4):
    session = _FakeSession(chunks)
    result = vsm.install_voice("piper:alan", session=session)
    return result, session


def test_install_voice_writes_model_and_row(
    catalog: VoiceStoreManager,
) -> None:
    result, session = _install_alan(catalog)
    assert result["success"] is True, result["error"]
    data = result["data"]
    assert session.urls == ["https://example.invalid/voices/alan.onnx"]
    model = Path(data["model_file_path"])
    assert model.exists()
    assert model.name == "alan.onnx"
    assert model.parent == _model_root(catalog) / "piper" / "models" / "alan"
    row = catalog.db.db.fetch_one(
        "SELECT * FROM installed_voices WHERE engine = 'piper'"
        " AND voice_name = 'alan'"
    )
    assert row is not None
    assert row["store_voice_id"] == "piper:alan"
    assert row["model_file_path"] == str(model)
    assert int(row["is_enabled"]) == 1
    assert int(row["total_uses"]) == 0
    assert int(row["ram_required_mb"]) == 512
    cache = catalog.db.db.fetch_one(
        "SELECT is_installed FROM voice_store_cache WHERE id = 'piper:alan'"
    )
    assert int(cache["is_installed"]) == 1


def test_install_emits_progress_and_installed_events(
    catalog: VoiceStoreManager,
) -> None:
    events = []
    catalog.event_bus.subscribe(
        "voice_store.download_progress", events.append
    )
    installed = []
    catalog.event_bus.subscribe(
        "voice_store.voice_installed", installed.append
    )
    _install_alan(catalog, chunks=(b"a" * 1024,) * 3)
    assert len(events) == 3
    assert events[-1]["voice_id"] == "piper:alan"
    assert installed and installed[0]["voice_id"] == "piper:alan"


def test_install_unknown_voice_rejected(catalog: VoiceStoreManager) -> None:
    result = catalog.install_voice("piper:none", session=_FakeSession([b"x"]))
    assert result["success"] is False


def test_install_twice_blocked_then_forced(
    catalog: VoiceStoreManager,
) -> None:
    first, _ = _install_alan(catalog)
    again = catalog.install_voice("piper:alan", session=_FakeSession([b"x"]))
    assert again["success"] is False
    assert "already installed" in again["error"]
    forced = catalog.install_voice(
        "piper:alan", force=True, session=_FakeSession([b"new-bytes"])
    )
    assert forced["success"] is True
    assert forced["data"]["reinstalled"] is True
    assert forced["data"]["installed_id"] != first["data"]["installed_id"]
    rows = catalog.db.db.fetch_all(
        "SELECT id FROM installed_voices WHERE voice_name = 'alan'"
    )
    assert len(rows) == 1  # UNIQUE(engine, voice_name) respected
    assert Path(forced["data"]["model_file_path"]).read_bytes() == b"new-bytes"


def test_install_http_failure_is_graceful(catalog: VoiceStoreManager) -> None:
    result = catalog.install_voice(
        "piper:alan", session=_FakeSession([b"x"], status=404)
    )
    assert result["success"] is False
    assert "download failed" in result["error"]
    root = _model_root(catalog) / "piper" / "models" / "alan"
    if root.exists():
        assert not any(root.glob("*.onnx"))
    assert (
        catalog.db.db.fetch_one(
            "SELECT id FROM installed_voices WHERE voice_name = 'alan'"
        )
        is None
    )


def test_install_empty_download_rejected(catalog: VoiceStoreManager) -> None:
    result = catalog.install_voice("piper:alan", session=_FakeSession([]))
    assert result["success"] is False
    assert "empty" in result["error"]


def test_install_size_drift_warns(catalog: VoiceStoreManager) -> None:
    result, _ = _install_alan(catalog, chunks=(b"tiny",))
    assert result["success"] is True
    assert any("size drift" in w for w in result["warnings"])


def test_install_ram_warning(catalog: VoiceStoreManager, monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.voice_store_manager.psutil.virtual_memory",
        lambda: SimpleNamespace(available=8 * 1024 * 1024),
    )
    result = catalog.install_voice(
        "xtts:narratore",
        session=_FakeSession([b"\x00" * 1024]),
    )
    assert result["success"] is True
    assert any("RAM" in w for w in result["warnings"])


def test_install_disabled_module_error(catalog: VoiceStoreManager) -> None:
    catalog.set_enabled(False)
    result = catalog.install_voice(
        "piper:alan", session=_FakeSession([b"x"])
    )
    assert result["success"] is False
    assert "disabled" in result["error"]


# ------------------------------------------------------------------
# Uninstall + inventory
# ------------------------------------------------------------------
def test_uninstall_by_catalog_id(catalog: VoiceStoreManager) -> None:
    result, _ = _install_alan(catalog)
    model = Path(result["data"]["model_file_path"])
    voice_dir = model.parent
    events = []
    catalog.event_bus.subscribe(
        "voice_store.voice_uninstalled", events.append
    )
    done = catalog.uninstall_voice("piper:alan")
    assert done["success"] is True
    assert not model.exists()
    assert not voice_dir.exists()  # emptied dir is removed
    assert (
        catalog.db.db.fetch_one(
            "SELECT id FROM installed_voices WHERE voice_name = 'alan'"
        )
        is None
    )
    cache = catalog.db.db.fetch_one(
        "SELECT is_installed FROM voice_store_cache WHERE id = 'piper:alan'"
    )
    assert int(cache["is_installed"]) == 0
    assert events and events[0]["engine"] == "piper"


def test_uninstall_not_installed_rejected(catalog: VoiceStoreManager) -> None:
    assert catalog.uninstall_voice("piper:alan")["success"] is False


def test_list_and_get_installed(catalog: VoiceStoreManager) -> None:
    _install_alan(catalog)
    catalog.install_voice(
        "piper:bella", session=_FakeSession([b"1" * 256])
    )
    listed = catalog.list_installed()["data"]
    assert listed["count"] == 2
    one = catalog.list_installed(engine="piper")["data"]
    assert one["count"] == 2
    got = catalog.get_installed_voice("piper", "bella")
    assert got["success"] is True
    assert got["data"]["voice"]["voice_display_name"] == "Bella (Warm)"
    assert catalog.get_installed_voice("piper", "nope")["success"] is False


def test_record_voice_usage(catalog: VoiceStoreManager) -> None:
    _install_alan(catalog)
    first = catalog.record_voice_usage("piper", "alan")
    second = catalog.record_voice_usage("piper", "alan")
    assert first["data"]["total_uses"] == 1
    assert second["data"]["total_uses"] == 2
    row = catalog.db.db.fetch_one(
        "SELECT last_used_at FROM installed_voices WHERE voice_name = 'alan'"
    )
    assert row["last_used_at"] is not None
    assert catalog.record_voice_usage("piper", "nope")["success"] is False


def test_set_voice_enabled(catalog: VoiceStoreManager) -> None:
    result, _ = _install_alan(catalog)
    iid = result["data"]["installed_id"]
    catalog.set_voice_enabled(iid, False)
    row = catalog.db.db.fetch_one(
        "SELECT is_enabled FROM installed_voices WHERE id = ?", (iid,)
    )
    assert int(row["is_enabled"]) == 0
    catalog.set_voice_enabled(iid, True)
    row = catalog.db.db.fetch_one(
        "SELECT is_enabled FROM installed_voices WHERE id = ?", (iid,)
    )
    assert int(row["is_enabled"]) == 1
    assert catalog.set_voice_enabled("nope", True)["success"] is False


def test_store_stats(catalog: VoiceStoreManager) -> None:
    _install_alan(catalog)
    stats = catalog.get_store_stats()["data"]
    assert stats["catalog_size"] == 3
    assert stats["installed_count"] == 1
    assert stats["installed_by_engine"] == {"piper": 1}
    assert stats["featured_count"] == 1
