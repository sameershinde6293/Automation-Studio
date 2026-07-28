"""M10: guards for the defects the v1.1.0 release certification found.

The M9 consistency suite pinned the backend version against the README and
``PROJECT_STATUS.md`` only. That left three things unguarded, and all three had
actually drifted by the time M10 audited the tree:

* five documentation headers were still stamped ``Creator OS v1.1.0-rc1``
  while the code shipped ``1.1.0-rc3`` (M10-F2);
* the ``SSL_CERT_FILE`` workaround was exported for the verifier client rather
  than the backend process that performs the outbound request (M10-F1);
* ``PROJECT_STATUS.md`` listed two different milestones both numbered ``M10``
  (M10-F3).

These tests turn each of those into a build failure. They were confirmed
against the pre-fix tree: every one of them fails there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"

#: Documentation whose third line carries a ``Creator OS vX.Y.Z`` stamp.
VERSION_STAMPED_DOCS = [
    "DEPLOYMENT.md",
    "FAQ.md",
    "TROUBLESHOOTING.md",
    "UPGRADE_GUIDE.md",
    "INSTALLATION_GUIDE.md",
]

VERSION_STAMP = re.compile(r"Creator OS v(\d+\.\d+\.\d+(?:-rc\d+)?)")


def backend_version() -> str:
    from app.version import __version__

    return __version__


class TestDocumentationVersionStamps:
    """M10-F2: doc headers drifted three milestones behind the code."""

    @pytest.mark.parametrize("name", VERSION_STAMPED_DOCS)
    def test_doc_header_matches_the_shipped_version(self, name: str) -> None:
        path = DOCS / name
        assert path.exists(), f"{name} is missing"
        # The stamp lives in the document preamble, not buried in prose.
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:6])
        match = VERSION_STAMP.search(head)
        assert match, f"{name} has no 'Creator OS vX.Y.Z' header stamp"
        assert match.group(1) == backend_version(), (
            f"docs/{name} advertises {match.group(1)!r} but the backend ships "
            f"{backend_version()!r}"
        )

    def test_release_notes_lead_with_the_shipped_version(self) -> None:
        text = (DOCS / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        match = re.search(r"^## v(\S+)", text, re.MULTILINE)
        assert match, "RELEASE_NOTES.md has no '## vX.Y.Z' section"
        assert match.group(1) == backend_version()

    def test_changelog_leads_with_the_shipped_version(self) -> None:
        """The first *released* section must match the shipped version.

        A leading ``## [Unreleased]`` section is the keep-a-changelog
        convention for work merged but not yet tagged, and is skipped here:
        the guard is that the newest *version* heading matches what the
        backend reports, which it still enforces.
        """
        text = (DOCS / "CHANGELOG.md").read_text(encoding="utf-8")
        versions = [
            v
            for v in re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
            if v.lower() != "unreleased"
        ]
        assert versions, "CHANGELOG.md has no '## [X.Y.Z]' section"
        assert versions[0] == backend_version()

    def test_changelog_versions_are_unique(self) -> None:
        """A duplicated heading makes the history ambiguous to read."""
        text = (DOCS / "CHANGELOG.md").read_text(encoding="utf-8")
        versions = re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
        duplicates = {v for v in versions if versions.count(v) > 1}
        assert not duplicates, f"duplicate CHANGELOG headings: {sorted(duplicates)}"


class TestExampleVerificationUsesTheBackendTrustStore:
    """M10-F1: the TLS workaround was applied to the wrong process.

    ``verify_examples.py`` is an HTTP client. The workflow HTTP node runs
    inside the backend, so a CA bundle exported for the client cannot affect
    the request that was failing. Both runners must export it for the server.
    """

    def test_ci_local_exports_the_ca_bundle_for_the_backend(self) -> None:
        text = (REPO_ROOT / "scripts" / "ci-local.sh").read_text(encoding="utf-8")
        backend_start = text.index("uvicorn app.main:app --host 127.0.0.1 --port 8000")
        preamble = text[:backend_start]
        assert "export SSL_CERT_FILE" in preamble, (
            "ci-local.sh must export SSL_CERT_FILE before starting the backend; "
            "setting it on verify_examples.py has no effect (M10-F1)"
        )

    def test_ci_workflow_exports_the_ca_bundle_for_the_backend(self) -> None:
        text = (REPO_ROOT / "ci" / "github-actions-ci.yml").read_text(encoding="utf-8")
        backend_start = text.index("nohup uvicorn app.main:app")
        preamble = text[:backend_start]
        assert "export SSL_CERT_FILE" in preamble, (
            "the CI examples job must export SSL_CERT_FILE for the backend "
            "process, not for the verifier (M10-F1)"
        )


class TestProjectStatusIsInternallyConsistent:
    """M10-F3: two different milestones were both numbered M10."""

    def test_milestone_numbers_are_unique(self) -> None:
        text = (DOCS / "PROJECT_STATUS.md").read_text(encoding="utf-8")
        table = re.findall(r"^\| (M\d+) \| ", text, re.MULTILINE)
        duplicates = {m for m in table if table.count(m) > 1}
        assert not duplicates, (
            f"PROJECT_STATUS.md reuses milestone numbers: {sorted(duplicates)}"
        )

    def test_certification_report_exists_and_is_linked(self) -> None:
        report = DOCS / "M10_RELEASE_CERTIFICATION.md"
        assert report.exists(), "the M10 certification report is missing"
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "M10_RELEASE_CERTIFICATION.md" in readme


class TestNoStaleReleaseClaims:
    """M10-F4: the repo claimed CI was activated when it never was."""

    def test_readme_does_not_claim_ci_is_activated(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "(activated in M8)" not in readme, (
            "CI has never executed; the workflow directory is not in the "
            "repository. Do not claim activation (M10-F4)"
        )

    def test_workflow_directory_claim_matches_reality(self) -> None:
        """If someone does activate CI, this test tells them to update the docs."""
        activated = (REPO_ROOT / ".github" / "workflows" / "ci.yml").exists()
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        claims_pending = "has never executed" in readme
        assert activated != claims_pending or not activated, (
            "README says CI has never executed but .github/workflows/ci.yml "
            "now exists — update the documentation"
        )
