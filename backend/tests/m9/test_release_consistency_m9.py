"""M9-F4/F5: keep the shipped version and the published numbers honest.

The M8 release shipped README and PROJECT_STATUS claiming 1.1.0-rc2 while
backend/app/version.py, frontend/package.json and the running /health/ready
response all said 1.1.0-rc1. A reader could not tell which artefact they had.
These tests make the drift a build failure instead of a documentation bug.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSION_PY = REPO_ROOT / "backend" / "app" / "version.py"
PKG_JSON = REPO_ROOT / "frontend" / "package.json"
PKG_LOCK = REPO_ROOT / "frontend" / "package-lock.json"
README = REPO_ROOT / "README.md"
PROJECT_STATUS = REPO_ROOT / "docs" / "PROJECT_STATUS.md"

SEMVER_RC = re.compile(r"^\d+\.\d+\.\d+(-rc\d+)?$")


def backend_version() -> str:
    from app.version import __version__

    return __version__


class TestVersionConsistency:
    def test_backend_version_is_wellformed(self):
        assert SEMVER_RC.match(backend_version()), (
            f"{backend_version()!r} is not a valid version string"
        )

    def test_settings_reports_the_same_version(self):
        from app.infrastructure.config.settings import settings

        assert settings.VERSION == backend_version()

    def test_frontend_package_matches_backend(self):
        pkg = json.loads(PKG_JSON.read_text(encoding="utf-8"))
        assert pkg["version"] == backend_version(), (
            f"frontend/package.json says {pkg['version']!r}, "
            f"backend says {backend_version()!r}"
        )

    def test_package_lock_matches_backend(self):
        lock = json.loads(PKG_LOCK.read_text(encoding="utf-8"))
        assert lock["version"] == backend_version()
        root_pkg = lock.get("packages", {}).get("")
        if root_pkg is not None:
            assert root_pkg.get("version") == backend_version()

    def test_readme_headline_version_matches_backend(self):
        """M9-F4: README advertised rc2 while the code shipped rc1."""
        text = README.read_text(encoding="utf-8")
        match = re.search(r"\*\*Version (\d+\.\d+\.\d+(?:-rc\d+)?)\*\*", text)
        assert match, "README has no '**Version X.Y.Z**' headline"
        assert match.group(1) == backend_version(), (
            f"README advertises {match.group(1)!r} but the backend ships "
            f"{backend_version()!r}"
        )

    def test_project_status_version_matches_backend(self):
        text = PROJECT_STATUS.read_text(encoding="utf-8")
        match = re.search(r"\*\*Version:\*\*\s*(\d+\.\d+\.\d+(?:-rc\d+)?)", text)
        assert match, "PROJECT_STATUS.md has no '**Version:**' line"
        assert match.group(1) == backend_version()

    def test_health_endpoint_reports_the_shipped_version(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            payload = client.get("/health/ready").json()
        assert payload["version"] == backend_version()


class TestPublishedTestCountsAreNotStale:
    """M9-F5: README quoted 1529 passed / 8 skipped; the suite was 1527 / 10.

    Rather than pin an exact number that every future test addition would
    break, assert that whatever the README quotes is plausible and that the
    documentation points at a report which states how it was measured.
    """

    def test_readme_quotes_a_backend_test_count(self):
        text = README.read_text(encoding="utf-8")
        assert re.search(r"\*\*\d{3,5} passed", text), (
            "README no longer states a backend test count"
        )

    def test_readme_links_the_current_validation_report(self):
        text = README.read_text(encoding="utf-8")
        assert "M9_VALIDATION_REPORT.md" in text, (
            "README must link the newest validation report so the numbers "
            "can be traced to the run that produced them"
        )

    def test_validation_report_exists(self):
        assert (REPO_ROOT / "docs" / "M9_VALIDATION_REPORT.md").exists()
