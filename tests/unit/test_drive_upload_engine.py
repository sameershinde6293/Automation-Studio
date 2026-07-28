"""Unit tests for modules.drive_upload_engine (D.7).

The Drive REST v3 resumable protocol is pinned end-to-end OFFLINE:
every test overrides the module's single network funnel
(``engine._request``) with a scripted FakeTransport that simulates the
server (token exchange, session begin, chunk 308s, Range accounting,
drops and expiry). Settings always point ``drive_endpoints`` at
localhost literals so a missed override fails loudly — no test can ever
reach googleapis.com. The RS256 service-account JWT is verified with
the real public key, not mocked away.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.service_container import ServiceContainer
from modules.drive_upload_engine import (
    _CHUNK_MAX,
    _CHUNK_QUANTUM,
    _HttpReply,
    _OfflineError,
    DriveUploadEngine,
)

LOCAL = "http://127.0.0.1:9"
CHUNK = _CHUNK_QUANTUM  # 256 KiB quantum keeps fixture files small


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


class FakeTransport:
    """Scriptable Drive stand-in for engine._request."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.store = 0  # bytes the "server" holds for the session
        self.offline = False
        self.fail_on_chunk: Optional[int] = None  # chunk index to drop
        self.expire_probe = False  # probe answers 404 (session gone)
        self.rangeless_308 = False  # malformed 308 (no Range header)
        self._chunks = 0

    def __call__(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Any = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> _HttpReply:
        assert "googleapis.com" not in url, f"test leaked to real API: {url}"
        headers = dict(headers or {})
        self.calls.append(
            {"method": method, "url": url, "headers": headers,
             "data": data, "json": json_body, "params": params}
        )
        if self.offline:
            raise _OfflineError(
                "offline or Google Drive unreachable (simulated)"
            )
        if method == "POST" and url.endswith("/token"):
            return _HttpReply(
                200, {}, json.dumps(
                    {"access_token": "fake-token", "expires_in": 3600}
                )
            )
        if method == "POST" and params and params.get("uploadType"):
            self.store = 0  # a fresh session holds nothing server-side
            self._chunks = 0
            return _HttpReply(
                200, {"Location": f"{LOCAL}/session/abc"}, ""
            )
        if url.startswith(f"{LOCAL}/session"):
            return self._session_reply(headers, data)
        if method == "GET" and "/files/" in url:
            return _HttpReply(
                200, {}, json.dumps(
                    {"id": "file-1",
                     "webViewLink": "https://drive.test/view/file-1"}
                )
            )
        raise AssertionError(f"unexpected request {method} {url}")

    def _session_reply(
        self, headers: Dict[str, str], data: Any
    ) -> _HttpReply:
        content_range = str(headers.get("Content-Range") or "")
        if content_range.startswith("bytes */"):
            if self.expire_probe:
                return _HttpReply(404, {}, "session expired")
            if self.store:
                return _HttpReply(
                    308, {"Range": f"bytes=0-{self.store - 1}"}, ""
                )
            return _HttpReply(308, {}, "")
        start_s, rest = content_range.split(" ", 1)[1].split("-", 1)
        end_s, total_s = rest.split("/", 1)
        start, end, total = int(start_s), int(end_s), int(total_s)
        assert start == self.store, (
            f"client resumed at {start}, server holds {self.store}"
        )
        body_len = len(data) if isinstance(data, (bytes, bytearray)) else 0
        assert body_len == end - start + 1, "chunk size/Content-Range"
        if self.fail_on_chunk is not None and self._chunks >= (
            self.fail_on_chunk
        ):
            raise _OfflineError(
                "offline mid-chunk: connection dropped (simulated)"
            )
        self._chunks += 1
        self.store = end + 1
        if self.store >= total:
            return _HttpReply(
                200, {}, json.dumps({"id": "file-1", "name": "video.mp4"})
            )
        if self.rangeless_308:
            return _HttpReply(308, {}, "")
        return _HttpReply(308, {"Range": f"bytes=0-{end}"}, "")


@pytest.fixture
def creds(tmp_path: Path) -> Dict[str, Any]:
    """Real RSA keypair in a fake service-account JSON."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    path = tmp_path / "proj" / "config"
    path.mkdir(parents=True, exist_ok=True)
    creds_path = path / "drive_service_account.json"
    creds_path.write_text(
        json.dumps(
            {"client_email": "bot@project.iam.gserviceaccount.com",
             "private_key": pem, "token_uri": f"{LOCAL}/token"}
        ),
        encoding="utf-8",
    )
    return {"key": key, "path": creds_path}


def _settings_dict(
    creds: Dict[str, Any], enabled: bool = True, **overrides: Any
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "enabled": enabled,
        "credentials_file": str(creds["path"]),
        "folder_id": "folder-9",
        "chunk_size_mb": 0.25,
        "resume_pending_on_run": False,
        "drive_endpoints": {
            "token_uri": f"{LOCAL}/token",
            "upload_url": f"{LOCAL}/upload/files",
            "files_url": f"{LOCAL}/files",
        },
    }
    cfg.update(overrides)
    return cfg


def _engine(
    tmp_path: Path,
    settings: Optional[Dict[str, Any]] = None,
    transport: Optional[FakeTransport] = None,
) -> DriveUploadEngine:
    container = ServiceContainer.create_test_container()
    config = container.get("config")
    config.config_folder = tmp_path / "proj" / "config"
    config.get_config.return_value = settings or {}
    engine = DriveUploadEngine(container)
    if transport is not None:
        engine._request = transport
    return engine


def _video(tmp_path: Path, size: int, name: str = "video.mp4") -> Path:
    path = tmp_path / name
    pattern = bytes(range(256))
    body = (pattern * (size // 256 + 1))[:size]
    path.write_bytes(body)
    return path


# ------------------------------------------------------------------
# Status / configuration (RULE 7 + RULE 8)
# ------------------------------------------------------------------
def test_status_defaults_when_config_missing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    data = engine.upload_status()["data"]
    assert data["enabled"] is False
    assert data["configured"] is False
    assert data["pending_uploads"] == 0
    assert "disabled" in data["reason"]
    skipped = engine.upload_file("anything.mp4")
    assert skipped["success"] is True
    assert "disabled" in skipped["data"]["skipped"]


def test_status_reports_missing_credentials(tmp_path: Path) -> None:
    settings = _settings_dict({"path": tmp_path / "nope.json"})
    engine = _engine(tmp_path, settings)
    data = engine.upload_status()["data"]
    assert data["enabled"] is True
    assert data["configured"] is False
    assert "credentials file not found" in data["reason"]
    assert data["endpoints_local"] is True


def test_missing_credentials_fails_gracefully(tmp_path: Path) -> None:
    settings = _settings_dict({"path": tmp_path / "absent.json"})
    engine = _engine(tmp_path, settings, FakeTransport())
    video = _video(tmp_path, 1000)
    reply = engine.upload_file(video)
    assert reply["success"] is False
    assert "credentials" in reply["error"]


def test_malformed_credentials_json(tmp_path: Path, creds) -> None:
    creds["path"].write_text("{not json", encoding="utf-8")
    engine = _engine(tmp_path, _settings_dict(creds), FakeTransport())
    reply = engine.upload_file(_video(tmp_path, 1000))
    assert reply["success"] is False
    assert "unreadable" in reply["error"]


def test_chunk_size_clamped_to_quantum(tmp_path: Path, creds) -> None:
    engine = _engine(tmp_path, _settings_dict(creds, chunk_size_mb=0))
    assert engine._settings()["chunk_bytes"] == _CHUNK_QUANTUM
    engine = _engine(tmp_path, _settings_dict(creds, chunk_size_mb=999))
    assert engine._settings()["chunk_bytes"] == _CHUNK_MAX
    engine = _engine(tmp_path, _settings_dict(creds, chunk_size_mb="x"))
    assert engine._settings()["chunk_bytes"] == 8 * 1024 * 1024


# ------------------------------------------------------------------
# Guard rails: skip paths and file validation
# ------------------------------------------------------------------
def test_final_render_skips_when_disabled(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    reply = engine.upload_final_render("/out/v.mp4", project_id="p1")
    assert reply["success"] is True
    assert "disabled" in reply["data"]["skipped"]


def test_final_render_skips_without_output(tmp_path: Path, creds) -> None:
    engine = _engine(tmp_path, _settings_dict(creds), FakeTransport())
    reply = engine.upload_final_render(None)
    assert reply["success"] is True
    assert "no rendered file" in reply["data"]["skipped"]


def test_missing_file_is_graceful_failure(tmp_path: Path, creds) -> None:
    engine = _engine(tmp_path, _settings_dict(creds), FakeTransport())
    reply = engine.upload_file(tmp_path / "ghost.mp4")
    assert reply["success"] is False
    assert "file not found" in reply["error"]


def test_zero_byte_file_refused(tmp_path: Path, creds) -> None:
    engine = _engine(tmp_path, _settings_dict(creds), FakeTransport())
    empty = _video(tmp_path, 0, "empty.mp4")
    reply = engine.upload_file(empty)
    assert reply["success"] is False
    assert "0-byte" in reply["error"]


# ------------------------------------------------------------------
# Auth: JWT structure + RS256 verification with the real public key
# ------------------------------------------------------------------
def test_service_account_jwt_is_signed_rs256(
    tmp_path: Path, creds
) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    transport = FakeTransport()
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    reply = engine.upload_file(_video(tmp_path, 1000))
    assert reply["success"] is True
    token_posts = [
        c for c in transport.calls if c["url"].endswith("/token")
    ]
    assert len(token_posts) == 1
    assertion = token_posts[0]["data"]["assertion"]
    header_s, claims_s, signature_s = assertion.split(".")
    header = json.loads(_b64url_decode(header_s))
    claims = json.loads(_b64url_decode(claims_s))
    assert header["alg"] == "RS256"
    assert claims["iss"] == "bot@project.iam.gserviceaccount.com"
    assert claims["scope"].endswith("drive.file")
    assert claims["aud"] == f"{LOCAL}/token"
    assert claims["exp"] > claims["iat"] >= time.time() - 30
    creds["key"].public_key().verify(
        _b64url_decode(signature_s),
        f"{header_s}.{claims_s}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )  # raises InvalidSignature on any tampering


def test_token_is_cached_between_uploads(tmp_path: Path, creds) -> None:
    transport = FakeTransport()
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    assert engine.upload_file(_video(tmp_path, 1000, "a.mp4"))["success"]
    assert engine.upload_file(_video(tmp_path, 1000, "b.mp4"))["success"]
    token_posts = [
        c for c in transport.calls if c["url"].endswith("/token")
    ]
    assert len(token_posts) == 1


# ------------------------------------------------------------------
# Upload protocol: happy path, chunking, drops, resume, expiry
# ------------------------------------------------------------------
def test_small_file_uploads_in_one_chunk(tmp_path: Path, creds) -> None:
    transport = FakeTransport()
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    reply = engine.upload_file(_video(tmp_path, 1000))
    assert reply["success"] is True
    data = reply["data"]
    assert data["file_id"] == "file-1"
    assert data["web_view_link"] == "https://drive.test/view/file-1"
    assert data["size_bytes"] == 1000
    puts = [c for c in transport.calls if c["method"] == "PUT"]
    assert len(puts) == 1
    assert puts[0]["headers"]["Content-Range"] == "bytes 0-999/1000"


def test_offline_start_fails_without_crash(tmp_path: Path, creds) -> None:
    transport = FakeTransport()
    transport.offline = True
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    reply = engine.upload_file(_video(tmp_path, 1000))
    assert reply["success"] is False
    assert "offline" in reply["error"]
    assert reply["data"]["pending_state"] is False


def test_chunk_drop_persists_state_then_resumes(
    tmp_path: Path, creds
) -> None:
    video = _video(tmp_path, CHUNK * 2 + 123)
    transport = FakeTransport()
    transport.fail_on_chunk = 1  # drop on the second PUT chunk
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    reply = engine.upload_file(video)
    assert reply["success"] is False
    assert "offline" in reply["error"]
    assert reply["data"]["pending_state"] is True
    states = engine._pending_states(engine._settings())
    assert len(states) == 1
    assert states[0]["bytes_sent"] == CHUNK
    assert states[0]["session_uri"] == f"{LOCAL}/session/abc"

    resume = FakeTransport()
    resume.store = CHUNK  # server kept the first chunk
    engine._request = resume
    reply = engine.upload_file(video)
    assert reply["success"] is True
    put_ranges = [
        c["headers"]["Content-Range"]
        for c in resume.calls
        if c["method"] == "PUT"
        and not c["headers"]["Content-Range"].startswith("bytes */")
    ]
    assert put_ranges[0].startswith(f"bytes {CHUNK}-")
    assert engine._pending_states(engine._settings()) == []


def test_resume_pending_roundtrip(tmp_path: Path, creds) -> None:
    transport = FakeTransport()
    transport.offline = True
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    assert engine.resume_pending()["data"]["attempted"] == 0
    # plant an interrupted session (first chunk lands, second drops)
    transport.offline = False
    transport.fail_on_chunk = 1
    video = _video(tmp_path, CHUNK + 10)
    assert engine.upload_file(video)["success"] is False
    stats = engine.upload_status()["data"]
    assert stats["pending_uploads"] == 1
    resume = FakeTransport()  # server lost the chunk: store stays 0
    engine._request = resume
    result = engine.resume_pending()
    data = result["data"]
    assert data["attempted"] == 1 and data["resumed"] == 1
    assert data["pending_remaining"] == 0
    assert result["warnings"] == []


def test_expired_session_restarts_once_then_succeeds(
    tmp_path: Path, creds
) -> None:
    video = _video(tmp_path, 1000)
    transport = FakeTransport()
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    # plant a state with a session URI the server no longer knows
    engine._save_state(
        engine._settings(),
        {"path": str(video), "name": video.name,
         "session_uri": f"{LOCAL}/session/abc", "size_bytes": 1000,
         "bytes_sent": 500},
    )
    transport.expire_probe = True
    transport.store = 0
    reply = engine.upload_file(video)
    assert reply["success"] is True
    begins = [
        c for c in transport.calls
        if c["method"] == "POST" and c["params"]
        and c["params"].get("uploadType")
    ]
    assert len(begins) == 1  # one fresh session after the 404
    assert engine._pending_states(engine._settings()) == []


def test_rangeless_308_still_completes(tmp_path: Path, creds) -> None:
    transport = FakeTransport()
    transport.rangeless_308 = True
    engine = _engine(tmp_path, _settings_dict(creds), transport)
    reply = engine.upload_file(_video(tmp_path, CHUNK + 10))
    assert reply["success"] is True


def test_final_render_resumes_stragglers_first(
    tmp_path: Path, creds
) -> None:
    settings = _settings_dict(creds, resume_pending_on_run=True)
    transport = FakeTransport()
    transport.fail_on_chunk = 1  # first chunk lands, second drops
    engine = _engine(tmp_path, settings, transport)
    straggler = _video(tmp_path, CHUNK + 10, "old.mp4")
    assert engine.upload_file(straggler)["success"] is False
    resume = FakeTransport()
    engine._request = resume
    fresh = _video(tmp_path, 1000, "new.mp4")
    reply = engine.upload_final_render(fresh, project_id="p9")
    assert reply["success"] is True
    assert reply["data"]["resumed_before_upload"] == 1
    assert reply["data"]["project_id"] == "p9"
    assert engine._pending_states(engine._settings()) == []


# ------------------------------------------------------------------
# Pipeline seam: core_engine skip/warning semantics for this stage
# ------------------------------------------------------------------
class _StageStub:
    def __init__(self, outcome: Dict[str, Any]) -> None:
        self.enabled = True
        self.outcome = outcome
        self.calls: List[Dict[str, Any]] = []

    def upload_final_render(self, output, project_id=None, title=None):
        self.calls.append(
            {"output": output, "project_id": project_id, "title": title}
        )
        return dict(self.outcome)


def _pipeline_ctx(tmp_path: Path) -> Dict[str, Any]:
    return {
        "skip_stages": set(), "warnings": [], "project_id": "p1",
        "project_title": "Demo", "final_output": str(tmp_path / "v.mp4"),
    }


def test_stage_self_skip_marks_skipped_and_publishes(
    tmp_path: Path,
) -> None:
    from core.core_engine import CoreEngine

    container = ServiceContainer.create_test_container()
    engine = CoreEngine(
        container, module_loader=lambda name: None, auto_load=False
    )
    stub = _StageStub(
        {"success": True,
         "data": {"skipped": "drive upload disabled (config)"}}
    )
    engine._modules["drive_upload_engine"] = stub
    ctx = _pipeline_ctx(tmp_path)
    result = engine._execute_stage("drive_upload", ctx)
    assert result["status"] == "skipped"
    assert "disabled" in result["reason"]
    engine.event_bus.publish.assert_any_call(
        "pipeline.stage_skipped",
        {"stage": "drive_upload",
         "reason": "drive upload disabled (config)"},
    )
    assert stub.calls[0]["output"] == str(tmp_path / "v.mp4")
    assert stub.calls[0]["project_id"] == "p1"


def test_stage_optional_failure_degrades_to_warning(
    tmp_path: Path,
) -> None:
    from core.core_engine import CoreEngine

    container = ServiceContainer.create_test_container()
    engine = CoreEngine(
        container, module_loader=lambda name: None, auto_load=False
    )
    engine._modules["drive_upload_engine"] = _StageStub(
        {"success": False, "error": "offline or Drive unreachable"}
    )
    ctx = _pipeline_ctx(tmp_path)
    result = engine._execute_stage("drive_upload", ctx)
    assert result["status"] == "warning"  # optional stage, no abort
    assert any("drive_upload" in w for w in ctx["warnings"])


def test_stage_skips_cleanly_without_final_output(tmp_path: Path) -> None:
    from core.core_engine import CoreEngine

    container = ServiceContainer.create_test_container()
    engine = CoreEngine(
        container, module_loader=lambda name: None, auto_load=False
    )
    stub = _StageStub({"success": True, "data": {}})
    engine._modules["drive_upload_engine"] = stub
    ctx = _pipeline_ctx(tmp_path)
    ctx["final_output"] = None
    result = engine._execute_stage("drive_upload", ctx)
    assert result["status"] == "skipped"
    assert stub.calls == []  # handler short-circuits before the module
