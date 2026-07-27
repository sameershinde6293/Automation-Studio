"""Regression tests for M7-F1: ``.env`` must be found from the documented CWD.

The defect
----------
``Settings.model_config`` used ``env_file=".env"``. pydantic-settings resolves
that **relative to the process working directory**. Every guide in this
repository tells an operator to write ``.env`` at the repository root:

    cp .env.production.example .env          # -> <repo>/.env

and then to start the server from ``backend/``:

    cd backend && uvicorn app.main:app       # CWD = <repo>/backend

Those are different directories, so ``<repo>/.env`` was never read. The process
did not fail — it silently fell back to **every default**, which is the worst
possible outcome for the one file that carries the security posture:

* ``ENVIRONMENT``  -> ``development`` (so the production gate never engaged)
* ``AUTH_ENABLED`` -> ``False``       (every caller treated as a local admin)
* ``ENABLE_DOCS``  -> ``True``        (Swagger served publicly)
* ``DATABASE_URL`` -> local SQLite    (PostgreSQL ignored; a stray .db appears)

Startup validation could not save it: that gate only refuses to boot when it
believes it is in production, and ``ENVIRONMENT`` had itself defaulted back to
``development``. M7 reproduced all four outcomes against a real server before
fixing them. See ``docs/M7_RELEASE_AUDIT.md`` finding M7-F1.

What these tests pin
--------------------
Configuration precedence is a security boundary here, so the tests assert the
*resolution rules* rather than re-reading the shipped file:

1. the repository root and ``backend/`` are both searched, located from the
   module's own path so the answer does not depend on the CWD;
2. the working-directory file still wins, so no existing deployment changes
   behaviour;
3. ``CREATOR_OS_ENV_FILE`` overrides everything, and suppresses the search so a
   stray ``.env`` cannot shadow an explicitly chosen file;
4. real ``Settings`` objects built against temporary trees load the values —
   the end-to-end behaviour, not just the candidate list.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.infrastructure.config import settings as settings_module
from app.infrastructure.config.settings import Settings, _candidate_env_files

BACKEND_DIR = Path(settings_module.__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent


class TestCandidateEnvFiles:
    """The search path itself — the thing that was wrong."""

    def test_repository_root_env_is_searched(self, monkeypatch):
        """The exact file every guide tells the operator to create."""
        monkeypatch.delenv("CREATOR_OS_ENV_FILE", raising=False)
        assert str(REPO_ROOT / ".env") in _candidate_env_files()

    def test_backend_directory_env_is_searched(self, monkeypatch):
        monkeypatch.delenv("CREATOR_OS_ENV_FILE", raising=False)
        assert str(BACKEND_DIR / ".env") in _candidate_env_files()

    def test_repo_root_is_found_regardless_of_cwd(self, monkeypatch, tmp_path):
        """The regression in one line: the answer must not depend on the CWD.

        Pre-M7 the search path *was* the CWD, so running from ``backend/`` —
        the documented way — resolved to a file that does not exist.
        """
        monkeypatch.delenv("CREATOR_OS_ENV_FILE", raising=False)

        monkeypatch.chdir(BACKEND_DIR)
        from_backend = _candidate_env_files()
        monkeypatch.chdir(tmp_path)
        from_elsewhere = _candidate_env_files()

        root_env = str(REPO_ROOT / ".env")
        assert root_env in from_backend
        assert root_env in from_elsewhere

    def test_cwd_env_has_highest_precedence(self, monkeypatch, tmp_path):
        """Backwards compatibility: the pre-M7 location must still win.

        pydantic-settings gives later entries precedence, so the CWD file has
        to be last. Widening the search must not change what an existing
        deployment resolves to.
        """
        monkeypatch.delenv("CREATOR_OS_ENV_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        assert _candidate_env_files()[-1] == str(tmp_path / ".env")

    def test_no_duplicate_candidates(self, monkeypatch):
        """Running from the repo root must not list the same file twice."""
        monkeypatch.delenv("CREATOR_OS_ENV_FILE", raising=False)
        monkeypatch.chdir(REPO_ROOT)
        candidates = _candidate_env_files()
        assert len(candidates) == len(set(candidates))

    def test_explicit_override_is_used_alone(self, monkeypatch, tmp_path):
        """An explicit path must not be shadowed by a stray local ``.env``."""
        explicit = tmp_path / "custom.env"
        explicit.write_text("ENVIRONMENT=staging\n", encoding="utf-8")
        monkeypatch.setenv("CREATOR_OS_ENV_FILE", str(explicit))
        monkeypatch.chdir(tmp_path)
        assert _candidate_env_files() == [str(explicit)]

    def test_blank_override_falls_back_to_search(self, monkeypatch):
        """An empty variable is an unset variable, not an empty path."""
        monkeypatch.setenv("CREATOR_OS_ENV_FILE", "   ")
        assert len(_candidate_env_files()) > 1


class TestSettingsLoadFromDiscoveredEnv:
    """End-to-end: a real ``Settings`` object must pick the values up."""

    @staticmethod
    def _isolate(monkeypatch):
        """Drop ambient env vars that would mask the file under test."""
        for key in (
            "ENVIRONMENT",
            "AUTH_ENABLED",
            "ENABLE_DOCS",
            "DATABASE_URL",
            "CREATOR_OS_ENV_FILE",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_env_file_outside_cwd_is_loaded(self, monkeypatch, tmp_path):
        """The M7-F1 scenario, reproduced with a temporary tree."""
        self._isolate(monkeypatch)
        root = tmp_path / "repo"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "ENVIRONMENT=production\n"
            "AUTH_ENABLED=true\n"
            "ENABLE_DOCS=false\n"
            "DATABASE_URL=postgresql+psycopg://u:p@db:5432/creator\n",
            encoding="utf-8",
        )

        # Start from backend/, exactly as the documentation instructs.
        monkeypatch.chdir(backend)
        loaded = Settings(_env_file=[str(root / ".env"), str(backend / ".env")])

        assert loaded.ENVIRONMENT == "production"
        assert loaded.is_production is True
        assert loaded.AUTH_ENABLED is True
        assert loaded.ENABLE_DOCS is False
        assert loaded.DATABASE_URL.startswith("postgresql+psycopg://")

    def test_defaults_are_the_unsafe_ones_the_fix_prevents(self, monkeypatch, tmp_path):
        """Documents *why* a silently missed file was release-blocking.

        With no ``.env`` at all the process still starts, in development mode,
        with authentication off and docs exposed. That is correct for a desktop
        install and catastrophic for a server that believed it had a config.
        """
        self._isolate(monkeypatch)
        monkeypatch.chdir(tmp_path)
        fallback = Settings(_env_file=str(tmp_path / "does-not-exist.env"))

        assert fallback.ENVIRONMENT == "development"
        assert fallback.is_production is False
        assert fallback.AUTH_ENABLED is False
        assert fallback.ENABLE_DOCS is True
        assert fallback.is_sqlite is True

    def test_cwd_env_overrides_repo_root_env(self, monkeypatch, tmp_path):
        """Precedence is load order; assert it rather than trusting it."""
        self._isolate(monkeypatch)
        root = tmp_path / "repo"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text("ENVIRONMENT=production\n", encoding="utf-8")
        (backend / ".env").write_text("ENVIRONMENT=staging\n", encoding="utf-8")

        monkeypatch.chdir(backend)
        loaded = Settings(_env_file=[str(root / ".env"), str(backend / ".env")])
        assert loaded.ENVIRONMENT == "staging"

    def test_real_environment_variables_still_win_over_files(
        self, monkeypatch, tmp_path
    ):
        """Container deployments inject env vars; they must stay authoritative."""
        self._isolate(monkeypatch)
        env_file = tmp_path / ".env"
        env_file.write_text("ENVIRONMENT=production\n", encoding="utf-8")
        monkeypatch.setenv("ENVIRONMENT", "staging")
        assert Settings(_env_file=str(env_file)).ENVIRONMENT == "staging"


class TestConfiguredSearchPathIsWired:
    """Guards against the fix being reverted to a bare relative filename."""

    def test_model_config_uses_absolute_paths(self):
        env_file = Settings.model_config.get("env_file")
        assert not isinstance(env_file, str), (
            "env_file is a bare string again: it would resolve relative to the "
            "working directory and reintroduce M7-F1"
        )
        assert all(os.path.isabs(p) for p in env_file), env_file

    def test_model_config_includes_repository_root(self):
        assert str(REPO_ROOT / ".env") in Settings.model_config.get("env_file")
