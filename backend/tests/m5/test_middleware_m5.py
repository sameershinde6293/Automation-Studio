"""M5: CSRF, rate limiting, security headers, trusted hosts and correlation."""

from __future__ import annotations

import pytest

from app.core.middleware import client_identity


class _FakeRequest:
    """Minimal stand-in for a Starlette request, for keying logic only."""

    def __init__(self, headers=None, host="1.2.3.4"):
        self.headers = headers or {}

        class _Client:
            def __init__(self, h):
                self.host = h

        self.client = _Client(host) if host else None


class TestClientIdentity:
    def test_credential_is_preferred_over_address(self):
        """Two callers behind one NAT must not share a bucket."""
        first = client_identity(_FakeRequest({"X-API-Key": "key-one"}))
        second = client_identity(_FakeRequest({"X-API-Key": "key-two"}))
        assert first != second
        assert first.startswith("key:")

    def test_raw_credential_is_never_used_as_the_key(self):
        """The limiter's key ends up in logs, so it must not carry a secret."""
        key = client_identity(_FakeRequest({"X-API-Key": "super-secret-value"}))
        assert "super-secret-value" not in key

    def test_forwarded_header_is_ignored_by_default(self):
        """Honouring it unconditionally would let any client spoof its IP."""
        request = _FakeRequest({"X-Forwarded-For": "9.9.9.9"}, host="1.2.3.4")
        assert client_identity(request, trust_proxy=False) == "ip:1.2.3.4"

    def test_forwarded_header_is_used_when_proxy_is_trusted(self):
        request = _FakeRequest({"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}, host="1.2.3.4")
        assert client_identity(request, trust_proxy=True) == "ip:9.9.9.9"

    def test_missing_client_falls_back_safely(self):
        assert client_identity(_FakeRequest(host=None)) == "ip:unknown"


class TestSecurityHeaders:
    def test_defensive_headers_are_present(self, client):
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "Content-Security-Policy" in headers
        assert headers["Cross-Origin-Opener-Policy"] == "same-origin"

    def test_hsts_is_off_by_default(self, client):
        assert "Strict-Transport-Security" not in client.get("/health").headers

    def test_hsts_is_emitted_when_enabled(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "SECURITY_HSTS_ENABLED", True)
        headers = make_client().get("/health").headers
        assert "max-age=" in headers["Strict-Transport-Security"]


class TestRequestCorrelation:
    def test_request_id_is_generated_and_returned(self, client):
        headers = client.get("/health").headers
        assert headers["X-Request-ID"]
        assert "X-Response-Time-ms" in headers

    def test_supplied_request_id_is_echoed(self, client):
        response = client.get("/health", headers={"X-Request-ID": "abc123"})
        assert response.headers["X-Request-ID"] == "abc123"

    def test_correlation_id_defaults_to_the_request_id(self, client):
        headers = client.get("/health").headers
        assert headers["X-Correlation-ID"] == headers["X-Request-ID"]

    def test_correlation_id_is_preserved_across_requests(self, client):
        """One logical operation may span several requests."""
        first = client.get("/health", headers={"X-Correlation-ID": "op-42"})
        second = client.get("/api/system/info", headers={"X-Correlation-ID": "op-42"})
        assert first.headers["X-Correlation-ID"] == "op-42"
        assert second.headers["X-Correlation-ID"] == "op-42"
        assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]

    def test_absurdly_long_request_id_is_replaced(self, client):
        response = client.get("/health", headers={"X-Request-ID": "x" * 500})
        assert response.headers["X-Request-ID"] != "x" * 500


class TestCsrf:
    def test_safe_methods_are_never_challenged(self, client):
        response = client.get(
            "/api/system/info", cookies={"creator_os_csrf": "token-value"}
        )
        assert response.status_code == 200

    def test_header_authenticated_requests_bypass_csrf(self, client):
        """Bearer/API-key credentials are not auto-attached by a browser."""
        response = client.post(
            "/api/projects/",
            json={"name": "p"},
            headers={"Authorization": "Bearer whatever"},
            cookies={"creator_os_csrf": "token-value"},
        )
        assert response.status_code != 403

    def test_requests_without_a_csrf_cookie_pass_through(self, client):
        """No cookie session means there is nothing to ride on."""
        response = client.post("/api/projects/", json={"name": "p"})
        assert response.status_code != 403

    def test_cookie_without_matching_header_is_refused(self, client):
        response = client.post(
            "/api/projects/", json={"name": "p"}, cookies={"creator_os_csrf": "abc"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_failed"

    def test_mismatched_token_is_refused(self, client):
        response = client.post(
            "/api/projects/",
            json={"name": "p"},
            cookies={"creator_os_csrf": "abc"},
            headers={"X-CSRF-Token": "different"},
        )
        assert response.status_code == 403

    def test_matching_token_is_accepted(self, client):
        response = client.post(
            "/api/projects/",
            json={"name": "p"},
            cookies={"creator_os_csrf": "abc"},
            headers={"X-CSRF-Token": "abc"},
        )
        assert response.status_code == 200

    def test_can_be_disabled(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        response = make_client().post(
            "/api/projects/", json={"name": "p"}, cookies={"creator_os_csrf": "abc"}
        )
        assert response.status_code == 200


class TestRateLimiting:
    @pytest.fixture
    def limited_client(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_REQUESTS", 5)
        monkeypatch.setattr(settings, "RATE_LIMIT_WINDOW_SECONDS", 60.0)
        monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_REQUESTS", 2)
        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        return make_client()

    def test_requests_are_limited_and_report_retry_after(self, limited_client):
        statuses = [
            limited_client.get("/api/system/info").status_code for _ in range(8)
        ]
        assert 429 in statuses
        assert statuses[:5] == [200] * 5

        blocked = limited_client.get("/api/system/info")
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert int(blocked.headers["Retry-After"]) >= 1

    def test_remaining_budget_is_advertised(self, limited_client):
        response = limited_client.get("/api/system/info")
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert int(response.headers["X-RateLimit-Remaining"]) == 4

    def test_health_endpoints_are_exempt(self, limited_client):
        """A probe must never be throttled out of service."""
        for _ in range(20):
            assert limited_client.get("/health").status_code == 200
        assert limited_client.get("/health/live").status_code == 200

    def test_login_has_a_stricter_independent_budget(self, limited_client):
        """Credential stuffing is throttled separately from normal API use."""
        statuses = [
            limited_client.post(
                "/api/auth/login", json={"username": "a", "password": "b"}
            ).status_code
            for _ in range(4)
        ]
        assert 429 in statuses
        # The general budget is untouched by those login attempts.
        assert limited_client.get("/api/system/info").status_code == 200

    def test_distinct_credentials_get_distinct_budgets(self, limited_client):
        for _ in range(6):
            limited_client.get("/api/system/info", headers={"X-API-Key": "key-a"})
        # A different key is unaffected by the first one's exhaustion.
        assert (
            limited_client.get(
                "/api/system/info", headers={"X-API-Key": "key-b"}
            ).status_code
            == 200
        )


class TestTrustedHost:
    def test_disabled_by_default(self, client):
        response = client.get("/health", headers={"Host": "evil.example.com"})
        assert response.status_code == 200

    def test_unknown_host_is_rejected_when_configured(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["creator.example.com"])
        client = make_client()
        assert (
            client.get("/health", headers={"Host": "evil.example.com"}).status_code
            == 400
        )
        assert (
            client.get("/health", headers={"Host": "creator.example.com"}).status_code
            == 200
        )


class TestBodySizeLimit:
    def test_oversized_body_is_refused(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 512)
        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        response = make_client().post(
            "/api/projects/", json={"name": "x" * 4000}
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
