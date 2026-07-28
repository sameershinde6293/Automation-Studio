"""Drive upload engine: resumable Google Drive backup for final MP4s.

Optional BaseModule (registry priority 20, CAN BE DISABLED: YES). Built
from File 12 (future scope: "Cloud backup (optional, user controlled)")
as pipeline phase D.7. After thumbnails, the orchestrator calls
``upload_final_render`` so a finished documentary lands in the user's
Google Drive folder without leaving this module.

Design decisions (honesty-first, this app is offline by default):

* NO google-api-python-client dependency. The whole flow is the Drive
  REST v3 resumable-upload protocol over ``requests`` (already a core
  requirement) plus a service-account OAuth2 bearer exchange signed
  with ``cryptography`` (already shipped for the license manager).
  Nothing new to install for the frozen exe.
* OFFLINE IS NORMAL (RULE 7). Every network failure becomes a failed
  response object — never an exception. The stage is optional, so a
  render without internet still completes; the upload self-skips or
  degrades to a pipeline warning with a resumable state file behind it.
* RESUMABLE. Google issues a session URI; confirmed byte offsets are
  persisted to a state JSON after every 308 chunk reply. A crash,
  cancelled run, or dead WiFi resumes from the last confirmed byte
  via ``resume_pending`` (Settings page button) or the next render
  when ``resume_pending_on_run`` is on.
* USER-CONTROLLED. Uploads happen only when ``config/drive_upload.json``
  says ``enabled: true`` AND a service-account JSON exists. Scope is
  the minimal ``drive.file`` (only files this app created).
* TEST SEAM. ``_request`` is the single network funnel; unit tests
  swap in scripted replies (including localhost test endpoints via
  ``drive_endpoints``) so no test ever touches the real internet —
  localhost/127.0.0.1 literals are pinned behind that setting only.

RULE 1: imports no other module. The orchestrator (core/core_engine.py)
owns the pipeline seam; the UI reaches this module through
``engine.module("drive_upload_engine")`` (viewmodel seam) only.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.safe_io import LazyModule, atomic_write_json, ensure_directory
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

requests = LazyModule("requests")

MODULE_NAME = "drive_upload_engine"

_TOKEN_URL_DEFAULT = "https://oauth2.googleapis.com/token"
_UPLOAD_URL_DEFAULT = "https://www.googleapis.com/upload/drive/v3/files"
_FILES_URL_DEFAULT = "https://www.googleapis.com/drive/v3/files"
_SCOPE = "https://www.googleapis.com/auth/drive.file"
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_TOKEN_REFRESH_MARGIN = 60  # re-login this many seconds before expiry

_CHUNK_QUANTUM = 256 * 1024  # Drive resumable chunks: 256 KiB multiple
_CHUNK_DEFAULT = 8 * 1024 * 1024
_CHUNK_MAX = 64 * 1024 * 1024
_MAX_SESSION_RESTARTS = 1  # begin a fresh session this often per file

_STATE_DIR = "cache/drive_upload_state"
_STATE_SUFFIX = ".upload.json"

_DEFAULT_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

_LOCAL_HOST_HINTS = ("127.0.0.1", "localhost", "::1")


class _OfflineError(Exception):
    """Network-level failure: no route, DNS, timeout, refused."""


class _UploadError(Exception):
    """Protocol-level failure: unexpected status or malformed reply."""


class _SessionExpiredError(Exception):
    """The resumable session URI is gone (404); restart from scratch."""


class _HttpReply:
    """Minimal response shape shared by the real funnel and test fakes."""

    def __init__(
        self,
        status_code: int,
        headers: Optional[Dict[str, str]] = None,
        text: str = "",
    ) -> None:
        self.status_code = int(status_code)
        self.headers = dict(headers or {})
        self.text = text

    def json(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.text or "{}")
        except ValueError as exc:
            raise _UploadError(
                f"invalid JSON from Google (HTTP {self.status_code})"
            ) from exc
        return data if isinstance(data, dict) else {}

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive single header lookup."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return None


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _b64url(data: bytes) -> str:
    """Base64url without padding (JWT segments)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _clamp_chunk(raw: Any) -> int:
    """Chunk size in bytes: 256 KiB multiple, [256 KiB, 64 MiB]."""
    try:
        value = int(float(raw) * 1024 * 1024)
    except (TypeError, ValueError):
        return _CHUNK_DEFAULT
    value = max(_CHUNK_QUANTUM, min(value, _CHUNK_MAX))
    return (value // _CHUNK_QUANTUM) * _CHUNK_QUANTUM


def _positive_timeout(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class DriveUploadEngine(BaseModule):
    """Resumable Google Drive uploads for rendered documentaries."""

    def __init__(
        self, container: ServiceContainer, module_name: str = MODULE_NAME
    ) -> None:
        super().__init__(container, module_name)
        self._token_cache: Optional[Tuple[str, int]] = None

    def is_optional_module(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def upload_status(self) -> Dict[str, Any]:
        """Advisory settings-page snapshot; never fails."""
        started = time.perf_counter()
        settings = self._settings()
        creds = self._credentials_path(settings)
        creds_exists = bool(creds) and creds.is_file()
        if not requests:
            reason = "python package 'requests' is not installed"
        elif not settings["enabled"]:
            reason = "disabled in config/drive_upload.json"
        elif not creds_exists:
            reason = f"credentials file not found: {creds}"
        else:
            reason = ""
        return self.make_response(
            True,
            {
                "enabled": settings["enabled"],
                "configured": bool(
                    settings["enabled"] and creds_exists and requests
                ),
                "pending_uploads": len(self._pending_states(settings)),
                "chunk_bytes": settings["chunk_bytes"],
                "folder_id": settings["folder_id"] or None,
                "credentials_file": str(creds) if creds else None,
                "requests_installed": bool(requests),
                "endpoints_local": self._using_local_endpoints(settings),
                "reason": reason,
            },
            duration_ms=_ms(started),
        )

    def upload_file(
        self, file_path: Any, remote_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload one file to the configured Drive folder (resumable).

        Honest outcomes, never raises:

        * disabled / not configured -> success with data["skipped"]
        * missing file / credentials -> failed response (RULE 7)
        * offline or Drive error mid-flight -> failed response AND a
          persisted session state, resumable via resume_pending()
        * success -> data has file_id / web_view_link / bytes sent
        """
        started = time.perf_counter()
        settings = self._settings()
        if not settings["enabled"]:
            return self.make_response(
                True,
                {"skipped": "drive upload disabled (config)",
                 "enabled": False},
                duration_ms=_ms(started),
            )
        if not requests:
            return self.make_response(
                False,
                error="drive upload unavailable: python package"
                " 'requests' is not installed",
                duration_ms=_ms(started),
            )
        path = Path(str(file_path or ""))
        if not path.is_file():
            return self.make_response(
                False,
                error=f"file not found: {path}",
                duration_ms=_ms(started),
            )
        size = path.stat().st_size
        if size <= 0:
            return self.make_response(
                False,
                error=f"refusing to upload a 0-byte file: {path}",
                duration_ms=_ms(started),
            )
        creds, creds_error = self._load_credentials(settings)
        if creds is None:
            return self.make_response(
                False, error=creds_error, duration_ms=_ms(started)
            )

        name = remote_name or path.name
        if self._using_local_endpoints(settings):
            self.log.warning(
                "drive endpoints point at localhost - test config only"
            )
        state = self._load_state(settings, path) or {}
        try:
            token = self._access_token(settings, creds)
            final: Dict[str, Any] = {}
            restarts = 0
            while True:
                try:
                    final = self._run_session(
                        settings, token, path, name, size, state
                    )
                    break
                except _SessionExpiredError:
                    self._clear_state(settings, path)
                    state = {}
                    token = self._access_token(settings, creds)
                    restarts += 1
                    if restarts > _MAX_SESSION_RESTARTS:
                        raise _UploadError(
                            "resumable session expired repeatedly;"
                            " giving up for this run"
                        )
            self._clear_state(settings, path)
            links = self._fetch_links(settings, token, final.get("id"))
        except (_OfflineError, _UploadError) as exc:
            self.log.warning("drive upload incomplete: %s", exc)
            return self.make_response(
                False,
                data={"pending_state": self._has_state(settings, path)},
                error=str(exc),
                duration_ms=_ms(started),
            )

        file_id = final.get("id")
        data = {
            "file_id": file_id,
            "name": final.get("name") or name,
            "size_bytes": size,
            "web_view_link": links.get("webViewLink"),
            "drive_folder_id": settings["folder_id"] or None,
        }
        try:
            self.event_bus.publish(
                "drive.upload_completed",
                {"file_id": file_id, "name": data["name"],
                 "size_bytes": size},
            )
        except Exception:  # noqa: BLE001 - event fan-out is advisory
            pass
        return self.make_response(True, data, duration_ms=_ms(started))

    def upload_final_render(
        self,
        output_path: Any,
        project_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pipeline seam: upload the finished MP4 (+ resume stragglers).

        Any skipped state is reported as success with data["skipped"]
        so an offline or unconfigured machine never hard-fails a render
        at the final, optional stage.
        """
        started = time.perf_counter()
        settings = self._settings()
        if not settings["enabled"]:
            return self.make_response(
                True,
                {"skipped": "drive upload disabled (config)",
                 "enabled": False},
                duration_ms=_ms(started),
            )
        if output_path is None or not str(output_path).strip():
            return self.make_response(
                True,
                {"skipped": "no rendered file to upload"},
                duration_ms=_ms(started),
            )
        resumed: Dict[str, Any] = {}
        if settings["resume_pending_on_run"]:
            reply = self.resume_pending()
            if reply.get("success"):
                resumed = reply.get("data") or {}
        result = self.upload_file(output_path)
        data = dict(result.get("data") or {})
        if project_id:
            data["project_id"] = str(project_id)
        if resumed:
            data["resumed_before_upload"] = resumed.get("resumed", 0)
        return self.make_response(
            bool(result.get("success")),
            data,
            error=result.get("error"),
            warnings=result.get("warnings"),
            duration_ms=_ms(started),
        )

    def resume_pending(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """Resume every persisted upload session, oldest first."""
        started = time.perf_counter()
        settings = self._settings()
        if not settings["enabled"]:
            return self.make_response(
                True,
                {"skipped": "drive upload disabled (config)",
                 "enabled": False, "resumed": 0},
                duration_ms=_ms(started),
            )
        states = self._pending_states(settings)
        if isinstance(limit, int) and limit > 0:
            states = states[:limit]
        results: List[Dict[str, Any]] = []
        resumed = 0
        for state in states:
            reply = self.upload_file(
                state.get("path"), remote_name=state.get("name")
            )
            ok = bool(reply.get("success"))
            resumed += 1 if ok else 0
            results.append(
                {"path": state.get("path"), "success": ok,
                 "error": reply.get("error")}
            )
        return self.make_response(
            True,
            {
                "attempted": len(results),
                "resumed": resumed,
                "results": results,
                "pending_remaining": len(self._pending_states(settings)),
            },
            warnings=[
                f"resume failed for {r['path']}: {r['error']}"
                for r in results
                if not r["success"]
            ],
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Configuration (RULE 8: every read is type-checked, defaults win)
    # ------------------------------------------------------------------
    def _settings(self) -> Dict[str, Any]:
        try:
            raw = self.config.get_config("drive_upload")
        except Exception:  # noqa: BLE001 - config read must not kill UI
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        endpoints = raw.get("drive_endpoints")
        endpoints = endpoints if isinstance(endpoints, dict) else {}
        mime_extra = raw.get("mime_types")
        mime_types = dict(_DEFAULT_MIME_TYPES)
        if isinstance(mime_extra, dict):
            for key, value in mime_extra.items():
                mime_types[str(key).lower()] = str(value)
        return {
            "enabled": bool(raw.get("enabled", False)),
            "credentials_file": str(
                raw.get("credentials_file")
                or "config/drive_service_account.json"
            ),
            "folder_id": str(raw.get("folder_id") or "").strip(),
            "chunk_bytes": _clamp_chunk(raw.get("chunk_size_mb", 8)),
            "resume_pending_on_run": bool(
                raw.get("resume_pending_on_run", True)
            ),
            "connect_timeout": _positive_timeout(
                raw.get("connect_timeout_seconds", 10), 10
            ),
            "read_timeout": _positive_timeout(
                raw.get("read_timeout_seconds", 120), 120
            ),
            "token_uri": str(
                endpoints.get("token_uri") or _TOKEN_URL_DEFAULT
            ),
            "upload_url": str(
                endpoints.get("upload_url") or _UPLOAD_URL_DEFAULT
            ),
            "files_url": str(
                endpoints.get("files_url") or _FILES_URL_DEFAULT
            ),
            "mime_types": mime_types,
        }

    @staticmethod
    def _using_local_endpoints(settings: Dict[str, Any]) -> bool:
        """True when any endpoint is a localhost test double."""
        for key in ("token_uri", "upload_url", "files_url"):
            url = str(settings.get(key) or "")
            if any(hint in url for hint in _LOCAL_HOST_HINTS):
                return True
        return False

    def _project_root(self) -> Path:
        """Root for relative paths: parent of the config folder."""
        folder = getattr(self.config, "config_folder", None)
        try:
            if folder:
                return Path(str(folder)).resolve().parent
        except (TypeError, ValueError, OSError):
            pass
        return Path.cwd()

    def _resolve(self, root: Path, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else root / path

    def _credentials_path(self, settings: Dict[str, Any]) -> Path:
        return self._resolve(self._project_root(),
                             settings["credentials_file"])

    def _load_credentials(
        self, settings: Dict[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Read the service-account JSON: (creds, error) - RULE 7/8."""
        path = self._credentials_path(settings)
        if not path.is_file():
            return None, f"credentials file not found: {path}"
        try:
            creds = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"credentials JSON unreadable: {exc}"
        if not isinstance(creds, dict):
            return None, f"credentials JSON is not an object: {path}"
        missing = [
            key
            for key in ("client_email", "private_key")
            if not creds.get(key)
        ]
        if missing:
            return None, (
                f"credentials missing field(s) {missing}: {path}"
            )
        return creds, None

    # ------------------------------------------------------------------
    # Service-account OAuth2 (JWT bearer, RS256 via cryptography)
    # ------------------------------------------------------------------
    def _service_account_jwt(
        self, settings: Dict[str, Any], creds: Dict[str, Any]
    ) -> str:
        """Build + sign the JWT assertion for the token exchange."""
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key,
            )
        except ImportError as exc:
            raise _UploadError(
                "cryptography package unavailable for service-account"
                " signing"
            ) from exc
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": str(creds.get("client_email")),
            "scope": _SCOPE,
            "aud": str(creds.get("token_uri") or settings["token_uri"]),
            "iat": now,
            "exp": now + 3600,
        }
        try:
            key = load_pem_private_key(
                str(creds["private_key"]).encode("utf-8"), password=None
            )
        except (ValueError, TypeError) as exc:
            raise _UploadError(
                f"credentials private_key unreadable: {exc}"
            ) from exc
        signing_input = (
            f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}"
            f".{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
        )
        signature = key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_b64url(signature)}"

    def _access_token(
        self, settings: Dict[str, Any], creds: Dict[str, Any]
    ) -> str:
        """Bearer token, cached until 60s before expiry."""
        cached = self._token_cache
        now = int(time.time())
        if cached and cached[1] - _TOKEN_REFRESH_MARGIN > now:
            return cached[0]
        assertion = self._service_account_jwt(settings, creds)
        reply = self._request(
            "POST",
            settings["token_uri"],
            data={"grant_type": _GRANT_TYPE, "assertion": assertion},
            headers={"Content-Type":
                     "application/x-www-form-urlencoded"},
            settings=settings,
        )
        if reply.status_code != 200:
            raise _UploadError(
                f"token exchange failed (HTTP {reply.status_code}):"
                f" {reply.text[:160]}"
            )
        payload = reply.json()
        token = payload.get("access_token")
        if not token:
            raise _UploadError("token exchange returned no access_token")
        ttl = _positive_timeout(payload.get("expires_in"), 3600)
        self._token_cache = (str(token), now + ttl)
        return str(token)

    # ------------------------------------------------------------------
    # Resumable session machinery
    # ------------------------------------------------------------------
    def _begin_session(
        self,
        settings: Dict[str, Any],
        token: str,
        name: str,
        size: int,
        mime: str,
    ) -> str:
        """POST metadata -> Drive session URI (Location header)."""
        metadata: Dict[str, Any] = {"name": name, "mimeType": mime}
        if settings["folder_id"]:
            metadata["parents"] = [settings["folder_id"]]
        reply = self._request(
            "POST",
            settings["upload_url"],
            params={"uploadType": "resumable",
                    "supportsAllDrives": "true"},
            json_body=metadata,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            },
            settings=settings,
        )
        if reply.status_code != 200:
            raise _UploadError(
                f"could not begin resumable session"
                f" (HTTP {reply.status_code}): {reply.text[:160]}"
            )
        location = reply.header("Location")
        if not location:
            raise _UploadError(
                "resumable session reply had no Location header"
            )
        return location

    def _probe_session(
        self, settings: Dict[str, Any], session_uri: str, size: int
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """Ask Drive how many bytes it holds: (confirmed, final)."""
        reply = self._request(
            "PUT",
            session_uri,
            data=b"",
            headers={"Content-Range": f"bytes */{size}"},
            settings=settings,
        )
        if reply.status_code in (200, 201):
            return size, reply.json()
        if reply.status_code == 404:
            raise _SessionExpiredError("session URI no longer exists")
        if reply.status_code != 308:
            raise _UploadError(
                f"session status probe failed (HTTP {reply.status_code})"
            )
        ranged = self._range_offset(reply)
        # A 308 with no Range means Drive holds nothing yet.
        return (ranged if ranged is not None else 0), None

    def _run_session(
        self,
        settings: Dict[str, Any],
        token: str,
        path: Path,
        name: str,
        size: int,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resume-or-begin a session and stream every remaining chunk."""
        mime = settings["mime_types"].get(
            path.suffix.lower(), "application/octet-stream"
        )
        session_uri = str(state.get("session_uri") or "")
        confirmed = int(state.get("bytes_sent") or 0)
        if session_uri:
            confirmed, final = self._probe_session(
                settings, session_uri, size
            )
            if final is not None:
                return final
        if not session_uri:
            session_uri = self._begin_session(
                settings, token, name, size, mime
            )
            confirmed = 0
        chunk_bytes = settings["chunk_bytes"]
        with path.open("rb") as handle:
            while confirmed < size:
                handle.seek(confirmed)
                chunk = handle.read(min(chunk_bytes, size - confirmed))
                end = confirmed + len(chunk) - 1
                reply = self._request(
                    "PUT",
                    session_uri,
                    data=chunk,
                    headers={
                        "Content-Range":
                            f"bytes {confirmed}-{end}/{size}",
                        "Content-Type": mime,
                    },
                    settings=settings,
                )
                if reply.status_code in (200, 201):
                    return reply.json()
                if reply.status_code == 308:
                    ranged = self._range_offset(reply)
                    if ranged is None:
                        self.log.warning(
                            "308 reply without Range header; assuming"
                            " chunk accepted"
                        )
                        confirmed = end + 1
                    else:
                        confirmed = ranged
                    state.update(
                        {
                            "path": str(path),
                            "name": name,
                            "session_uri": session_uri,
                            "size_bytes": size,
                            "bytes_sent": confirmed,
                            "mime_type": mime,
                            "updated_at": utc_now_str(),
                        }
                    )
                    self._save_state(settings, state)
                    continue
                if reply.status_code == 404:
                    raise _SessionExpiredError(
                        "session URI expired mid-upload"
                    )
                if reply.status_code >= 500:
                    raise _OfflineError(
                        f"Google Drive server error"
                        f" (HTTP {reply.status_code});"
                        f" upload will resume later"
                    )
                raise _UploadError(
                    f"upload chunk rejected (HTTP {reply.status_code}):"
                    f" {reply.text[:160]}"
                )
        return {}

    @staticmethod
    def _range_offset(reply: _HttpReply) -> Optional[int]:
        """Confirmed byte count from a 308 Range header (bytes=0-N).

        Returns None when the header is absent or unparsable so callers
        can apply their own documented fallback.
        """
        header = str(reply.header("Range") or "")
        if "=" in header and "-" in header:
            tail = header.split("=", 1)[1].split("-", 1)[1]
            try:
                return int(tail) + 1
            except ValueError:
                return None
        return None

    def _fetch_links(
        self,
        settings: Dict[str, Any],
        token: str,
        file_id: Optional[str],
    ) -> Dict[str, Any]:
        """Best-effort webViewLink lookup; failure loses the link only."""
        if not file_id:
            return {}
        try:
            reply = self._request(
                "GET",
                f"{settings['files_url']}/{file_id}",
                params={"fields": "id,name,webViewLink",
                        "supportsAllDrives": "true"},
                headers={"Authorization": f"Bearer {token}"},
                settings=settings,
            )
        except _OfflineError:
            return {}
        if reply.status_code != 200:
            return {}
        return reply.json()

    # ------------------------------------------------------------------
    # The single network funnel (tests replace this; nothing else talks)
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Any = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> _HttpReply:
        """One HTTP call through requests with bounded timeouts.

        The localhost/127.0.0.1 literals reachable via
        ``drive_endpoints`` exist for offline integration tests only;
        production config keeps the googleapis defaults.
        """
        if not requests:
            raise _OfflineError(
                "python package 'requests' is not installed"
            )
        if settings:
            timeout = (
                int(settings["connect_timeout"]),
                int(settings["read_timeout"]),
            )
        else:
            timeout = (10, 120)
        try:
            response = requests.request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise _OfflineError(
                f"offline or Google Drive unreachable ({exc})"
            ) from exc
        return _HttpReply(
            response.status_code, dict(response.headers), response.text
        )

    # ------------------------------------------------------------------
    # Resume-state persistence (JSON per file, RULE 3: path via config)
    # ------------------------------------------------------------------
    def _state_dir(self, settings: Dict[str, Any]) -> Path:
        return self._project_root() / _STATE_DIR

    def _state_path(self, settings: Dict[str, Any], path: Path) -> Path:
        key = base64.urlsafe_b64encode(
            str(path).encode("utf-8")
        ).decode("ascii").rstrip("=")[:64]
        return self._state_dir(settings) / f"{key}{_STATE_SUFFIX}"

    def _load_state(
        self, settings: Dict[str, Any], path: Path
    ) -> Optional[Dict[str, Any]]:
        state_path = self._state_path(settings, path)
        if not state_path.is_file():
            return None
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if int(data.get("size_bytes") or -1) != path.stat().st_size:
            return None  # file changed since the session began
        return data

    def _save_state(self, settings: Dict[str, Any],
                    state: Dict[str, Any]) -> None:
        # PHASE 9: the resume state is written atomically. A truncated
        # state file used to be indistinguishable from a valid one until
        # the next resume tried to parse it, at which point the upload
        # restarted from zero.
        try:
            folder = self._state_dir(settings)
        except OSError as exc:
            self.log.warning("could not resolve upload state folder: %s", exc)
            return
        if ensure_directory(folder) is None:
            return
        atomic_write_json(
            self._state_path(settings, Path(str(state.get("path")))), state
        )

    def _clear_state(self, settings: Dict[str, Any], path: Path) -> None:
        try:
            self._state_path(settings, path).unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning("could not clear upload state: %s", exc)

    def _has_state(self, settings: Dict[str, Any], path: Path) -> bool:
        return self._state_path(settings, path).is_file()

    def _pending_states(self, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
        folder = self._state_dir(settings)
        states: List[Dict[str, Any]] = []
        if not folder.is_dir():
            return states
        for state_path in sorted(folder.glob(f"*{_STATE_SUFFIX}")):
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("path"):
                states.append(data)
        states.sort(key=lambda s: str(s.get("updated_at") or ""))
        return states
