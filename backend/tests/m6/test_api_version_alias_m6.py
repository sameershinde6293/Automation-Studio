"""Regression tests for M6-F2: the /api/v1 alias must not bypass controls.

``main.create_app`` mounts every router twice — once at ``/api`` and once at
``/api/v1`` — so clients can pin a version. Four security/behaviour controls
matched the literal ``/api`` prefix and therefore silently did not apply to the
versioned alias:

===========================  ==========================  =========================
Control                      ``/api/...`` (M5)           ``/api/v1/...`` (M5 bug)
===========================  ==========================  =========================
Auth rate-limit budget       throttled at 10/min         **never throttled**
CSRF exemption for login     allowed                     403 csrf_failed
``Cache-Control: no-store``  present                     **absent**
Upload body-size exemption   accepted                    413
===========================  ==========================  =========================

The rate-limit row is the security-relevant one: M5's credential-stuffing
defence was defeated by inserting ``/v1`` into the URL. The fix normalises the
path once (``app.core.middleware.canonical_path``) and applies it at all four
sites, so a future ``/api/v2`` mount inherits every control automatically.

See docs/M6_VALIDATION_REPORT.md finding M6-F2.
"""

from __future__ import annotations

import pytest

from app.core.middleware import canonical_path

pytestmark = pytest.mark.usefixtures("bind_sessions")


# --------------------------------------------------------------------------- #
# The normalisation primitive
# --------------------------------------------------------------------------- #
class TestCanonicalPath:
    @pytest.mark.parametrize(
        "versioned,expected",
        [
            ("/api/v1/auth/login", "/api/auth/login"),
            ("/api/v1/auth/refresh", "/api/auth/refresh"),
            ("/api/v1/media/upload", "/api/media/upload"),
            ("/api/v1/workflows", "/api/workflows"),
            ("/api/v1", "/api"),
        ],
    )
    def test_version_prefix_is_collapsed(self, versioned, expected):
        assert canonical_path(versioned) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "/api/auth/login",
            "/api/workflows",
            "/health",
            "/health/ready",
            "/metrics",
            "/",
        ],
    )
    def test_unversioned_paths_are_unchanged(self, path):
        assert canonical_path(path) == path

    def test_does_not_mangle_a_lookalike_segment(self):
        """``/api/v10`` is not ``/api/v1`` — prefix matching must be exact."""
        assert canonical_path("/api/v10/workflows") == "/api/v10/workflows"

    def test_does_not_touch_a_v1_that_is_not_a_prefix(self):
        assert canonical_path("/api/workflows/v1") == "/api/workflows/v1"


