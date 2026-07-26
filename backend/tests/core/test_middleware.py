"""Middleware tests: request ids, security headers, size limits, rate limiting."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)


def build_app(*middleware_specs):
    app = FastAPI()
    for cls, kwargs in middleware_specs:
        app.add_middleware(cls, **kwargs)

    @app.get("/ping")
    def ping():
        return {"pong": True}

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    @app.post("/echo")
    def echo(payload: dict):
        return payload

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    return app


class TestRequestContextMiddleware:
    def test_adds_request_id_header(self):
        client = TestClient(build_app((RequestContextMiddleware, {})))
        response = client.get("/ping")
        assert response.headers["X-Request-ID"]
        assert len(response.headers["X-Request-ID"]) == 32

    def test_adds_response_time_header(self):
        client = TestClient(build_app((RequestContextMiddleware, {})))
        response = client.get("/ping")
        assert float(response.headers["X-Response-Time-ms"]) >= 0

    def test_honours_incoming_request_id(self):
        client = TestClient(build_app((RequestContextMiddleware, {})))
        response = client.get("/ping", headers={"X-Request-ID": "my-trace-id"})
        assert response.headers["X-Request-ID"] == "my-trace-id"

    def test_rejects_overlong_incoming_id(self):
        client = TestClient(build_app((RequestContextMiddleware, {})))
        response = client.get("/ping", headers={"X-Request-ID": "x" * 500})
        assert response.headers["X-Request-ID"] != "x" * 500

    def test_ids_are_unique_per_request(self):
        client = TestClient(build_app((RequestContextMiddleware, {})))
        first = client.get("/ping").headers["X-Request-ID"]
        second = client.get("/ping").headers["X-Request-ID"]
        assert first != second

    def test_error_still_logs_and_propagates(self):
        client = TestClient(
            build_app((RequestContextMiddleware, {})), raise_server_exceptions=False
        )
        assert client.get("/boom").status_code == 500


class TestSecurityHeadersMiddleware:
    @pytest.fixture
    def client(self):
        return TestClient(build_app((SecurityHeadersMiddleware, {})))

    @pytest.mark.parametrize(
        "header,value",
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Permitted-Cross-Domain-Policies", "none"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
        ],
    )
    def test_headers_present(self, client, header, value):
        assert client.get("/ping").headers[header] == value

    def test_csp_present_on_api_routes(self, client):
        assert "default-src 'none'" in client.get("/ping").headers["Content-Security-Policy"]

    def test_permissions_policy_present(self, client):
        assert "camera=()" in client.get("/ping").headers["Permissions-Policy"]

    def test_custom_csp(self):
        client = TestClient(build_app((SecurityHeadersMiddleware, {"csp": "default-src 'self'"})))
        assert client.get("/ping").headers["Content-Security-Policy"] == "default-src 'self'"


class TestBodySizeLimitMiddleware:
    def test_allows_small_body(self):
        client = TestClient(build_app((BodySizeLimitMiddleware, {"max_bytes": 10_000})))
        assert client.post("/echo", json={"a": 1}).status_code == 200

    def test_rejects_oversized_body(self):
        client = TestClient(build_app((BodySizeLimitMiddleware, {"max_bytes": 10})))
        response = client.post("/echo", json={"a": "x" * 1000})
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    def test_exempt_path_bypasses_limit(self):
        client = TestClient(
            build_app((BodySizeLimitMiddleware, {"max_bytes": 10, "exempt_paths": ("/echo",)}))
        )
        assert client.post("/echo", json={"a": "x" * 1000}).status_code == 200

    def test_get_requests_unaffected(self):
        client = TestClient(build_app((BodySizeLimitMiddleware, {"max_bytes": 1})))
        assert client.get("/ping").status_code == 200


class TestRateLimitMiddleware:
    def test_allows_under_limit(self):
        client = TestClient(
            build_app((RateLimitMiddleware, {"max_requests": 5, "window_seconds": 60}))
        )
        for _ in range(5):
            assert client.get("/ping").status_code == 200

    def test_blocks_over_limit(self):
        client = TestClient(
            build_app((RateLimitMiddleware, {"max_requests": 3, "window_seconds": 60}))
        )
        for _ in range(3):
            client.get("/ping")
        response = client.get("/ping")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
        assert response.headers["Retry-After"]

    def test_health_is_exempt(self):
        client = TestClient(
            build_app((RateLimitMiddleware, {"max_requests": 1, "window_seconds": 60}))
        )
        for _ in range(10):
            assert client.get("/health").status_code == 200

    def test_rate_limit_headers_present(self):
        client = TestClient(
            build_app((RateLimitMiddleware, {"max_requests": 10, "window_seconds": 60}))
        )
        response = client.get("/ping")
        assert response.headers["X-RateLimit-Limit"] == "10"
        assert int(response.headers["X-RateLimit-Remaining"]) == 9

    def test_window_expiry_allows_again(self):
        client = TestClient(
            build_app((RateLimitMiddleware, {"max_requests": 1, "window_seconds": 0.05}))
        )
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 429
        import time

        time.sleep(0.1)
        assert client.get("/ping").status_code == 200


class TestMiddlewareStack:
    def test_all_layers_compose(self):
        client = TestClient(
            build_app(
                (RateLimitMiddleware, {"max_requests": 100}),
                (BodySizeLimitMiddleware, {"max_bytes": 100_000}),
                (SecurityHeadersMiddleware, {}),
                (RequestContextMiddleware, {}),
            )
        )
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.headers["X-Request-ID"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-RateLimit-Limit"] == "100"
