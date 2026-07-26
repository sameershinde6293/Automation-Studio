"""M5: every mutating endpoint enforces a permission.

These tests exist because of a defect the M5 self-audit caught **after** the
authorization machinery was already written and passing its own unit tests:
``require_permission`` had only been wired into 2 of the 9 routers. A ``viewer``
could create and delete workflows, register plugins and forge audit entries,
and an anonymous caller could do the same.

Unit-testing the dependency was not enough — what mattered was whether it was
*applied*. The coverage test below walks the live route table and fails if any
route is missing an authorization dependency, so a new unprotected endpoint
cannot ship by omission.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("auth_settings")

PASSWORD = "correct-horse-battery"

#: Routes that are legitimately reachable without a permission check.
PUBLIC_PATHS = {
    "/",
    "/health",
    "/health/live",
    "/health/ready",
    "/metrics",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    # Credential endpoints: a caller cannot hold a permission before logging in.
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/register",
    "/api/auth/me",
    # System introspection used by the editor to render its palette.
    "/api/system/info",
    "/api/system/metrics",
    "/api/system/node-types",
    "/api/system/node-schemas",
    "/api/system/events",
    "/api/system/scheduler/jobs",
}

AUTH_DEPENDENCY_NAMES = {
    "get_principal",
    "require_authenticated",
    "require_read",
    "require_write",
    "require_execute",
    "require_manage_users",
    "require_manage_plugins",
    "require_manage_settings",
    "require_view_audit",
    "require_read_or_write",
    "require_read_or_execute",
    "require_read_or_manage_plugins",
    "require_self_or_manage_users",
}


def _route_has_auth(route) -> bool:
    """Whether any dependency in the route's tree performs an auth check."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False

    stack = [dependant]
    seen = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        call = getattr(current, "call", None)
        if call is not None and getattr(call, "__name__", "") in AUTH_DEPENDENCY_NAMES:
            return True
        stack.extend(getattr(current, "dependencies", []) or [])
    return False


class TestRouteCoverage:
    def test_every_route_declares_an_authorization_dependency(self, client):
        """Walks the real route table; fails on any unprotected endpoint."""
        from fastapi.routing import APIRoute

        unprotected = []
        for route in client.app.routes:
            if not isinstance(route, APIRoute):
                continue
            # /api/v1 mirrors are the same handlers as the unprefixed routes.
            path = route.path.replace("/api/v1", "/api", 1)
            if path in PUBLIC_PATHS:
                continue
            if not _route_has_auth(route):
                unprotected.append(f"{sorted(route.methods)} {route.path}")

        assert not unprotected, (
            "these routes have no authorization dependency:\n  "
            + "\n  ".join(sorted(unprotected))
        )


class TestAnonymousIsRejected:
    @pytest.mark.parametrize(
        "method,path,payload",
        [
            ("post", "/api/workflows/", {"name": "x"}),
            ("get", "/api/workflows/", None),
            ("post", "/api/projects/", {"name": "x"}),
            ("get", "/api/projects/", None),
            ("get", "/api/executions", None),
            ("post", "/api/plugins/", {"name": "p", "version": "1"}),
            ("get", "/api/enterprise/audit", None),
            ("post", "/api/enterprise/audit", {"event_name": "e"}),
            ("get", "/api/media/assets", None),
            ("get", "/api/ai/models", None),
        ],
    )
    def test_no_credential_is_refused(self, client, method, path, payload):
        response = getattr(client, method)(path, **({"json": payload} if payload else {}))
        assert response.status_code == 401, f"{method.upper()} {path} allowed anonymously"


