"""M5: RBAC enforcement, permission checks and privilege-escalation attempts.

The M5 audit found that ``EnterpriseAuth.require_permission`` existed but was
never called from anywhere, so RBAC was decorative. These tests exist to make
that failure mode impossible to reintroduce silently.
"""

from __future__ import annotations

import pytest

from app.services.enterprise.auth import ROLE_PERMISSIONS, enterprise_auth
from app.services.security.principal import (
    AUTH_METHOD_API_KEY,
    AUTH_METHOD_DISABLED,
    ANONYMOUS_PRINCIPAL,
    LOCAL_ADMIN_PRINCIPAL,
    Principal,
)

PASSWORD = "correct-horse-battery"


class TestPrincipalModel:
    def test_role_permissions_are_resolved(self):
        principal = Principal(user_id=1, username="a", role="editor", auth_method="jwt")
        assert principal.has_permission("write") is True
        assert principal.has_permission("manage_users") is False

    def test_anonymous_has_no_permissions(self):
        assert ANONYMOUS_PRINCIPAL.permissions == frozenset()
        assert ANONYMOUS_PRINCIPAL.is_authenticated is False

    def test_local_admin_is_used_when_auth_is_disabled(self):
        assert LOCAL_ADMIN_PRINCIPAL.auth_method == AUTH_METHOD_DISABLED
        assert LOCAL_ADMIN_PRINCIPAL.has_permission("manage_users") is True
        assert LOCAL_ADMIN_PRINCIPAL.is_authenticated is True

    def test_scopes_intersect_and_cannot_escalate(self):
        """A key scoped beyond its owner's role gains nothing."""
        principal = Principal(
            user_id=1,
            username="viewer-user",
            role="viewer",
            auth_method=AUTH_METHOD_API_KEY,
            scopes=frozenset({"read", "write", "manage_users"}),
        )
        # The role only grants read, so that is all the key gets.
        assert principal.permissions == frozenset({"read"})
        assert principal.has_permission("write") is False
        assert principal.has_permission("manage_users") is False

    def test_unknown_role_grants_nothing(self):
        principal = Principal(
            user_id=1, username="x", role="wizard", auth_method="jwt"
        )
        assert principal.permissions == frozenset()

    @pytest.mark.parametrize("role", sorted(ROLE_PERMISSIONS))
    def test_every_role_resolves_to_its_documented_permissions(self, role):
        principal = Principal(user_id=1, username="x", role=role, auth_method="jwt")
        assert principal.permissions == frozenset(ROLE_PERMISSIONS[role])

    def test_role_lookup_is_case_insensitive(self):
        principal = Principal(user_id=1, username="x", role="ADMIN", auth_method="jwt")
        assert principal.has_permission("manage_users") is True


class TestEnterpriseAuthUnchanged:
    """The V1.0 API must keep working exactly as before (compatibility)."""

    def test_check_permissions_still_behaves(self):
        assert enterprise_auth.check_permissions("admin", "manage_users") is True
        assert enterprise_auth.check_permissions("viewer", "write") is False
        assert enterprise_auth.check_permissions("", "read") is False


