"""Unit tests for core.license_manager.LicenseManager (+ admin KeyGenerator).

HWID sources are monkeypatched to deterministic component lists; key
round-trips exercise the REAL Fernet/PBKDF2 derivation shared between
core.license_manager and license_tool.key_generator (RULE 1 duplication
guard: the two derivations must stay byte-identical).
"""

from __future__ import annotations

import base64
import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.license_manager as lm_mod
import license_tool.key_generator as kg_mod
from core.license_manager import (
    ADMIN_PASSWORD,
    KEY_VERSION,
    TRIAL_DAYS,
    LicenseManager,
)
from license_tool.key_generator import KeyGenerator

FIXED_COMPONENTS = [
    ("machine_guid", "FAKE-GUID-1234"),
    ("cpu", "BFEBFBFF000906E9"),
    ("mb", "SN-TEST-BOARD"),
    ("mac", "0x525400123456"),
]

HWID_RE = re.compile(r"^([0-9A-F]{4}-){5}[0-9A-F]{4}$")


@pytest.fixture
def lm(production_container) -> LicenseManager:
    """LicenseManager on the isolated container's database."""
    return LicenseManager(production_container)


@pytest.fixture
def lm_fixed(lm: LicenseManager, monkeypatch: pytest.MonkeyPatch) -> LicenseManager:
    """LicenseManager whose HWID sources are deterministic."""
    monkeypatch.setattr(lm, "_collect_hwid_components", lambda: list(FIXED_COMPONENTS))
    return lm


@pytest.fixture
def generator(tmp_path: Path) -> KeyGenerator:
    """KeyGenerator writing its history CSV into tmp."""
    return KeyGenerator(history_path=tmp_path / "key_history.csv")


def _backdate_trial(lm: LicenseManager, days_ago: int) -> None:
    date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    lm.db.db.execute(
        "UPDATE license_data SET trial_start_date = ? WHERE id = 1", (date,)
    )


