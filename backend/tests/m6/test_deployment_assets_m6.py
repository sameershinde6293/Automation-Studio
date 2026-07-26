"""Regression tests for M6-F4: referenced deployment assets must exist.

``docker-compose.yml`` and ``docs/DEPLOYMENT.md`` both open with
``cp .env.production.example .env``. That file had never been committed —
``.gitignore`` carried a blanket ``.env.*`` rule with only ``!.env.example``
negated, so the template was silently swallowed and the documented quick start
failed at its first step. See docs/M6_VALIDATION_REPORT.md finding M6-F4.

Documentation drift is invisible to a normal test suite, which is precisely why
it survived M5's review. These tests treat "a file the docs tell an operator to
copy" as a build artifact with a contract.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_TEMPLATE = REPO_ROOT / ".env.production.example"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "DEPLOYMENT.md"


class TestProductionEnvTemplateExists:
    def test_template_file_is_present(self):
        assert ENV_TEMPLATE.is_file(), (
            f"{ENV_TEMPLATE.name} is referenced by docker-compose.yml and "
            "docs/DEPLOYMENT.md but does not exist"
        )

    def test_template_is_not_gitignored(self):
        """The blanket ``.env.*`` rule is what hid this file in M5."""
        result = subprocess.run(
            ["git", "check-ignore", str(ENV_TEMPLATE)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # git check-ignore exits 0 when the path IS ignored.
        assert result.returncode != 0, (
            ".env.production.example is matched by a .gitignore rule, so it "
            "will never be committed and the documented quick start will fail"
        )

    def test_template_is_tracked_by_git(self):
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(ENV_TEMPLATE)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, "template exists but is untracked"


class TestProductionEnvTemplateContent:
    @pytest.fixture
    def template_text(self):
        if not ENV_TEMPLATE.is_file():
            pytest.skip("template missing; covered by TestProductionEnvTemplateExists")
        return ENV_TEMPLATE.read_text()

    @pytest.mark.parametrize(
        "key",
        [
            "ENVIRONMENT",
            "AUTH_ENABLED",
            "AUTH_SECRET_KEY",
            "CORS_ORIGINS",
            "ALLOWED_HOSTS",
            "POSTGRES_PASSWORD",
            "LOG_FORMAT",
            "RATE_LIMIT_ENABLED",
        ],
    )
    def test_documents_the_settings_deployment_requires(self, template_text, key):
        assert re.search(rf"^#?\s*{key}=", template_text, re.MULTILINE), (
            f"{key} is required by docs/DEPLOYMENT.md but absent from the template"
        )

    def test_defaults_to_production_environment(self, template_text):
        assert re.search(r"^ENVIRONMENT=production\s*$", template_text, re.MULTILINE)

    def test_enables_authentication(self, template_text):
        """AUTH_ENABLED=false in production is a hard startup error."""
        assert re.search(r"^AUTH_ENABLED=true\s*$", template_text, re.MULTILINE)

    def test_ships_no_real_secret(self, template_text):
        """A committed template must never carry a usable credential.

        The value is everything before an inline ``#`` comment, so a trailing
        "how to generate this" hint does not read as a baked-in secret.
        """
        for key in ("AUTH_SECRET_KEY", "POSTGRES_PASSWORD"):
            match = re.search(rf"^{key}=(.*)$", template_text, re.MULTILINE)
            assert match, f"{key} missing"
            value = match.group(1).split("#", 1)[0].strip()
            assert value == "", (
                f"{key} has a baked-in value ({value!r}); every deployment "
                "following the template would share the same secret"
            )

    def test_dangerous_executors_default_to_disabled(self, template_text):
        for flag in (
            "ALLOW_SHELL_EXECUTOR",
            "ALLOW_PYTHON_EXECUTOR",
            "ALLOW_JAVASCRIPT_EXECUTOR",
            "ALLOW_DATABASE_EXECUTOR",
            "HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS",
        ):
            assert re.search(rf"^{flag}=false\s*$", template_text, re.MULTILINE), (
                f"{flag} must default to false in the production template"
            )

    def test_cors_origins_is_not_a_wildcard(self, template_text):
        match = re.search(r"^CORS_ORIGINS=(.*)$", template_text, re.MULTILINE)
        assert match and "*" not in match.group(1)

    def test_template_parses_as_settings(self, template_text, tmp_path, monkeypatch):
        """The template must actually load — this is M6-F1 and M6-F4 combined.

        A template full of comma-separated origins is worthless if the loader
        rejects it, which was exactly the M5 situation.
        """
        from app.infrastructure.config.settings import Settings

        for name in (
            "ENVIRONMENT",
            "LOG_LEVEL",
            "RATE_LIMIT_ENABLED",
            "CORS_ORIGINS",
            "ALLOWED_HOSTS",
        ):
            monkeypatch.delenv(name, raising=False)

        # Strip commented lines and supply the two mandatory secrets.
        lines = [
            line
            for line in template_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        rendered = "\n".join(lines)
        rendered = rendered.replace("AUTH_SECRET_KEY=", "AUTH_SECRET_KEY=" + "s" * 48)
        rendered = rendered.replace("POSTGRES_PASSWORD=", "POSTGRES_PASSWORD=pw")

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(rendered + "\n")

        settings = Settings()
        assert settings.is_production
        assert settings.AUTH_ENABLED is True
        assert settings.CORS_ORIGINS and "*" not in settings.CORS_ORIGINS
        assert settings.ALLOWED_HOSTS != ["*"]

    def test_rendered_template_passes_startup_validation(
        self, template_text, tmp_path, monkeypatch
    ):
        """The shipped template must not itself trip a blocking finding.

        If following the documentation produces a config the app refuses to
        start, the template is wrong. Warnings are acceptable; errors are not.
        """
        from app.core.startup import SEVERITY_ERROR, validate_settings
        from app.infrastructure.config.settings import Settings

        for name in (
            "ENVIRONMENT",
            "LOG_LEVEL",
            "RATE_LIMIT_ENABLED",
            "CORS_ORIGINS",
            "ALLOWED_HOSTS",
        ):
            monkeypatch.delenv(name, raising=False)

        lines = [
            line
            for line in template_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        rendered = "\n".join(lines)
        rendered = rendered.replace("AUTH_SECRET_KEY=", "AUTH_SECRET_KEY=" + "s" * 48)
        rendered = rendered.replace("POSTGRES_PASSWORD=", "POSTGRES_PASSWORD=pw")
        rendered += "\nDATABASE_URL=postgresql+psycopg://creator:pw@db:5432/creator_os\n"

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(rendered + "\n")

        blocking = [
            f for f in validate_settings(Settings()) if f.severity == SEVERITY_ERROR
        ]
        assert blocking == [], (
            "the shipped production template trips blocking startup findings: "
            f"{[f.key for f in blocking]}"
        )


class TestDocumentationReferencesResolve:
    """Every file the deployment docs tell an operator to copy must exist."""

    @pytest.mark.parametrize(
        "source,filename",
        [
            (COMPOSE_FILE, ".env.production.example"),
            (DEPLOYMENT_DOC, ".env.production.example"),
        ],
    )
    def test_referenced_file_exists(self, source, filename):
        if not source.is_file():
            pytest.skip(f"{source} not present")
        if filename not in source.read_text():
            pytest.skip(f"{source.name} no longer references {filename}")
        assert (REPO_ROOT / filename).is_file(), (
            f"{source.name} references {filename}, which does not exist"
        )

    def test_compose_and_dockerfile_are_present(self):
        assert COMPOSE_FILE.is_file()
        assert (REPO_ROOT / "backend" / "Dockerfile").is_file()
        assert (REPO_ROOT / "frontend" / "Dockerfile").is_file()