# --------------------------------------------------------------------------- #
# 1. Rate limiting — the security-relevant bypass
# --------------------------------------------------------------------------- #
class TestAuthRateLimitCoversVersionedAlias:
    """The stricter credential budget must apply on both mounts."""

    @pytest.fixture
    def rate_limited_client(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        monkeypatch.setattr(settings, "AUTH_SECRET_KEY", "k" * 48)
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        # Generous general budget, tight auth budget: any 429 observed below is
        # therefore attributable to the credential-endpoint rule.
        monkeypatch.setattr(settings, "RATE_LIMIT_REQUESTS", 10_000)
        monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_REQUESTS", 5)
        monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_WINDOW_SECONDS", 60.0)
        return make_client()

    @staticmethod
    def _hammer(client, path, count=12):
        return [
            client.post(
                path, json={"username": "nobody", "password": "WrongPassw0rd!"}
            ).status_code
            for _ in range(count)
        ]

    def test_versioned_login_is_throttled(self, rate_limited_client):
        """Pre-M6 this returned 401 twelve times and never throttled."""
        codes = self._hammer(rate_limited_client, "/api/v1/auth/login")
        assert 429 in codes, f"/api/v1 login was never rate limited: {codes}"

    def test_unversioned_login_is_still_throttled(self, rate_limited_client):
        codes = self._hammer(rate_limited_client, "/api/auth/login")
        assert 429 in codes

    def test_both_mounts_share_one_budget(self, rate_limited_client):
        """Alternating between mounts must not double the allowance.

        Separate buckets would let an attacker get 2x the attempts simply by
        alternating URLs, which is the same bypass in a subtler form.
        """
        codes = []
        for _ in range(6):
            for path in ("/api/auth/login", "/api/v1/auth/login"):
                codes.append(
                    rate_limited_client.post(
                        path, json={"username": "nobody", "password": "WrongPassw0rd!"}
                    ).status_code
                )
        assert 429 in codes
        assert sum(c != 429 for c in codes) <= 6, (
            "alternating mounts exceeded the single shared budget: %s" % codes
        )


# --------------------------------------------------------------------------- #
# 2. Credential caching
# --------------------------------------------------------------------------- #
class TestAuthCacheControlCoversVersionedAlias:
    def test_versioned_auth_response_is_no_store(self, client):
        """Pre-M6 the versioned response carried no Cache-Control at all."""
        response = client.get("/api/v1/auth/me")
        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("Pragma") == "no-cache"

    def test_unversioned_auth_response_is_no_store(self, client):
        response = client.get("/api/auth/me")
        assert response.headers.get("Cache-Control") == "no-store"

    def test_non_auth_route_is_not_forced_no_store(self, client):
        """The header must be scoped to credentials, not blanket-applied."""
        assert client.get("/health").headers.get("Cache-Control") != "no-store"


# --------------------------------------------------------------------------- #
# 3. CSRF exemptions
# --------------------------------------------------------------------------- #
class TestCsrfExemptionCoversVersionedAlias:
    @pytest.fixture
    def csrf_client(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
        client = make_client()
        # A cookie-bearing browser session with no CSRF header is exactly the
        # case the middleware guards.
        client.cookies.set("creator_os_csrf", "cookie-value")
        return client

    @pytest.mark.parametrize(
        "path", ["/api/auth/login", "/api/v1/auth/login"]
    )
    def test_login_is_exempt_on_both_mounts(self, csrf_client, path):
        """Pre-M6 the /api/v1 form returned 403 csrf_failed and was unusable."""
        response = csrf_client.post(
            path, json={"username": "nobody", "password": "WrongPassw0rd!"}
        )
        assert response.status_code != 403 or "csrf" not in response.text.lower()

    @pytest.mark.parametrize(
        "path", ["/api/auth/refresh", "/api/v1/auth/refresh"]
    )
    def test_refresh_is_exempt_on_both_mounts(self, csrf_client, path):
        response = csrf_client.post(path, json={"refresh_token": "irrelevant"})
        assert response.status_code != 403 or "csrf" not in response.text.lower()

    def test_csrf_is_still_enforced_on_a_non_exempt_route(self, csrf_client):
        """The fix must not have turned CSRF protection off wholesale."""
        response = csrf_client.post("/api/v1/projects/", json={"name": "x"})
        assert response.status_code == 403
        assert "csrf" in response.text.lower()


# --------------------------------------------------------------------------- #
# 4. Body-size exemption for uploads
# --------------------------------------------------------------------------- #
class TestBodySizeExemptionCoversVersionedAlias:
    @pytest.fixture
    def small_limit_client(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 1024)
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
        return make_client()

    @pytest.mark.parametrize(
        "path", ["/api/media/upload", "/api/v1/media/upload"]
    )
    def test_upload_is_exempt_from_the_body_limit(self, small_limit_client, path):
        """Pre-M6 the versioned upload path returned 413 for any real file."""
        payload = b"x" * 8192  # 8x the configured limit
        response = small_limit_client.post(
            path, files={"file": ("big.bin", payload, "application/octet-stream")}
        )
        assert response.status_code != 413

    @pytest.mark.parametrize("path", ["/api/projects/", "/api/v1/projects/"])
    def test_non_exempt_routes_still_enforce_the_limit(
        self, small_limit_client, path
    ):
        """The exemption must stay narrow on both mounts."""
        response = small_limit_client.post(path, json={"name": "x" * 4096})
        assert response.status_code == 413


# --------------------------------------------------------------------------- #
# 5. RBAC parity — audited as already-correct, pinned so it stays that way
# --------------------------------------------------------------------------- #
class TestAuthorizationAppliesToBothMounts:
    """Route dependencies apply to both mounts; prove it rather than assume it.

    The M6 audit concluded the alias was *not* an authorization bypass because
    RBAC is enforced by router-level dependencies rather than path matching.
    That conclusion is worth a permanent test.
    """

    @pytest.fixture
    def authed(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        monkeypatch.setattr(settings, "AUTH_SECRET_KEY", "k" * 48)
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        return make_client()

    @pytest.mark.parametrize("path", ["/api/workflows/", "/api/v1/workflows/"])
    def test_anonymous_is_rejected_on_both_mounts(self, authed, path):
        assert authed.get(path).status_code == 401

    @pytest.mark.parametrize("path", ["/api/projects/", "/api/v1/projects/"])
    def test_viewer_cannot_write_on_either_mount(self, authed, path, make_user):
        make_user(username="viewer1", password="correct-horse-battery", role="viewer")
        tokens = authed.post(
            "/api/auth/login",
            json={"username": "viewer1", "password": "correct-horse-battery"},
        ).json()
        header = {"Authorization": f"Bearer {tokens['access_token']}"}
        response = authed.post(path, json={"name": "nope"}, headers=header)
        assert response.status_code == 403