class TestHwid:
    def test_format_and_determinism(self, lm_fixed: LicenseManager) -> None:
        hwid_a = lm_fixed.generate_hwid()
        hwid_b = lm_fixed.generate_hwid()
        assert hwid_a == hwid_b
        assert HWID_RE.match(hwid_a), f"bad HWID format: {hwid_a}"

    def test_component_order_independent(
        self, lm: LicenseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            lm, "_collect_hwid_components", lambda: list(FIXED_COMPONENTS)
        )
        original = lm.generate_hwid()
        monkeypatch.setattr(
            lm,
            "_collect_hwid_components",
            lambda: list(reversed(FIXED_COMPONENTS)),
        )
        assert lm.generate_hwid() == original

    def test_default_values_filtered(
        self, lm: LicenseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        noisy = FIXED_COMPONENTS + [
            ("mb2", "To be filled by O.E.M."),
            ("cpu2", ""),
            ("fan", "None"),
            ("board", "Default string"),
        ]
        monkeypatch.setattr(lm, "_collect_hwid_components", lambda: noisy)
        hwid_noisy = lm.generate_hwid()
        monkeypatch.setattr(
            lm, "_collect_hwid_components", lambda: list(FIXED_COMPONENTS)
        )
        assert hwid_noisy == lm.generate_hwid()

    def test_component_names_bind_value(
        self, lm: LicenseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lm, "_collect_hwid_components", lambda: [("cpu", "X")])
        hwid_cpu = lm.generate_hwid()
        monkeypatch.setattr(lm, "_collect_hwid_components", lambda: [("mb", "X")])
        assert lm.generate_hwid() != hwid_cpu

    def test_real_machine_hwid_format(self, lm: LicenseManager) -> None:
        hwid = lm.generate_hwid()  # real sources on this machine
        assert HWID_RE.match(hwid)


class TestTrialLifecycle:
    def test_initialize_over_uninitialized_row(self, lm_fixed: LicenseManager) -> None:
        result = lm_fixed.initialize_license()
        assert result["success"] is True
        data = result["data"]
        assert HWID_RE.match(data["hwid"])
        status = data["status"]
        assert status["status"] == "trial"
        assert status["days_remaining"] == TRIAL_DAYS
        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["hwid"] == data["hwid"]  # UNINITIALIZED replaced
        assert row["trial_start_date"] is not None
        assert row["tamper_hash"] is not None

    def test_initialize_idempotent(self, lm_fixed: LicenseManager) -> None:
        first = lm_fixed.initialize_license()["data"]
        second = lm_fixed.initialize_license()["data"]
        assert first["hwid"] == second["hwid"]
        assert second["status"]["days_remaining"] == TRIAL_DAYS

    def test_trial_countdown(self, lm_fixed: LicenseManager) -> None:
        lm_fixed.initialize_license()
        _backdate_trial(lm_fixed, 10)
        data = lm_fixed.check_license()["data"]
        assert data["status"] == "trial"
        assert data["days_remaining"] == TRIAL_DAYS - 10
        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["days_remaining"] == TRIAL_DAYS - 10  # DB kept in sync

    def test_trial_expired(self, lm_fixed: LicenseManager) -> None:
        lm_fixed.initialize_license()
        _backdate_trial(lm_fixed, TRIAL_DAYS + 10)
        data = lm_fixed.check_license()["data"]
        assert data["status"] == "expired"
        assert data["days_remaining"] == 0

    def test_get_days_remaining(self, lm_fixed: LicenseManager) -> None:
        lm_fixed.initialize_license()
        _backdate_trial(lm_fixed, 5)
        assert lm_fixed.get_days_remaining() == TRIAL_DAYS - 5


class TestActivation:
    def test_wrong_admin_password(self, generator: KeyGenerator) -> None:
        result = generator.generate_key(
            "A3F7-0000-0000-0000-0000-0000", 30, "n", "nope"
        )
        assert result["success"] is False
        assert "password" in (result["error"] or "").lower()
        assert generator.verify_admin_password("nope") is False
        assert generator.verify_admin_password(ADMIN_PASSWORD) is True

    def test_round_trip_activation(
        self, lm_fixed: LicenseManager, generator: KeyGenerator
    ) -> None:
        lm_fixed.initialize_license()
        hwid = lm_fixed.generate_hwid()
        generated = generator.generate_key(hwid, 30, "round-trip user", ADMIN_PASSWORD)
        assert generated["success"] is True
        key = generated["data"]["key"]

        result = lm_fixed.activate_license(key)
        assert result["success"] is True, result.get("error")
        assert result["data"]["days_remaining"] == 30
        assert result["data"]["status"] == "active"

        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["is_activated"] == 1 and row["is_trial"] == 0
        assert row["license_key"] == key
        assert row["days_granted"] == 30
        assert row["user_note"] == "round-trip user"
        assert row["activation_count"] == 1
        assert row["activation_date"] is not None
        assert row["expiry_date"] == generated["data"]["expiry"]
        expected = lm_fixed.generate_tamper_hash(row)
        assert row["tamper_hash"] == expected

    def test_hwid_mismatch_rejected(
        self, lm_fixed: LicenseManager, generator: KeyGenerator
    ) -> None:
        lm_fixed.initialize_license()
        generated = generator.generate_key(
            "0000-1111-2222-3333-4444-5555", 30, "other pc", ADMIN_PASSWORD
        )
        result = lm_fixed.activate_license(generated["data"]["key"])
        assert result["success"] is False
        assert "hardware" in (result["error"] or "").lower()
        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["is_activated"] == 0  # untouched

    def test_garbage_key_rejected(self, lm_fixed: LicenseManager) -> None:
        lm_fixed.initialize_license()
        for garbage in ("AAAA-BBBB-CCCC", "!!!!", "", "XXXX-~-~~~~"):
            result = lm_fixed.activate_license(garbage)
            assert result["success"] is False
            assert result["error"]

    def test_expired_key_rejected(
        self, lm_fixed: LicenseManager, generator: KeyGenerator
    ) -> None:
        lm_fixed.initialize_license()
        hwid = lm_fixed.generate_hwid()
        generated = generator.generate_key(hwid, 0, "zero-day", ADMIN_PASSWORD)
        assert generated["success"] is True  # generation of 0-day key is legal
        result = lm_fixed.activate_license(generated["data"]["key"])
        assert result["success"] is False
        assert "expired" in (result["error"] or "").lower()

    def test_wrong_version_rejected(
        self, lm_fixed: LicenseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lm_fixed.initialize_license()
        hwid = lm_fixed.generate_hwid()
        expiry = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
        payload = {
            "hwid": hwid,
            "expiry": expiry,
            "days": 30,
            "note": "future version",
            "version": "2",  # not KEY_VERSION
            "created": datetime.utcnow().strftime("%Y-%m-%d"),
        }
        assert payload["version"] != KEY_VERSION  # sanity: really a bad version
        from cryptography.fernet import Fernet

        fernet = Fernet(kg_mod._derive_fernet_key())
        token = (
            base64.urlsafe_b64encode(fernet.encrypt(json.dumps(payload).encode()))
            .decode()
            .replace("-", "~")
        )
        key = "-".join(token[i : i + 4] for i in range(0, len(token), 4))
        result = lm_fixed.activate_license(key)
        assert result["success"] is False
        assert "version" in (result["error"] or "").lower()

    def test_check_license_after_activation(
        self, lm_fixed: LicenseManager, generator: KeyGenerator
    ) -> None:
        lm_fixed.initialize_license()
        key = generator.generate_key(
            lm_fixed.generate_hwid(), 45, "long", ADMIN_PASSWORD
        )["data"]["key"]
        assert lm_fixed.activate_license(key)["success"] is True
        data = lm_fixed.check_license()["data"]
        assert data["status"] == "active"
        assert data["days_remaining"] == 45
        assert data["is_activated"] is True and data["is_trial"] is False

    def test_cryptography_missing_graceful(
        self, lm_fixed: LicenseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lm_mod, "_load_fernet", lambda: None)
        result = lm_fixed.activate_license("AAAA-BBBB-CCCC")
        assert result["success"] is False
        assert "decrypt" in (result["error"] or "").lower()


class TestTamperProtection:
    def test_db_tamper_detected(
        self, lm_fixed: LicenseManager, generator: KeyGenerator
    ) -> None:
        lm_fixed.initialize_license()
        key = generator.generate_key(
            lm_fixed.generate_hwid(), 30, "victim", ADMIN_PASSWORD
        )["data"]["key"]
        assert lm_fixed.activate_license(key)["success"] is True
        # Attacker extends expiry directly in the DB, not knowing the hash.
        lm_fixed.db.db.execute(
            "UPDATE license_data SET expiry_date = '2099-01-01',"
            " days_remaining = 9999 WHERE id = 1"
        )
        data = lm_fixed.check_license()["data"]
        assert data["status"] == "invalid"
        assert data["days_remaining"] == 0
        assert "tamper" in (data.get("reason") or "").lower()

    def test_clock_no_baseline(self, lm_fixed: LicenseManager) -> None:
        assert lm_fixed.detect_clock_tampering() is False
        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["last_check_date"] is not None  # baseline stamped

    def test_clock_tampering_detected(self, lm_fixed: LicenseManager) -> None:
        future = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        lm_fixed.db.db.execute(
            "UPDATE license_data SET last_check_date = ? WHERE id = 1", (future,)
        )
        assert lm_fixed.detect_clock_tampering() is True
        # Evidence preserved: tampered check must NOT rewrite the baseline.
        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["last_check_date"] == future

    def test_clock_normal_progression(self, lm_fixed: LicenseManager) -> None:
        past = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        lm_fixed.db.db.execute(
            "UPDATE license_data SET last_check_date = ? WHERE id = 1", (past,)
        )
        assert lm_fixed.detect_clock_tampering() is False
        row = lm_fixed.db.db.fetch_one("SELECT * FROM license_data WHERE id = 1")
        assert row["last_check_date"] > past  # baseline advanced


class TestKeyGenerator:
    def test_shared_derivation_byte_identical(self) -> None:
        assert lm_mod._derive_fernet_key() == kg_mod._derive_fernet_key()
        assert lm_mod.KEY_VERSION == kg_mod.KEY_VERSION
        assert lm_mod.ENCRYPTION_SALT == kg_mod.ENCRYPTION_SALT
        assert lm_mod.ADMIN_PASSWORD == kg_mod.ADMIN_PASSWORD

    def test_key_format_groups(self, generator: KeyGenerator) -> None:
        result = generator.generate_key(
            "A3F7-1234-5678-9ABC-DEF0-0000", 30, "fmt", ADMIN_PASSWORD
        )
        key = result["data"]["key"]
        groups = key.split("-")
        assert all(len(group) == 4 for group in groups[:-1])
        assert 1 <= len(groups[-1]) <= 4
        # Payload '-' must have been sanitized away; only separators remain.
        assert re.fullmatch(r"[A-Za-z0-9_~=]", key[0]) is not None

    def test_history_csv_written(self, generator: KeyGenerator) -> None:
        generator.generate_key("HWID-ONE-0000-0000-0000-0000", 10, "a", ADMIN_PASSWORD)
        generator.generate_key("HWID-TWO-0000-0000-0000-0000", 20, "b", ADMIN_PASSWORD)
        with generator.history_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert rows[0] == ["date", "hwid", "days", "user_note", "key", "expiry_date"]
        assert len(rows) == 3  # header + 2 generated keys
        assert rows[1][1] == "HWID-ONE-0000-0000-0000-0000"
        assert rows[1][2] == "10"
        assert rows[2][2] == "20"

    def test_invalid_days_rejected(self, generator: KeyGenerator) -> None:
        for bad_days in ("thirty", None, -5):
            result = generator.generate_key(
                "HWID-X", bad_days, "n", ADMIN_PASSWORD  # type: ignore[arg-type]
            )
            assert result["success"] is False
            assert result["error"]
