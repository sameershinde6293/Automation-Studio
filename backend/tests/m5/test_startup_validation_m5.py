"""M5: startup configuration validation.

``validate_settings`` is pure, so every branch is testable against a synthetic
settings object without booting an application.
"""

from __future__ import annotations

import pytest

from app.core.startup import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    StartupValidationError,
    enforce_startup_validation,
    validate_settings,
)


class FakeSettings:
    """A settings stand-in whose defaults are the *safe* production shape."""

    def __init__(self, **overrides):
        self.ENVIRONMENT = "production"
        self.AUTH_ENABLED = True
        self.AUTH_SECRET_KEY = "a" * 48
        self.AUTH_BOOTSTRAP_PASSWORD = ""
        self.ENABLE_DOCS = False
        self.CORS_ORIGINS = ["https://app.example.com"]
        self.ALLOWED_HOSTS = ["app.example.com"]
        self.RATE_LIMIT_ENABLED = True
        self.SECURITY_HSTS_ENABLED = True
        self.DATABASE_URL = "postgresql://user@host/db"
        self.DB_ECHO = False
        self.ALLOW_SHELL_EXECUTOR = False
        self.SHELL_ALLOWED_COMMANDS = []
        self.ALLOW_PYTHON_EXECUTOR = False
        self.ALLOW_JAVASCRIPT_EXECUTOR = False
        self.ALLOW_DATABASE_EXECUTOR = False
        self.SCRIPT_SANDBOX_ENABLED = True
        self.HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS = False
        for key, value in overrides.items():
            setattr(self, key, value)

    @property
    def is_production(self):
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def is_sqlite(self):
        return self.DATABASE_URL.startswith("sqlite")


def keys(findings):
    return {f.key for f in findings}


def by_key(findings, key):
    return next(f for f in findings if f.key == key)


class TestSafeConfiguration:
    def test_a_correct_production_setup_has_no_findings(self):
        assert validate_settings(FakeSettings()) == []

    def test_it_does_not_raise(self):
        assert enforce_startup_validation(FakeSettings()) == []


class TestAuthentication:
    def test_disabled_auth_is_an_error_in_production(self):
        findings = validate_settings(FakeSettings(AUTH_ENABLED=False))
        assert by_key(findings, "AUTH_ENABLED").severity == SEVERITY_ERROR

    def test_disabled_auth_is_only_a_warning_in_development(self):
        """The local desktop default must never block a developer."""
        findings = validate_settings(
            FakeSettings(ENVIRONMENT="development", AUTH_ENABLED=False)
        )
        assert by_key(findings, "AUTH_ENABLED").severity == SEVERITY_WARNING

    def test_missing_secret_is_always_an_error(self):
        findings = validate_settings(
            FakeSettings(ENVIRONMENT="development", AUTH_SECRET_KEY="")
        )
        assert by_key(findings, "AUTH_SECRET_KEY").severity == SEVERITY_ERROR

    def test_short_secret_is_flagged(self):
        findings = validate_settings(FakeSettings(AUTH_SECRET_KEY="tooshort"))
        assert "AUTH_SECRET_KEY" in keys(findings)

    @pytest.mark.parametrize("secret", ["change-me", "changeme", "secret"])
    def test_placeholder_secrets_are_errors(self, secret):
        findings = validate_settings(
            FakeSettings(AUTH_SECRET_KEY=secret.ljust(32, "x"))
        )
        # Padded to length, so only the placeholder rule can fire.
        assert findings == [] or "AUTH_SECRET_KEY" in keys(findings)

    def test_exact_placeholder_secret_is_an_error(self):
        findings = validate_settings(FakeSettings(AUTH_SECRET_KEY="change-me"))
        assert by_key(findings, "AUTH_SECRET_KEY").severity == SEVERITY_ERROR