@pytest.mark.usefixtures("auth_settings")
class TestEndpointEnforcement:
    def test_admin_endpoints_reject_anonymous_callers(self, client):
        assert client.get("/api/auth/users").status_code == 401

    def test_admin_endpoints_reject_insufficient_roles(self, client, login):
        header, _, _ = login(username="viewer-user", role="viewer")
        response = client.get("/api/auth/users", headers=header)
        assert response.status_code == 403
        details = response.json()["error"]["details"]
        assert details["required_permission"] == "manage_users"
        assert details["role"] == "viewer"

    @pytest.mark.parametrize("role", ["viewer", "operator", "editor"])
    def test_non_admin_roles_cannot_manage_users(self, client, login, role):
        header, _, _ = login(username=f"{role}-user", role=role)
        assert client.get("/api/auth/users", headers=header).status_code == 403

    def test_admin_can_manage_users(self, client, login):
        header, _, _ = login(username="admin-user", role="admin")
        response = client.get("/api/auth/users", headers=header)
        assert response.status_code == 200
        assert "items" in response.json()

    def test_admin_can_create_a_user(self, client, login):
        header, _, _ = login(username="admin-user", role="admin")
        response = client.post(
            "/api/auth/users",
            headers=header,
            json={"username": "created", "password": PASSWORD, "role": "editor"},
        )
        assert response.status_code == 201
        assert response.json()["role"] == "editor"

    def test_creating_a_user_with_an_unknown_role_is_rejected(self, client, login):
        header, _, _ = login(username="admin-user", role="admin")
        response = client.post(
            "/api/auth/users",
            headers=header,
            json={"username": "created", "password": PASSWORD, "role": "superadmin"},
        )
        assert response.status_code == 422

    def test_scoped_api_key_is_refused_at_the_endpoint(self, client, login):
        """End-to-end: a read-scoped key held by an admin cannot manage users."""
        header, _, _ = login(username="admin-user", role="admin")
        key = client.post(
            "/api/auth/api-keys",
            headers=header,
            json={"name": "read-only", "scopes": ["read"]},
        ).json()["key"]
        assert client.get("/api/auth/users", headers={"X-API-Key": key}).status_code == 403

    def test_metrics_and_errors_respect_view_audit(self, client, login):
        viewer_header, _, _ = login(username="viewer-user", role="viewer")
        assert client.get("/api/system/errors", headers=viewer_header).status_code == 403
        admin_header, _, _ = login(username="admin-user", role="admin")
        assert client.get("/api/system/errors", headers=admin_header).status_code == 200

    def test_config_validation_requires_manage_settings(self, client, login):
        editor_header, _, _ = login(username="editor-user", role="editor")
        assert (
            client.get("/api/system/config/validation", headers=editor_header).status_code
            == 403
        )
        admin_header, _, _ = login(username="admin-user", role="admin")
        assert (
            client.get("/api/system/config/validation", headers=admin_header).status_code
            == 200
        )


@pytest.mark.usefixtures("auth_settings")
class TestSelfLockoutProtection:
    def test_an_admin_cannot_deactivate_themselves(self, client, login):
        header, _, user = login(username="admin-user", role="admin")
        response = client.patch(
            f"/api/auth/users/{user.id}", headers=header, json={"is_active": False}
        )
        assert response.status_code == 422

    def test_an_admin_cannot_demote_themselves(self, client, login):
        header, _, user = login(username="admin-user", role="admin")
        response = client.patch(
            f"/api/auth/users/{user.id}", headers=header, json={"role": "viewer"}
        )
        assert response.status_code == 422

    def test_an_admin_can_still_modify_others(self, client, login, make_user):
        header, _, _ = login(username="admin-user", role="admin")
        other = make_user(username="other", password=PASSWORD, role="viewer")
        response = client.patch(
            f"/api/auth/users/{other.id}", headers=header, json={"role": "editor"}
        )
        assert response.status_code == 200
        assert response.json()["role"] == "editor"

    def test_deactivating_a_user_kills_their_sessions(self, client, login, make_user):
        victim_header, victim_tokens, victim = login(
            username="victim", password=PASSWORD, role="editor"
        )
        assert client.get("/api/auth/me", headers=victim_header).json()["authenticated"]

        admin_header, _, _ = login(username="admin-user", role="admin")
        client.patch(
            f"/api/auth/users/{victim.id}",
            headers=admin_header,
            json={"is_active": False},
        )
        # The access token stops working immediately.
        assert client.get("/api/auth/me", headers=victim_header).status_code == 401
        # And the refresh token cannot mint a new one.
        assert (
            client.post(
                "/api/auth/refresh",
                json={"refresh_token": victim_tokens["refresh_token"]},
            ).status_code
            == 401
        )


class TestBackwardCompatibility:
    """With AUTH_ENABLED false (the default) nothing may require credentials."""

    def test_admin_endpoints_are_open_when_auth_is_disabled(self, client):
        assert client.get("/api/auth/users").status_code == 200

    def test_caller_is_reported_as_the_local_admin(self, client):
        body = client.get("/api/auth/me").json()
        assert body["auth_enabled"] is False
        assert body["authenticated"] is True
        assert body["role"] == "admin"
        assert body["auth_method"] == "disabled"

    def test_existing_unauthenticated_api_calls_still_work(self, client):
        """The pre-M5 surface must not have become credential-gated."""
        for path in (
            "/api/workflows/",
            "/api/projects/",
            "/api/system/info",
            "/api/system/node-types",
            "/health",
        ):
            assert client.get(path).status_code == 200, path
