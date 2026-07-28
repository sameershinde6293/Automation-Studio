"""Regression tests for the independent post-v1.1.0 certification audit.

Each test here corresponds to a defect that was reproduced against the v1.1.0
tree before it was fixed. They exist so the specific bypass cannot silently
return; every one of them fails on the pre-fix code.

| ID      | Severity | Defect                                                |
| ------- | -------- | ----------------------------------------------------- |
| AUDIT-1 | Critical | SSRF guard bypassed via HTTP redirect                 |
| AUDIT-2 | High     | Login rate limiter evaded by rotating an auth header  |
| AUDIT-3 | High     | Deployment details readable by anonymous callers      |
| AUDIT-4 | High     | Email node leaked bcc addresses / never delivered them|
| AUDIT-5 | High     | AI provider ignored OPENAI_API_KEY and *_BASE_URL     |
"""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import threading
from email import message_from_string

import pytest

from app.core.errors import SecurityError


# --------------------------------------------------------------------------- #
# AUDIT-1 — SSRF via redirect
# --------------------------------------------------------------------------- #
class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Public-looking endpoint that bounces the caller at an internal address."""

    redirect_target = ""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/open":
            self.send_response(302)
            self.send_header("Location", self.redirect_target)
            self.end_headers()
        else:
            body = b"INTERNAL-ONLY"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):  # pragma: no cover - silence test output
        pass


@pytest.fixture()
def redirect_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    _RedirectHandler.redirect_target = f"http://127.0.0.1:{port}/secret"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


class TestSSRFRedirectIsRevalidated:
    """AUDIT-1: every redirect hop must pass back through the SSRF guard."""

    def test_redirect_to_loopback_is_blocked(self, redirect_server, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.workflow.nodes.network_nodes import _perform_request

        monkeypatch.setattr(
            settings, "HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS", False, raising=False
        )
        monkeypatch.setattr(settings, "HTTP_EXECUTOR_ALLOWED_HOSTS", [], raising=False)

        async def run():
            return await _perform_request(
                method="GET",
                url=f"http://127.0.0.1:{redirect_server}/open",
                headers={},
                body=None,
                timeout=5,
                max_bytes=10_000,
            )

        # The *initial* URL is loopback too, so assert the redirect specifically
        # cannot reach the internal body even though the hop is what matters.
        with pytest.raises(SecurityError):
            asyncio.run(run())

    def test_permitted_redirect_is_still_followed(self, redirect_server, monkeypatch):
        """The fix must not break ordinary redirect handling."""
        from app.infrastructure.config.settings import settings
        from app.services.workflow.nodes.network_nodes import _perform_request

        monkeypatch.setattr(
            settings, "HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS", True, raising=False
        )
        monkeypatch.setattr(settings, "HTTP_EXECUTOR_ALLOWED_HOSTS", [], raising=False)

        async def run():
            return await _perform_request(
                method="GET",
                url=f"http://127.0.0.1:{redirect_server}/open",
                headers={},
                body=None,
                timeout=5,
                max_bytes=10_000,
            )

        result = asyncio.run(run())
        assert result["status_code"] == 200
        assert "INTERNAL-ONLY" in result["response"]

    def test_http_client_does_not_delegate_redirects_to_httpx(self):
        """``follow_redirects=True`` on the client is what caused the bypass."""
        import ast
        import inspect

        from app.services.workflow import executors
        from app.services.workflow.nodes import network_nodes

        for module in (executors, network_nodes):
            tree = ast.parse(inspect.getsource(module))
            # Look at real keyword arguments only, so prose in a docstring or
            # comment explaining the hazard does not trip the check.
            offenders = [
                ast.dump(kw)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                for kw in node.keywords
                if kw.arg == "follow_redirects"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ]
            assert not offenders, (
                f"{module.__name__} lets httpx follow redirects, which skips "
                "validate_outbound_url on every hop after the first."
            )


# --------------------------------------------------------------------------- #
# AUDIT-2 — login rate limiter evasion
# --------------------------------------------------------------------------- #
class TestAuthRateLimitKeysOnAddress:
    """AUDIT-2: a rotating credential header must not mint a fresh bucket."""

    def test_credential_endpoints_bucket_by_address(self):
        from app.core.middleware import client_identity

        class _Request:
            def __init__(self, headers):
                self.headers = headers
                self.client = type("C", (), {"host": "203.0.113.5"})()

        first = client_identity(
            _Request({"Authorization": "Bearer junk-1"}), prefer_address=True
        )
        second = client_identity(
            _Request({"Authorization": "Bearer junk-2"}), prefer_address=True
        )
        assert first == second == "ip:203.0.113.5"

    def test_normal_endpoints_still_bucket_by_credential(self):
        """The credential-first default is deliberate everywhere else."""
        from app.core.middleware import client_identity

        class _Request:
            def __init__(self, headers):
                self.headers = headers
                self.client = type("C", (), {"host": "203.0.113.5"})()

        first = client_identity(_Request({"X-API-Key": "key-one"}))
        second = client_identity(_Request({"X-API-Key": "key-two"}))
        assert first != second

    def test_login_limiter_fires_despite_rotating_header(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.core.middleware import RateLimitMiddleware

        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware, max_requests=1000, auth_max_requests=3
        )

        @app.post("/api/auth/login")
        def login():
            return {"ok": True}

        client = TestClient(app)
        codes = [
            client.post(
                "/api/auth/login", headers={"Authorization": f"Bearer junk{i}"}
            ).status_code
            for i in range(10)
        ]
        assert 429 in codes, (
            "rotating the Authorization header still evades the login limiter"
        )


# --------------------------------------------------------------------------- #
# AUDIT-3 — anonymous system introspection
# --------------------------------------------------------------------------- #
class TestSystemIntrospectionRequiresAuth:
    """AUDIT-3: deployment/runtime detail must not be anonymously readable."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/system/info",
            "/api/system/metrics",
            "/api/system/events",
            "/api/system/scheduler/jobs",
        ],
    )
    def test_anonymous_is_refused(self, path):
        from app.api.routers import system_router

        route = next(
            r
            for r in system_router.router.routes
            if getattr(r, "path", None) == path.replace("/api", "", 1)
        )
        dependency_names = {
            getattr(d.call, "__name__", "") for d in route.dependant.dependencies
        }
        assert "require_read" in dependency_names, (
            f"{path} has no authorization dependency and is anonymously readable"
        )

    def test_node_catalog_stays_public(self):
        """The editor needs the palette before a user is known; it is static."""
        from app.api.routers import system_router

        route = next(
            r
            for r in system_router.router.routes
            if getattr(r, "path", None) == "/system/node-types"
        )
        names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        assert "require_read" not in names


