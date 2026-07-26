"""Regression tests for M6-F1: comma-separated list settings must not crash.

The M5 code declared a ``_split_csv`` field validator intended to accept
``CORS_ORIGINS=a,b,c``. That validator never ran for environment input:
``pydantic-settings`` JSON-decodes complex (list/dict) fields *inside the
settings source*, before validators, and raises ``SettingsError`` on non-JSON.
Because ``Settings()`` is evaluated at module import, the process died before
logging or startup validation existed to report why.

The practical effect was that every deployment following the documented
``.env`` format could not boot at all, and the M5 startup-validation gate —
whose whole purpose is to refuse an unsafe production configuration — was
unreachable. See docs/M6_VALIDATION_REPORT.md finding M6-F1.

These tests pin the loader's behaviour from the *outside*: they set real
environment variables and real ``.env`` files, because that is the only way
the original defect was observable. Asserting on the validator directly would
have passed against the broken code.
"""

from __future__ import annotations

import pytest

from app.infrastructure.config.settings import Settings

#: Every list-typed setting an operator is documented to configure.
LIST_SETTINGS = (
    "CORS_ORIGINS",
    "ALLOWED_HOSTS",
    "SHELL_ALLOWED_COMMANDS",
    "HTTP_EXECUTOR_ALLOWED_HOSTS",
    "AI_FALLBACK_CHAIN",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate settings construction from the developer's own environment.

    ``tests/conftest.py`` exports ENVIRONMENT/LOG_LEVEL/RATE_LIMIT_ENABLED for
    the whole suite, and a real environment variable outranks a ``.env`` file.
    Those are cleared here so a test that writes a ``.env`` is actually
    exercising the dotenv source.
    """
    for name in LIST_SETTINGS + ("ENVIRONMENT", "LOG_LEVEL", "RATE_LIMIT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    # Point env_file at an empty directory so a repo-root .env cannot leak in.
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestCommaSeparatedEnvironmentValues:
    """The documented CSV form must load, not raise."""

    @pytest.mark.parametrize("name", LIST_SETTINGS)
    def test_single_value_without_json_syntax_loads(self, name, clean_env, monkeypatch):
        monkeypatch.setenv(name, "alpha")
        assert getattr(Settings(), name) == ["alpha"]

    @pytest.mark.parametrize("name", LIST_SETTINGS)
    def test_multiple_comma_separated_values_load(self, name, clean_env, monkeypatch):
        monkeypatch.setenv(name, "alpha,beta,gamma")
        assert getattr(Settings(), name) == ["alpha", "beta", "gamma"]

    def test_realistic_production_origins(self, clean_env, monkeypatch):
        """The exact shape docs/DEPLOYMENT.md tells operators to write."""
        monkeypatch.setenv(
            "CORS_ORIGINS", "https://studio.example.com,https://admin.example.com"
        )
        monkeypatch.setenv("ALLOWED_HOSTS", "studio.example.com,admin.example.com")
        settings = Settings()
        assert settings.CORS_ORIGINS == [
            "https://studio.example.com",
            "https://admin.example.com",
        ]
        assert settings.ALLOWED_HOSTS == ["studio.example.com", "admin.example.com"]

    def test_surrounding_whitespace_is_stripped(self, clean_env, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", " https://a.example.com , https://b.example.com ")
        assert Settings().CORS_ORIGINS == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_empty_value_yields_empty_list(self, clean_env, monkeypatch):
        monkeypatch.setenv("SHELL_ALLOWED_COMMANDS", "")
        assert Settings().SHELL_ALLOWED_COMMANDS == []

    def test_trailing_comma_does_not_produce_empty_entry(self, clean_env, monkeypatch):
        """An empty allowlist entry would be a security-relevant surprise."""
        monkeypatch.setenv("HTTP_EXECUTOR_ALLOWED_HOSTS", "a.example.com,")
        assert Settings().HTTP_EXECUTOR_ALLOWED_HOSTS == ["a.example.com"]


class TestJsonRemainsSupported:
    """The pre-M6 JSON form must keep working — this is a widening, not a swap."""

    @pytest.mark.parametrize("name", LIST_SETTINGS)
    def test_json_array_still_parses(self, name, clean_env, monkeypatch):
        monkeypatch.setenv(name, '["alpha", "beta"]')
        assert getattr(Settings(), name) == ["alpha", "beta"]

    def test_json_empty_array(self, clean_env, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "[]")
        assert Settings().CORS_ORIGINS == []

    def test_malformed_json_still_raises(self, clean_env, monkeypatch):
        """A truncated JSON array is an operator typo, not a one-item list.

        Silently coercing '["unclosed' into a literal host string would be
        worse than failing: it would produce an allowlist nobody intended.
        """
        monkeypatch.setenv("CORS_ORIGINS", '["unclosed')
        with pytest.raises(Exception):
            Settings()


class TestDotEnvFile:
    """The .env path is a separate settings source and regressed identically."""

    def test_comma_separated_values_in_dotenv_file(self, clean_env, monkeypatch):
        (clean_env / ".env").write_text(
            "CORS_ORIGINS=https://a.example.com,https://b.example.com\n"
            "ALLOWED_HOSTS=a.example.com,b.example.com\n"
        )
        settings = Settings()
        assert settings.CORS_ORIGINS == [
            "https://a.example.com",
            "https://b.example.com",
        ]
        assert settings.ALLOWED_HOSTS == ["a.example.com", "b.example.com"]

    def test_json_in_dotenv_file_still_parses(self, clean_env):
        (clean_env / ".env").write_text('CORS_ORIGINS=["https://a.example.com"]\n')
        assert Settings().CORS_ORIGINS == ["https://a.example.com"]

    def test_environment_still_overrides_dotenv(self, clean_env, monkeypatch):
        """Source precedence must be unchanged by the M6 source swap."""
        (clean_env / ".env").write_text("CORS_ORIGINS=https://from-file.example.com\n")
        monkeypatch.setenv("CORS_ORIGINS", "https://from-env.example.com")
        assert Settings().CORS_ORIGINS == ["https://from-env.example.com"]


class TestNonListSettingsUnaffected:
    """Scalar parsing must not have been disturbed by the custom sources."""

    def test_scalar_string_setting(self, clean_env, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 40)
        assert Settings().AUTH_SECRET_KEY == "x" * 40

    def test_scalar_bool_setting(self, clean_env, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        assert Settings().AUTH_ENABLED is True

    def test_scalar_int_setting(self, clean_env, monkeypatch):
        monkeypatch.setenv("EXECUTION_MAX_WORKERS", "12")
        assert Settings().EXECUTION_MAX_WORKERS == 12

    def test_invalid_int_still_rejected(self, clean_env, monkeypatch):
        monkeypatch.setenv("EXECUTION_MAX_WORKERS", "not-a-number")
        with pytest.raises(Exception):
            Settings()


class TestProductionConfigurationBoots:
    """End-to-end: a fully documented production .env must construct."""

    def test_documented_production_env_loads(self, clean_env, monkeypatch):
        (clean_env / ".env").write_text(
            "ENVIRONMENT=production\n"
            "AUTH_ENABLED=true\n"
            f"AUTH_SECRET_KEY={'z' * 48}\n"
            "DATABASE_URL=postgresql+psycopg://creator:pw@db:5432/creator_os\n"
            "CORS_ORIGINS=https://studio.example.com\n"
            "ALLOWED_HOSTS=studio.example.com\n"
            "ENABLE_DOCS=false\n"
            "SECURITY_HSTS_ENABLED=true\n"
            "AI_FALLBACK_CHAIN=openai,local,mock\n"
        )
        settings = Settings()
        assert settings.is_production
        assert settings.CORS_ORIGINS == ["https://studio.example.com"]
        assert settings.ALLOWED_HOSTS == ["studio.example.com"]
        assert settings.AI_FALLBACK_CHAIN == ["openai", "local", "mock"]

    def test_startup_validation_is_reachable_for_that_config(
        self, clean_env, monkeypatch
    ):
        """The M5 gate must now actually get to run.

        Previously the process died in Settings() before validate_settings
        could be called, so an unsafe production config produced an opaque
        import crash instead of the intended actionable findings.
        """
        from app.core.startup import validate_settings

        (clean_env / ".env").write_text(
            "ENVIRONMENT=production\n"
            "AUTH_ENABLED=false\n"
            "CORS_ORIGINS=https://studio.example.com\n"
        )
        findings = validate_settings(Settings())
        assert any(f.key == "AUTH_ENABLED" and f.severity == "error" for f in findings)
