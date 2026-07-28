"""M9-F3: backup/restore scripts must never report success without a backup.

The staging run produced a backup directory that the script reported as
successful (exit 0) while it contained no database dump at all: pg_dump was
absent from PATH and the failure was swallowed by an `|| echo` and a bare
warning. The defect is only observable at restore time, during an incident.

These tests assert on the script text because the behaviour lives in shell,
and executing a full pg_dump in unit tests would require a live PostgreSQL.
The end-to-end drill (dump -> drop -> restore -> verify row counts) is
recorded in docs/M9_VALIDATION_REPORT.md.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKUP = REPO_ROOT / "scripts" / "backup.sh"
RESTORE = REPO_ROOT / "scripts" / "restore.sh"


@pytest.fixture(scope="module")
def backup_text() -> str:
    return BACKUP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def restore_text() -> str:
    return RESTORE.read_text(encoding="utf-8")


class TestScriptsExist:
    def test_both_scripts_are_executable(self):
        for script in (BACKUP, RESTORE):
            assert script.exists(), f"{script} is missing"
            assert script.stat().st_mode & 0o111, f"{script} is not executable"

    def test_scripts_are_valid_bash(self):
        for script in (BACKUP, RESTORE):
            result = subprocess.run(
                ["bash", "-n", str(script)], capture_output=True, text=True
            )
            assert result.returncode == 0, (
                f"{script.name} is not valid bash: {result.stderr}"
            )


class TestBackupFailsLoudly:
    def test_missing_pg_dump_is_fatal(self, backup_text):
        """The old code printed '⚠ pg_dump not available' and exited 0."""
        assert "⚠ pg_dump not available" not in backup_text
        assert "❌ pg_dump not found" in backup_text
        # The error branch must terminate the script.
        section = backup_text.split("❌ pg_dump not found", 1)[1][:800]
        assert "exit 1" in section

    def test_pg_dump_failure_is_fatal(self, backup_text):
        assert '|| echo "⚠ pg_dump failed"' not in backup_text
        assert "❌ pg_dump failed" in backup_text
        section = backup_text.split("❌ pg_dump failed", 1)[1][:400]
        assert "exit 1" in section

    def test_archive_integrity_is_verified(self, backup_text):
        """A corrupt or empty dump must not pass as a backup."""
        assert "gunzip -t" in backup_text
        assert "❌ Backup archive is corrupt" in backup_text
        assert "❌ Backup archive is suspiciously small" in backup_text

    def test_backup_without_a_database_archive_is_fatal(self, backup_text):
        assert "this backup is NOT restorable" in backup_text
        tail = backup_text.split("NOT restorable", 1)[1][:200]
        assert "exit 1" in tail

    def test_uses_database_url_rather_than_reassembling_it(self, backup_text):
        """Rebuilding from POSTGRES_* dropped host and port (wrong cluster)."""
        assert 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' not in backup_text
        assert "PG_URL" in backup_text
        # SQLAlchemy driver suffixes are not understood by libpq.
        assert "postgresql+psycopg" in backup_text

    def test_media_backup_honours_media_root(self, backup_text):
        """MEDIA_ROOT is configurable; the old code only knew two fixed paths."""
        assert "MEDIA_ROOT=" in backup_text
        assert "MEDIA_DIR" in backup_text

    def test_manifest_version_is_parsed_correctly(self, backup_text):
        """`grep __version__` matched the alias line too and emitted junk."""
        assert "cat backend/app/version.py" not in backup_text
        assert re.search(r"grep -E '\^__version__'", backup_text)

    def test_manifest_records_checksums(self, backup_text):
        assert "sha256sum" in backup_text


class TestRestoreFailsLoudly:
    def test_missing_database_archive_is_fatal(self, restore_text):
        assert "⚠ No database backup found" not in restore_text
        assert "❌ No database backup found" in restore_text

    def test_restore_does_not_guess_the_target_database(self, restore_text):
        """`psql "${DATABASE_URL:-}"` silently fell back to a local socket."""
        # Ignore comment lines: the fix documents the old form in a comment.
        code = "\n".join(
            line for line in restore_text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert 'psql "${DATABASE_URL:-}"' not in code
        assert "refusing to guess the target database" in restore_text

    def test_partial_restore_is_an_error(self, restore_text):
        """Without ON_ERROR_STOP psql exits 0 on a half-applied dump."""
        assert "ON_ERROR_STOP=1" in restore_text
        assert "❌ Restore failed" in restore_text

    def test_corrupt_archive_is_rejected_before_touching_the_database(
        self, restore_text
    ):
        assert "gunzip -t" in restore_text
        assert "aborting before touching the database" in restore_text

    def test_credentials_are_redacted_in_output(self, restore_text):
        """The target line must not print the password."""
        assert ":***@" in restore_text

    def test_media_restore_honours_media_root(self, restore_text):
        assert "MEDIA_ROOT" in restore_text
        assert "MEDIA_DIR" in restore_text

    def test_falls_back_to_a_bundled_psql(self, restore_text):
        """Hosts without PostgreSQL client packages still need to restore."""
        assert "pgserver/pginstall/bin/psql" in restore_text
