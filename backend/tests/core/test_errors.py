"""Error hierarchy and exception-handler tests."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import (
    ConfigurationError,
    ConflictError,
    CreatorOSError,
    ExecutionError,
    ForbiddenError,
    NotFoundError,
    ProviderError,
    RateLimitError,
    SecurityError,
    UnauthorizedError,
    ValidationError,
    register_exception_handlers,
)


class TestErrorHierarchy:
    @pytest.mark.parametrize(
        "cls,status,code",
        [
            (NotFoundError, 404, "not_found"),
            (ValidationError, 422, "validation_error"),
            (ConflictError, 409, "conflict"),
            (UnauthorizedError, 401, "unauthorized"),
            (ForbiddenError, 403, "forbidden"),
            (RateLimitError, 429, "rate_limited"),
            (SecurityError, 400, "security_policy_violation"),
            (ProviderError, 502, "provider_error"),
            (ExecutionError, 500, "execution_error"),
            (ConfigurationError, 500, "configuration_error"),
        ],
    )
    def test_status_and_code(self, cls, status, code):
        error = cls("message")
        assert error.status_code == status
        assert error.code == code

    def test_all_subclass_base(self):
        assert issubclass(NotFoundError, CreatorOSError)
        assert issubclass(CreatorOSError, Exception)

    def test_details_carried(self):
        error = NotFoundError("gone", details={"id": 7})
        assert error.details == {"id": 7}

    def test_overrides(self):
        error = CreatorOSError("m", status_code=418, code="teapot")
        assert error.status_code == 418 and error.code == "teapot"

    def test_to_dict_shape(self):
        payload = NotFoundError("missing", details={"a": 1}).to_dict("rid-1")
        assert payload == {
            "error": {
                "code": "not_found",
                "message": "missing",
                "details": {"a": 1},
                "request_id": "rid-1",
            }
        }

    def test_to_dict_omits_empty_fields(self):
        payload = NotFoundError("missing").to_dict()
        assert payload["error"] == {"code": "not_found", "message": "missing"}

    def test_default_message(self):
        assert "unexpected" in CreatorOSError().message.lower()

    def test_is_raisable(self):
        with pytest.raises(NotFoundError, match="nope"):
            raise NotFoundError("nope")


@pytest.fixture
def error_app():
    app = FastAPI()
    register_exception_handlers(app)

    class Body(BaseModel):
        count: int

    @app.get("/app-error")
    def app_error():
        raise NotFoundError("Workflow 12 not found", details={"id": 12})

    @app.get("/http-error")
    def http_error():
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="nope")

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail: password=hunter2")

    @app.post("/validate")
    def validate(body: Body):
        return {"count": body.count}

    return TestClient(app, raise_server_exceptions=False)


class TestExceptionHandlers:
    def test_app_error_envelope(self, error_app):
        response = error_app.get("/app-error")
        assert response.status_code == 404
        body = response.json()["error"]
        assert body["code"] == "not_found"
        assert body["message"] == "Workflow 12 not found"
        assert body["details"] == {"id": 12}

    def test_http_exception_envelope(self, error_app):
        response = error_app.get("/http-error")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

    def test_unexpected_error_does_not_leak_internals(self, error_app):
        response = error_app.get("/boom")
        assert response.status_code == 500
        body = response.json()["error"]
        assert body["code"] == "internal_error"
        assert "hunter2" not in str(body)
        assert "RuntimeError" not in str(body)

    def test_validation_error_envelope(self, error_app):
        response = error_app.post("/validate", json={"count": "not-an-int"})
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "validation_error"
        assert isinstance(body["details"]["errors"], list)
        assert body["details"]["errors"][0]["loc"]

    def test_validation_details_are_json_serialisable(self, error_app):
        import json

        response = error_app.post("/validate", json={})
        json.dumps(response.json())  # must not raise

    def test_success_path_unaffected(self, error_app):
        assert error_app.post("/validate", json={"count": 3}).json() == {"count": 3}
