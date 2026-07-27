"""Regression tests for M7-F2: custom settings sources must keep their config.

The defect
----------
M6 replaced the two standard settings sources with list-friendly subclasses so
that ``CORS_ORIGINS=a,b`` would parse (M6-F1). It constructed them like this::

    _ListFriendlyEnvSource(settings_cls)
    _ListFriendlyDotEnvSource(settings_cls)

Passing only ``settings_cls`` means every other constructor argument falls back
to its default, **throwing away the configuration pydantic-settings had already
resolved** for the sources being replaced — most importantly the per-instance
``_env_file`` override handed to ``Settings(...)``.

So ``Settings(_env_file="/path/to/other.env")`` silently ignored the file and
returned defaults. The module-level singleton passes no overrides, so nothing
in normal operation exposed it, and it shipped in M6.

Found in M7 while writing the M7-F1 regression tests: those tests build a
``Settings`` against a temporary ``.env`` and kept reporting ``development``
even though the M7-F1 fix was correct. The constructor argument was never
reaching the source.

Why it is worth a test of its own
---------------------------------
This is the seam where a library contract is overridden by hand. A future
change that adds a constructor argument, or re-simplifies these calls back to
``Source(settings_cls)``, would silently break configuration loading again
without failing any other test. These tests assert the seam directly.
"""

from __future__ import annotations

import pytest

from app.infrastructure.config.settings import Settings


@pytest.fixture
def clean_env(monkeypatch):
    """Remove ambient variables that would mask the file under test."""
    for key in ("ENVIRONMENT", "AUTH_ENABLED", "ENABLE_DOCS", "DATABASE_URL",
                "CREATOR_OS_ENV_FILE", "LOG_LEVEL", "CORS_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestEnvFileOverrideReachesTheSource:
    """The exact contract M6 broke."""

    def test_explicit_env_file_is_honoured(self, clean_env, tmp_path):
        env_file = tmp_path / "custom.env"
        env_file.write_text("ENVIRONMENT=staging\n", encoding="utf-8")
        assert Settings(_env_file=str(env_file)).ENVIRONMENT == "staging"

    def test_explicit_env_file_loads_every_field(self, clean_env, tmp_path):
        """Not just one field — the whole file must be read."""
        env_file = tmp_path / "full.env"
        env_file.write_text(
            "ENVIRONMENT=production\n"
            "AUTH_ENABLED=true\n"
            "ENABLE_DOCS=false\n"
            "LOG_LEVEL=WARNING\n"
            "DATABASE_URL=postgresql+psycopg://u:p@db:5432/creator\n",
            encoding="utf-8",
        )
        loaded = Settings(_env_file=str(env_file))

        assert loaded.ENVIRONMENT == "production"
        assert loaded.AUTH_ENABLED is True
        assert loaded.ENABLE_DOCS is False
        assert loaded.LOG_LEVEL == "WARNING"
        assert loaded.DATABASE_URL.endswith("/creator")

    def test_env_file_list_is_honoured_in_order(self, clean_env, tmp_path):
        """A list of files must load, with later entries winning."""
        first = tmp_path / "base.env"
        second = tmp_path / "override.env"
        first.write_text("ENVIRONMENT=production\nLOG_LEVEL=INFO\n", encoding="utf-8")
        second.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

        loaded = Settings(_env_file=[str(first), str(second)])
        assert loaded.ENVIRONMENT == "production"  # from the first file
        assert loaded.LOG_LEVEL == "DEBUG"         # overridden by the second

    def test_missing_explicit_env_file_falls_back_to_defaults(
        self, clean_env, tmp_path
    ):
        """A nonexistent path is ignored, not an error."""
        loaded = Settings(_env_file=str(tmp_path / "absent.env"))
        assert loaded.ENVIRONMENT == "development"


class TestM6ListParsingStillWorks:
    """The M6-F1 behaviour these subclasses exist for must be preserved."""

    def test_comma_separated_list_from_env_file(self, clean_env, tmp_path):
        env_file = tmp_path / "list.env"
        env_file.write_text(
            "CORS_ORIGINS=https://a.example.com,https://b.example.com\n",
            encoding="utf-8",
        )
        assert Settings(_env_file=str(env_file)).CORS_ORIGINS == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_json_array_from_env_file(self, clean_env, tmp_path):
        env_file = tmp_path / "json.env"
        env_file.write_text(
            'CORS_ORIGINS=["https://a.example.com","https://b.example.com"]\n',
            encoding="utf-8",
        )
        assert Settings(_env_file=str(env_file)).CORS_ORIGINS == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_comma_separated_list_from_process_environment(self, clean_env):
        clean_env.setenv("CORS_ORIGINS", "https://x.example.com,https://y.example.com")
        assert Settings().CORS_ORIGINS == [
            "https://x.example.com",
            "https://y.example.com",
        ]

    def test_empty_list_value_yields_empty_list(self, clean_env, tmp_path):
        env_file = tmp_path / "empty.env"
        env_file.write_text("CORS_ORIGINS=\n", encoding="utf-8")
        assert Settings(_env_file=str(env_file)).CORS_ORIGINS == []


class TestPrecedenceContract:
    """``init > env > .env > secrets`` must survive the source swap."""

    def test_init_argument_beats_everything(self, clean_env, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("ENVIRONMENT=production\n", encoding="utf-8")
        clean_env.setenv("ENVIRONMENT", "staging")
        assert Settings(_env_file=str(env_file), ENVIRONMENT="testing").ENVIRONMENT == (
            "testing"
        )

    def test_process_env_beats_env_file(self, clean_env, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("ENVIRONMENT=production\n", encoding="utf-8")
        clean_env.setenv("ENVIRONMENT", "staging")
        assert Settings(_env_file=str(env_file)).ENVIRONMENT == "staging"