class TestViewerCannotMutate:
    """A read-only role must not be able to change anything."""

    @pytest.mark.parametrize(
        "method,path,payload",
        [
            ("post", "/api/workflows/", {"name": "pwned"}),
            ("delete", "/api/workflows/1", None),
            ("post", "/api/projects/", {"name": "pwned"}),
            ("delete", "/api/projects/1", None),
            ("post", "/api/ai/conversations", {"title": "x"}),
            ("post", "/api/plugins/", {"name": "evil", "version": "1"}),
        ],
    )
    def test_mutation_is_forbidden(self, client, login, method, path, payload):
        header, _, _ = login(username="viewer-user", role="viewer")
        response = getattr(client, method)(
            path, headers=header, **({"json": payload} if payload else {})
        )
        assert response.status_code == 403, f"{method.upper()} {path} allowed for viewer"

    def test_viewer_can_still_read(self, client, login):
        header, _, _ = login(username="viewer-user", role="viewer")
        for path in ("/api/workflows/", "/api/projects/", "/api/executions"):
            assert client.get(path, headers=header).status_code == 200, path

    def test_viewer_cannot_read_the_audit_log(self, client, login):
        header, _, _ = login(username="viewer-user", role="viewer")
        assert client.get("/api/enterprise/audit", headers=header).status_code == 403

    def test_viewer_cannot_trigger_an_execution(self, client, login):
        header, _, _ = login(username="viewer-user", role="viewer")
        response = client.post("/api/workflows/1/executions", headers=header, json={})
        assert response.status_code == 403


class TestEditorAndOperator:
    def test_editor_can_create_content(self, client, login):
        header, _, _ = login(username="editor-user", role="editor")
        assert client.post(
            "/api/workflows/", headers=header, json={"name": "wf"}
        ).status_code == 200

    def test_editor_cannot_register_plugins(self, client, login):
        """Plugins execute code, so they need manage_plugins, not write."""
        header, _, _ = login(username="editor-user", role="editor")
        response = client.post(
            "/api/plugins/", headers=header, json={"name": "p", "version": "1"}
        )
        assert response.status_code == 403

    def test_operator_can_read_but_not_edit(self, client, login):
        header, _, _ = login(username="operator-user", role="operator")
        assert client.get("/api/workflows/", headers=header).status_code == 200
        assert client.post(
            "/api/workflows/", headers=header, json={"name": "x"}
        ).status_code == 403

    def test_admin_can_manage_plugins(self, client, login):
        header, _, _ = login(username="admin-user", role="admin")
        assert client.post(
            "/api/plugins/", headers=header, json={"name": "p", "version": "1"}
        ).status_code == 200


class TestAuditIntegrity:
    def test_actor_is_the_authenticated_caller_not_the_payload(self, client, login):
        """The audit trail must not be forgeable via a client-supplied id."""
        header, _, user = login(username="admin-user", role="admin")
        response = client.post(
            "/api/enterprise/audit",
            headers=header,
            json={"event_name": "test.event", "user_id": 999999},
        )
        assert response.status_code == 201

        events = client.get("/api/enterprise/audit", headers=header).json()
        recorded = next(e for e in events if e["event_name"] == "test.event")
        assert recorded["user_id"] == user.id
        assert recorded["user_id"] != 999999
        # Retained as a subject reference only.
        assert recorded["details"]["subject_user_id"] == 999999

    def test_writing_an_audit_event_requires_manage_settings(self, client, login):
        header, _, _ = login(username="editor-user", role="editor")
        response = client.post(
            "/api/enterprise/audit", headers=header, json={"event_name": "e"}
        )
        assert response.status_code == 403


class TestApiKeyScopesAtEndpoints:
    def test_read_scoped_key_cannot_mutate(self, client, login):
        """End-to-end: scope narrowing must hold at a real endpoint."""
        header, _, _ = login(username="admin-user", role="admin")
        key = client.post(
            "/api/auth/api-keys",
            headers=header,
            json={"name": "read-only", "scopes": ["read"]},
        ).json()["key"]

        key_header = {"X-API-Key": key}
        assert client.get("/api/workflows/", headers=key_header).status_code == 200
        assert client.post(
            "/api/workflows/", headers=key_header, json={"name": "x"}
        ).status_code == 403


class TestBackwardCompatibility:
    """With AUTH_ENABLED false, none of this may apply."""

    def test_all_endpoints_remain_open_when_auth_is_disabled(
        self, make_client, monkeypatch
    ):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "AUTH_ENABLED", False)
        client = make_client()

        assert client.get("/api/workflows/").status_code == 200
        assert client.post("/api/workflows/", json={"name": "wf"}).status_code == 200
        assert client.get("/api/enterprise/audit").status_code == 200
        assert client.post(
            "/api/plugins/", json={"name": "p", "version": "1"}
        ).status_code == 200