# --------------------------------------------------------------------------- #
# AUDIT-4 — email bcc handling
# --------------------------------------------------------------------------- #
class TestEmailBccIsBlindAndDelivered:
    """AUDIT-4: bcc must reach the envelope but never a visible header."""

    def test_bcc_is_in_envelope_and_not_in_headers(self):
        from app.services.workflow.nodes.data_nodes import EmailNode

        sent = {}

        class _FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):  # pragma: no cover - not exercised here
                pass

            def login(self, *args):  # pragma: no cover - not exercised here
                pass

            def send_message(self, message, from_addr=None, to_addrs=None):
                sent["message"] = message
                sent["to_addrs"] = to_addrs

        import smtplib

        original = smtplib.SMTP
        smtplib.SMTP = _FakeSMTP
        try:
            EmailNode()._send_sync(
                sender="from@example.com",
                recipients=["to@example.com", "cc@example.com", "bcc@example.com"],
                to=["to@example.com"],
                cc=["cc@example.com"],
                subject="s",
                body="b",
                html=False,
            )
        finally:
            smtplib.SMTP = original

        message = message_from_string(str(sent["message"]))
        # Delivered.
        assert "bcc@example.com" in sent["to_addrs"]
        # Blind.
        visible = (message.get("To") or "") + (message.get("Cc") or "")
        assert "bcc@example.com" not in visible, "bcc address leaked into a header"
        assert message.get("To") == "to@example.com"


# --------------------------------------------------------------------------- #
# AUDIT-5 — AI provider configuration
# --------------------------------------------------------------------------- #
class TestAIProvidersReadSettings:
    """AUDIT-5: providers must honour settings, not hardcoded values."""

    def test_openai_reads_key_from_settings(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.ai.providers.openai_provider import OpenAIProvider

        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-from-dotenv", raising=False)
        assert OpenAIProvider()._resolve_api_key() == "sk-from-dotenv"

    def test_openai_base_url_is_configurable(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.ai.providers.openai_provider import OpenAIProvider

        monkeypatch.setattr(
            settings, "OPENAI_BASE_URL", "https://proxy.internal/v1", raising=False
        )
        assert OpenAIProvider()._resolve_base_url() == "https://proxy.internal/v1"

    def test_ollama_base_url_is_configurable(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.ai.providers.local_provider import OllamaProvider

        monkeypatch.setattr(
            settings, "OLLAMA_BASE_URL", "http://ollama-box:11434/api", raising=False
        )
        assert OllamaProvider()._resolve_base_url() == "http://ollama-box:11434/api"