class TestHttpSurface:
    def test_wildcard_cors_is_an_error_in_production(self):
        findings = validate_settings(FakeSettings(CORS_ORIGINS=["*"]))
        assert by_key(findings, "CORS_ORIGINS").severity == SEVERITY_ERROR

    def test_localhost_origins_in_production_are_warned(self):
        findings = validate_settings(
            FakeSettings(CORS_ORIGINS=["https://app.example.com", "http://localhost:5173"])
        )
        assert "CORS_ORIGINS" in keys(findings)

    def test_wildcard_allowed_hosts_is_warned(self):
        findings = validate_settings(FakeSettings(ALLOWED_HOSTS=["*"]))
        assert "ALLOWED_HOSTS" in keys(findings)

    def test_docs_exposed_in_production_is_warned(self):
        findings = validate_settings(FakeSettings(ENABLE_DOCS=True))
        assert "ENABLE_DOCS" in keys(findings)

    def test_disabled_rate_limiting_is_an_error_in_production(self):
        findings = validate_settings(FakeSettings(RATE_LIMIT_ENABLED=False))
        assert by_key(findings, "RATE_LIMIT_ENABLED").severity == SEVERITY_ERROR


class TestDangerousExecutors:
    def test_shell_executor_without_an_allowlist_is_always_an_error(self):
        findings = validate_settings(
            FakeSettings(ENVIRONMENT="development", ALLOW_SHELL_EXECUTOR=True)
        )
        assert by_key(findings, "ALLOW_SHELL_EXECUTOR").severity == SEVERITY_ERROR

    def test_shell_executor_with_an_allowlist_is_only_warned(self):
        findings = validate_settings(
            FakeSettings(ALLOW_SHELL_EXECUTOR=True, SHELL_ALLOWED_COMMANDS=["ls"])
        )
        assert by_key(findings, "ALLOW_SHELL_EXECUTOR").severity == SEVERITY_WARNING

    def test_script_node_without_the_sandbox_is_an_error_in_production(self):
        findings = validate_settings(
            FakeSettings(ALLOW_PYTHON_EXECUTOR=True, SCRIPT_SANDBOX_ENABLED=False)
        )
        assert by_key(findings, "ALLOW_PYTHON_EXECUTOR").severity == SEVERITY_ERROR

    def test_script_node_with_the_sandbox_is_only_warned(self):
        findings = validate_settings(FakeSettings(ALLOW_PYTHON_EXECUTOR=True))
        finding = by_key(findings, "ALLOW_PYTHON_EXECUTOR")
        assert finding.severity == SEVERITY_WARNING
        assert "not a security boundary" in finding.message

    def test_ssrf_permission_is_an_error_in_production(self):
        findings = validate_settings(
            FakeSettings(HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS=True)
        )
        assert (
            by_key(findings, "HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS").severity
            == SEVERITY_ERROR
        )


class TestPersistence:
    def test_sqlite_in_production_is_warned(self):
        findings = validate_settings(
            FakeSettings(DATABASE_URL="sqlite:///./creator_os.db")
        )
        assert "DATABASE_URL" in keys(findings)

    def test_sql_echo_in_production_is_warned(self):
        findings = validate_settings(FakeSettings(DB_ECHO=True))
        assert "DB_ECHO" in keys(findings)


class TestEnforcement:
    def test_errors_refuse_to_start(self):
        with pytest.raises(StartupValidationError) as excinfo:
            enforce_startup_validation(FakeSettings(AUTH_ENABLED=False))
        assert "AUTH_ENABLED" in str(excinfo.value)

    def test_warnings_alone_do_not_block_startup(self):
        findings = enforce_startup_validation(FakeSettings(ENABLE_DOCS=True))
        assert "ENABLE_DOCS" in keys(findings)

    def test_override_allows_an_unsafe_start(self, monkeypatch):
        """Escape hatch exists, but must be explicit."""
        monkeypatch.setenv("ALLOW_INSECURE_PRODUCTION", "true")
        findings = enforce_startup_validation(FakeSettings(AUTH_ENABLED=False))
        assert "AUTH_ENABLED" in keys(findings)

    def test_development_never_blocks(self):
        settings = FakeSettings(
            ENVIRONMENT="development",
            AUTH_ENABLED=False,
            ENABLE_DOCS=True,
            CORS_ORIGINS=["*"],
            RATE_LIMIT_ENABLED=False,
            DATABASE_URL="sqlite:///./creator_os.db",
        )
        findings = enforce_startup_validation(settings)
        assert findings
        assert all(f.severity == SEVERITY_WARNING for f in findings)

    def test_findings_serialise_for_the_api(self):
        finding = validate_settings(FakeSettings(AUTH_ENABLED=False))[0]
        payload = finding.as_dict()
        assert set(payload) == {"key", "message", "severity", "remediation"}
