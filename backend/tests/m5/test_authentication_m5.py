"""M5: authentication endpoints, sessions and API keys."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("auth_settings")

PASSWORD = "correct-horse-battery"


class TestLogin:
    def test_valid_credentials_return_a_token_pair(self, client, make_user):
        make_user(username="alice", password=PASSWORD, role="editor")
        response = client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["user"]["username"] == "alice"
        assert body["user"]["role"] == "editor"
        # The password must never come back out.
        assert PASSWORD not in response.text

    def test_wrong_password_is_rejected(self, client, make_user):
        make_user(username="alice", password=PASSWORD)
        response = client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong-password"}
        )
        assert response.status_code == 401

    def test_unknown_user_is_indistinguishable_from_a_bad_password(
        self, client, make_user
    ):
        """The response must not let an attacker enumerate valid usernames."""
        make_user(username="alice", password=PASSWORD)
        wrong_password = client.post(
            "/api/auth/login", json={"username": "alice", "password": "nope-nope-nope"}
        )
        unknown_user = client.post(
            "/api/auth/login", json={"username": "nobody", "password": "nope-nope-nope"}
        )
        assert wrong_password.status_code == unknown_user.status_code == 401
        assert (
            wrong_password.json()["error"]["message"]
            == unknown_user.json()["error"]["message"]
        )

    def test_username_is_case_insensitive(self, client, make_user):
        make_user(username="alice", password=PASSWORD)
        response = client.post(
            "/api/auth/login", json={"username": "ALICE", "password": PASSWORD}
        )
        assert response.status_code == 200

    def test_repeated_failures_lock_the_account(self, client, make_user, auth_settings):
        make_user(username="alice", password=PASSWORD)
        for _ in range(auth_settings.AUTH_MAX_FAILED_LOGINS):
            client.post(
                "/api/auth/login", json={"username": "alice", "password": "bad-guess-x"}
            )
        # Even the correct password is now refused.
        response = client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 401
        assert "locked" in response.json()["error"]["message"].lower()

    def test_successful_login_clears_the_failure_counter(self, client, make_user):
        make_user(username="alice", password=PASSWORD)
        client.post(
            "/api/auth/login", json={"username": "alice", "password": "bad-guess-x"}
        )
        assert (
            client.post(
                "/api/auth/login", json={"username": "alice", "password": PASSWORD}
            ).status_code
            == 200
        )
        # The earlier failure must not count toward a later lockout.
        for _ in range(3):
            client.post(
                "/api/auth/login", json={"username": "alice", "password": "bad-guess-x"}
            )
        assert (
            client.post(
                "/api/auth/login", json={"username": "alice", "password": PASSWORD}
            ).status_code
            == 200
        )

    def test_deactivated_account_cannot_log_in(
        self, client, make_user, bind_sessions
    ):
        from app.services.security.auth_service import auth_service

        user = make_user(username="alice", password=PASSWORD)
        db = bind_sessions()
        try:
            auth_service.set_active(db, auth_service.get_by_id(db, user.id), False)
        finally:
            db.close()
        response = client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 403

    def test_login_response_is_not_cacheable(self, client, make_user):
        make_user(username="alice", password=PASSWORD)
        response = client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.headers.get("Cache-Control") == "no-store"


class TestTokenUsage:
    def test_bearer_token_identifies_the_caller(self, client, login):
        header, _, user = login(username="alice", role="editor")
        response = client.get("/api/auth/me", headers=header)
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is True
        assert body["username"] == "alice"
        assert body["user_id"] == user.id
        assert body["auth_method"] == "jwt"
        assert set(body["permissions"]) == {"read", "write", "execute"}

    def test_no_credential_is_anonymous(self, client):
        body = client.get("/api/auth/me").json()
        assert body["authenticated"] is False
        assert body["permissions"] == []

    def test_garbage_token_is_rejected(self, client):
        response = client.get(
            "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    def test_refresh_token_is_not_accepted_as_a_bearer_token(self, client, login):
        _, tokens, _ = login()
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )
        assert response.status_code == 401

    def test_role_change_takes_effect_without_reissuing_the_token(
        self, client, login, bind_sessions
    ):
        """The role is read from the database, not trusted from the token."""
        from app.services.security.auth_service import auth_service

        header, _, user = login(username="alice", role="admin")
        db = bind_sessions()
        try:
            auth_service.set_role(db, auth_service.get_by_id(db, user.id), "viewer")
        finally:
            db.close()
        body = client.get("/api/auth/me", headers=header).json()
        assert body["role"] == "viewer"
        assert body["permissions"] == ["read"]


class TestRefreshAndLogout:
    def test_refresh_returns_a_new_pair(self, client, login):
        _, tokens, _ = login()
        response = client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert response.status_code == 200
        assert response.json()["refresh_token"] != tokens["refresh_token"]

    def test_a_consumed_refresh_token_cannot_be_reused(self, client, login):
        """Rotation must invalidate the presented token."""
        _, tokens, _ = login()
        client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        replay = client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert replay.status_code == 401

    def test_replay_revokes_the_whole_family(self, client, login):
        """Replay signals theft, so every session for that user is dropped."""
        _, tokens, _ = login()
        rotated = client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).json()
        client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        # The legitimately rotated token is now dead too.
        assert (
            client.post(
                "/api/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
            ).status_code
            == 401
        )

    def test_logout_revokes_the_session(self, client, login):
        _, tokens, _ = login()
        assert (
            client.post(
                "/api/auth/logout", json={"refresh_token": tokens["refresh_token"]}
            ).json()["revoked"]
            is True
        )
        assert (
            client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            ).status_code
            == 401
        )

    def test_password_change_revokes_existing_sessions(self, client, login):
        header, tokens, _ = login(username="alice")
        response = client.post(
            "/api/auth/password",
            headers=header,
            json={"current_password": PASSWORD, "new_password": "an-entirely-new-one"},
        )
        assert response.status_code == 200
        assert (
            client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            ).status_code
            == 401
        )

    def test_password_change_requires_the_current_password(self, client, login):
        header, _, _ = login()
        response = client.post(
            "/api/auth/password",
            headers=header,
            json={"current_password": "not-the-password", "new_password": "brand-new-secret"},
        )
        assert response.status_code == 401

    def test_password_change_enforces_the_strength_policy(self, client, login):
        header, _, _ = login()
        response = client.post(
            "/api/auth/password",
            headers=header,
            json={"current_password": PASSWORD, "new_password": "short"},
        )
        assert response.status_code == 422


class TestRegistration:
    def test_self_registration_is_disabled_by_default(self, client):
        response = client.post(
            "/api/auth/register",
            json={"username": "newcomer", "password": "a-fine-passphrase"},
        )
        assert response.status_code == 403

    def test_self_registration_when_enabled_grants_the_lowest_role(
        self, client, auth_settings, monkeypatch
    ):
        monkeypatch.setattr(auth_settings, "AUTH_ALLOW_SELF_REGISTRATION", True)
        response = client.post(
            "/api/auth/register",
            json={"username": "newcomer", "password": "a-fine-passphrase"},
        )
        assert response.status_code == 201
        assert response.json()["role"] == "viewer"

    def test_duplicate_username_conflicts(
        self, client, make_user, auth_settings, monkeypatch
    ):
        monkeypatch.setattr(auth_settings, "AUTH_ALLOW_SELF_REGISTRATION", True)
        make_user(username="taken", password=PASSWORD)
        response = client.post(
            "/api/auth/register",
            json={"username": "taken", "password": "a-fine-passphrase"},
        )
        assert response.status_code == 409


class TestApiKeys:
    def test_key_is_returned_once_and_then_works(self, client, login):
        header, _, _ = login(username="alice", role="admin")
        created = client.post(
            "/api/auth/api-keys", headers=header, json={"name": "ci"}
        )
        assert created.status_code == 201
        key = created.json()["key"]
        assert key.startswith("cos_")

        # It authenticates.
        me = client.get("/api/auth/me", headers={"X-API-Key": key})
        assert me.status_code == 200
        assert me.json()["auth_method"] == "api_key"
        assert me.json()["username"] == "alice"

        # It is never listed again.
        listed = client.get("/api/auth/api-keys", headers=header).json()["items"]
        assert len(listed) == 1
        assert "key" not in listed[0]
        assert key not in str(listed)

    def test_unknown_key_is_rejected(self, client):
        response = client.get("/api/auth/me", headers={"X-API-Key": "cos_bogus"})
        assert response.status_code == 401

    def test_revoked_key_stops_working(self, client, login):
        header, _, _ = login()
        created = client.post(
            "/api/auth/api-keys", headers=header, json={"name": "ci"}
        ).json()
        key = created["key"]
        assert client.get("/api/auth/me", headers={"X-API-Key": key}).status_code == 200

        assert (
            client.delete(
                f"/api/auth/api-keys/{created['id']}", headers=header
            ).status_code
            == 204
        )
        assert client.get("/api/auth/me", headers={"X-API-Key": key}).status_code == 401

    def test_scopes_narrow_but_never_widen_permissions(self, client, login):
        """A scoped key held by an admin must not grant more than the scope."""
        header, _, _ = login(username="admin-user", role="admin")
        key = client.post(
            "/api/auth/api-keys",
            headers=header,
            json={"name": "read-only", "scopes": ["read"]},
        ).json()["key"]

        body = client.get("/api/auth/me", headers={"X-API-Key": key}).json()
        assert body["permissions"] == ["read"]
        # The underlying role is still admin, but the key cannot use it.
        assert body["role"] == "admin"

    def test_scope_must_be_a_known_permission(self, client, login):
        header, _, _ = login()
        response = client.post(
            "/api/auth/api-keys",
            headers=header,
            json={"name": "bad", "scopes": ["not-a-permission"]},
        )
        assert response.status_code == 422

    def test_expired_key_is_rejected(self, client, login, bind_sessions):
        from datetime import timedelta

        from app.domain.models.base import utcnow
        from app.domain.models.identity import ApiKey

        header, _, _ = login()
        created = client.post(
            "/api/auth/api-keys", headers=header, json={"name": "ci"}
        ).json()
        db = bind_sessions()
        try:
            record = db.get(ApiKey, created["id"])
            record.expires_at = utcnow() - timedelta(days=1)
            db.add(record)
            db.commit()
        finally:
            db.close()
        assert (
            client.get(
                "/api/auth/me", headers={"X-API-Key": created["key"]}
            ).status_code
            == 401
        )

    def test_a_user_cannot_revoke_another_users_key(self, client, login):
        alice_header, _, _ = login(username="alice", role="admin")
        created = client.post(
            "/api/auth/api-keys", headers=alice_header, json={"name": "alice-key"}
        ).json()

        bob_header, _, _ = login(username="bob", role="admin")
        response = client.delete(
            f"/api/auth/api-keys/{created['id']}", headers=bob_header
        )
        assert response.status_code == 404
        # Alice's key still works.
        assert (
            client.get(
                "/api/auth/me", headers={"X-API-Key": created["key"]}
            ).status_code
            == 200
        )
