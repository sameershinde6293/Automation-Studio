"""License manager: HWID generation, trial handling, key activation.

Core protection component (CAN BE DISABLED: NO). Lives in ``core/`` per
spec (not a pipeline module; intentionally absent from
config/modules_config.json). Spec source: modules_specification.txt
MODULE 13 LICENSE MANAGER.

The matching admin-side key generator is the SEPARATE standalone tool
``license_tool/key_generator.py``. Both sides share the same derivation
(PBKDF2HMAC-SHA256, salt=ENCRYPTION_SALT, 100k iterations, password=
ADMIN_PASSWORD) and the same dash-segmented key format.

Dash-safety note (spec clarification): the outer urlsafe base64 layer of
a key may itself contain ``-`` characters, which would collide with the
dash group separators. The generator therefore translates payload ``-``
to ``~`` before segmenting; this module reverses it before decoding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.service_container import BaseModule, ServiceContainer
from core.time_helper import parse_utc, utc_now, utc_now_str

MODULE_NAME = "license_manager"

ADMIN_PASSWORD = "IAMKING"
ADMIN_HINT = "IKNG"
TRIAL_DAYS = 30
KEY_VERSION = "1"
ENCRYPTION_SALT = b"AutoDoku2025Salt"

_PBKDF2_ITERATIONS = 100_000
_DATE_FMT = "%Y-%m-%d"
_CLOCK_TOLERANCE = timedelta(hours=1)
_ROW_ID = 1

# Hardware identifiers that carry no signal and must be filtered out.
_IGNORED_COMPONENT_VALUES = {
    "",
    "none",
    "0",
    "unknown",
    "default string",
    "to be filled by o.e.m.",
    "to be filled by oem",
}

# Placeholder hwid baked into the schema's default license row.
_UNINITIALIZED_HWID = "UNINITIALIZED"


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _derive_fernet_key() -> bytes:
    """Derive the shared Fernet key (PBKDF2HMAC-SHA256, spec constants).

    Keep BYTE-IDENTICAL to license_tool/key_generator.py:_derive_fernet_key
    (RULE 1: the admin tool is standalone; the logic is duplicated on
    purpose and covered by the round-trip tests).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ENCRYPTION_SALT,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(ADMIN_PASSWORD.encode()))


def _load_fernet() -> Optional[Any]:
    """Return a Fernet instance, or None when cryptography is missing."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - exercised via monkeypatch
        return None
    return Fernet(_derive_fernet_key())


def _normalize_license_key(raw_key: str) -> str:
    """Strip grouping separators and restore sanitized '-' characters."""
    squashed = "".join(str(raw_key).split()).replace("-", "")
    return squashed.replace("~", "-")


def _b64decode_unpadded(payload: str) -> bytes:
    """urlsafe_b64decode tolerant of missing '=' padding."""
    padded = payload + "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(padded.encode())


class LicenseManager(BaseModule):
    """Handle license validation, HWID generation, and key management."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize license manager with the DI container."""
        super().__init__(container, MODULE_NAME)

    def is_optional_module(self) -> bool:
        """License protection is mandatory (CAN BE DISABLED: NO)."""
        return False

    # ------------------------------------------------------------------
    # HWID
    # ------------------------------------------------------------------
    def _collect_hwid_components(self) -> List[Tuple[str, str]]:
        """Gather raw hardware identifiers (best effort, per-platform).

        Sources: Windows registry MachineGuid, WMI CPU id, WMI motherboard
        serial, and the MAC address (all platforms). Every source is
        optional; failures are ignored gracefully.
        """
        components: List[Tuple[str, str]] = []

        # Windows Registry: Machine GUID
        try:
            import winreg  # type: ignore[import-not-found]

            key = winreg.OpenKey(  # type: ignore[attr-defined]
                winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
                r"SOFTWARE\Microsoft\Cryptography",
            )
            guid = winreg.QueryValueEx(key, "MachineGuid")[0]  # type: ignore[attr-defined]
            components.append(("machine_guid", str(guid)))
        except (ImportError, OSError):
            pass  # Not Windows or registry unavailable

        # WMI: CPU id + motherboard serial (Windows-only optional package)
        try:
            import wmi  # type: ignore[import-not-found]

            client = wmi.WMI()
            for proc in client.Win32_Processor():
                components.append(("cpu", str(proc.ProcessorId).strip()))
                break
            for board in client.Win32_BaseBoard():
                components.append(("mb", str(board.SerialNumber).strip()))
                break
        except ImportError:
            pass  # wmi package not installed / not Windows
        except Exception:  # WMI service unavailable, COM errors, ...
            self.log.warning("WMI HWID sources unavailable", exc_info=True)

        # MAC address (all platforms)
        try:
            components.append(("mac", hex(uuid.getnode())))
        except Exception:  # pragma: no cover - uuid is stdlib-stable
            self.log.warning("MAC address unavailable", exc_info=True)

        return components

    @staticmethod
    def _is_meaningful_component(value: str) -> bool:
        """Filter empty/default hardware values (spec: 'To be filled...')."""
        return str(value).strip().lower() not in _IGNORED_COMPONENT_VALUES

    def generate_hwid(self) -> str:
        """Generate the unique hardware ID for this PC.

        Components are filtered, sorted for consistency, joined as
        ``keyvalue`` pairs, hashed with SHA-256, and formatted as six
        readable 4-char uppercase groups.

        Returns:
            HWID string like ``A3F7-9B2C-....``.
        """
        components = [
            (key, value)
            for key, value in self._collect_hwid_components()
            if self._is_meaningful_component(value)
        ]
        components.sort(key=lambda item: (item[0], item[1]))
        combined = "".join(f"{key}{value}" for key, value in components)
        digest = hashlib.sha256(combined.encode()).hexdigest()
        return "-".join(digest[i : i + 4].upper() for i in range(0, 24, 4))

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------
    def _fetch_license_row(self) -> Optional[Dict[str, Any]]:
        """Load the single license_data row (id=1) or None."""
        return self.db.db.fetch_one(
            "SELECT * FROM license_data WHERE id = ?", (_ROW_ID,)
        )

    # ------------------------------------------------------------------
    # License lifecycle
    # ------------------------------------------------------------------
    def initialize_license(self) -> Dict[str, Any]:
        """Set up license data on first launch (and repair drift).

        Creates the license row when missing; otherwise updates the hwid
        when the hardware changed and backfills trial start / tamper hash.
        """
        started = time.perf_counter()
        hwid = self.generate_hwid()
        today = utc_now().strftime(_DATE_FMT)
        row = self._fetch_license_row()

        if row is None:
            self.db.db.execute(
                "INSERT INTO license_data (id, hwid, is_trial, trial_days,"
                " trial_start_date, days_remaining, tamper_hash)"
                " VALUES (?, ?, 1, ?, ?, ?, ?)",
                (_ROW_ID, hwid, TRIAL_DAYS, today, TRIAL_DAYS, None),
            )
            row = self._fetch_license_row()
            if row is not None:
                self._update_tamper_hash(row)
        else:
            if row.get("hwid") != hwid:
                # First real launch overwrites UNINITIALIZED; later changes
                # mean the PC hardware changed.
                self.db.db.execute(
                    "UPDATE license_data SET hwid = ? WHERE id = ?",
                    (hwid, _ROW_ID),
                )
                row["hwid"] = hwid
            if not row.get("is_activated") and not row.get("trial_start_date"):
                self.db.db.execute(
                    "UPDATE license_data SET trial_start_date = ? WHERE id = ?",
                    (today, _ROW_ID),
                )
                row["trial_start_date"] = today
            self._update_tamper_hash(row)

        status = self.check_license().get("data", {})
        return self.make_response(
            True, {"hwid": hwid, "status": status}, duration_ms=_ms(started)
        )

    def check_license(self) -> Dict[str, Any]:
        """Check whether the software is licensed and valid.

        Returns:
            Response with data: status ('active' | 'trial' | 'expired' |
            'invalid'), days_remaining, is_activated, is_trial, and
            clock_tampered. Success is True even for expired/invalid
            states (the CHECK succeeded; the license did not).
        """
        started = time.perf_counter()
        row = self._fetch_license_row()
        if row is None:
            return self.make_response(
                True,
                {
                    "status": "expired",
                    "days_remaining": 0,
                    "reason": "No license record found",
                },
                duration_ms=_ms(started),
            )

        clock_tampered = self.detect_clock_tampering()
        hwid = self.generate_hwid()
        today = utc_now().date()

        if row.get("is_activated") and row.get("license_key"):
            activated = self._verify_activated_row(row, hwid, today)
            activated["clock_tampered"] = clock_tampered
            return self.make_response(True, activated, duration_ms=_ms(started))

        if row.get("is_trial"):
            trial = self._verify_trial_row(row, today)
            trial["clock_tampered"] = clock_tampered
            return self.make_response(True, trial, duration_ms=_ms(started))

        return self.make_response(
            True,
            {
                "status": "expired",
                "days_remaining": 0,
                "is_activated": False,
                "is_trial": False,
                "clock_tampered": clock_tampered,
                "reason": "Neither activated nor in trial",
            },
            duration_ms=_ms(started),
        )

    def _verify_activated_row(
        self, row: Dict[str, Any], hwid: str, today: Any
    ) -> Dict[str, Any]:
        """Re-validate an activated license row against the live machine."""
        payload = self._decrypt_license_key(str(row.get("license_key") or ""))
        expected_hash = self.generate_tamper_hash(row)

        base: Dict[str, Any] = {
            "is_activated": True,
            "is_trial": False,
            "expiry_date": row.get("expiry_date"),
            "user_note": row.get("user_note"),
        }

        if expected_hash != row.get("tamper_hash"):
            self.log.warning("License DB tamper hash mismatch")
            return {
                **base,
                "status": "invalid",
                "days_remaining": 0,
                "reason": "License data was modified (tamper hash mismatch)",
            }
        if payload is None:
            return {
                **base,
                "status": "invalid",
                "days_remaining": 0,
                "reason": "Stored license key cannot be decrypted",
            }
        if str(payload.get("hwid", "")).upper() != hwid.upper():
            return {
                **base,
                "status": "invalid",
                "days_remaining": 0,
                "reason": "License key is bound to different hardware",
            }
        expiry = self._parse_date(str(payload.get("expiry", "")))
        days_remaining = (expiry - today).days if expiry else -1
        if expiry is None or days_remaining < 1:
            return {
                **base,
                "status": "expired",
                "days_remaining": 0,
                "reason": "License expiry date has passed",
            }
        if days_remaining != row.get("days_remaining"):
            self.db.db.execute(
                "UPDATE license_data SET days_remaining = ? WHERE id = ?",
                (days_remaining, _ROW_ID),
            )
        return {
            **base,
            "status": "active",
            "days_remaining": days_remaining,
        }

    def _verify_trial_row(self, row: Dict[str, Any], today: Any) -> Dict[str, Any]:
        """Compute trial countdown, updating days_remaining in the DB."""
        trial_days = int(row.get("trial_days") or TRIAL_DAYS)
        trial_start = self._parse_date(str(row.get("trial_start_date") or ""))
        if trial_start is None:
            # Uninitialized trial begins now (defensive; initialize_license
            # is the normal path that stamps the date).
            trial_start = today
            self.db.db.execute(
                "UPDATE license_data SET trial_start_date = ? WHERE id = ?",
                (today.strftime(_DATE_FMT), _ROW_ID),
            )
        days_used = (today - trial_start).days
        days_remaining = trial_days - days_used

        base: Dict[str, Any] = {
            "is_activated": False,
            "is_trial": True,
            "trial_days": trial_days,
        }
        if days_remaining > 0:
            if days_remaining != row.get("days_remaining"):
                self.db.db.execute(
                    "UPDATE license_data SET days_remaining = ? WHERE id = ?",
                    (days_remaining, _ROW_ID),
                )
            return {
                **base,
                "status": "trial",
                "days_remaining": days_remaining,
            }
        self.db.db.execute(
            "UPDATE license_data SET days_remaining = 0 WHERE id = ?", (_ROW_ID,)
        )
        return {
            **base,
            "status": "expired",
            "days_remaining": 0,
            "reason": "Trial period has ended",
        }

    def activate_license(self, license_key: str) -> Dict[str, Any]:
        """Validate and activate a license key generated for this PC.

        Args:
            license_key: Dash-segmented key from the admin key generator.

        Returns:
            Response with days_remaining on success, or an error naming
            the rejection reason (bad format, hwid mismatch, expired,
            wrong version).
        """
        started = time.perf_counter()
        if not license_key or not str(license_key).strip():
            return self.make_response(
                False, error="No license key provided", duration_ms=_ms(started)
            )

        payload = self._decrypt_license_key(license_key)
        if payload is None:
            return self.make_response(
                False,
                error="Invalid license key: cannot decrypt or parse key data",
                duration_ms=_ms(started),
            )

        if str(payload.get("version")) != KEY_VERSION:
            return self.make_response(
                False,
                error=(
                    f"Invalid license key: version"
                    f" {payload.get('version')!r} not supported"
                ),
                duration_ms=_ms(started),
            )

        hwid = self.generate_hwid()
        if str(payload.get("hwid", "")).upper() != hwid.upper():
            return self.make_response(
                False,
                error="Invalid license key: key is bound to different hardware",
                duration_ms=_ms(started),
            )

        expiry = self._parse_date(str(payload.get("expiry", "")))
        today = utc_now().date()
        days_remaining = (expiry - today).days if expiry else -1
        if expiry is None:
            return self.make_response(
                False,
                error="Invalid license key: malformed expiry date",
                duration_ms=_ms(started),
            )
        if days_remaining < 1:
            return self.make_response(
                False,
                error="Invalid license key: key has already expired",
                duration_ms=_ms(started),
            )

        now_str = utc_now_str()
        self.db.db.execute(
            "UPDATE license_data SET"
            " license_key = ?, is_activated = 1, is_trial = 0,"
            " activation_date = ?, expiry_date = ?, days_granted = ?,"
            " days_remaining = ?, user_note = ?,"
            " activation_count = COALESCE(activation_count, 0) + 1"
            " WHERE id = ?",
            (
                str(license_key),
                now_str,
                expiry.strftime(_DATE_FMT),
                int(payload.get("days") or 0),
                days_remaining,
                str(payload.get("note") or ""),
                _ROW_ID,
            ),
        )
        row = self._fetch_license_row()
        if row is not None:
            self._update_tamper_hash(row)

        self.log.info("License activated: %d days remaining", days_remaining)
        return self.make_response(
            True,
            {
                "status": "active",
                "days_remaining": days_remaining,
                "expiry_date": expiry.strftime(_DATE_FMT),
                "hwid": hwid,
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Tamper protection
    # ------------------------------------------------------------------
    def generate_tamper_hash(self, license_data: Dict[str, Any]) -> str:
        """Hash the integrity-critical license fields (spec formula)."""
        data_string = (
            f"{license_data.get('hwid')}"
            f"{license_data.get('expiry_date')}"
            f"{license_data.get('days_granted')}"
        )
        return hashlib.sha256(data_string.encode()).hexdigest()

    def _update_tamper_hash(self, row: Dict[str, Any]) -> None:
        """Recompute and persist the tamper hash for the given row."""
        self.db.db.execute(
            "UPDATE license_data SET tamper_hash = ? WHERE id = ?",
            (self.generate_tamper_hash(row), _ROW_ID),
        )

    def detect_clock_tampering(self) -> bool:
        """Detect the system clock being moved backward to bypass expiry.

        When tampering is detected the stored last_check_date is NOT
        updated (spec), so the evidence survives repeated checks.
        """
        today = utc_now()
        row = self._fetch_license_row()
        last_check_raw = (row or {}).get("last_check_date")
        if last_check_raw:
            last_check = parse_utc(str(last_check_raw))
            if last_check is not None and today < last_check - _CLOCK_TOLERANCE:
                self.log.warning("System clock moved backwards detected")
                return True
        self.db.db.execute(
            "UPDATE license_data SET last_check_date = ? WHERE id = ?",
            (today.strftime("%Y-%m-%d %H:%M:%S"), _ROW_ID),
        )
        return False

    def get_days_remaining(self) -> int:
        """Days remaining on the current license (0 when expired/invalid)."""
        return int(self.check_license().get("data", {}).get("days_remaining", 0))

    # ------------------------------------------------------------------
    # Key decryption / parsing helpers
    # ------------------------------------------------------------------
    def _decrypt_license_key(self, license_key: str) -> Optional[Dict[str, Any]]:
        """Decrypt a formatted license key into its JSON payload.

        Layers (inverse of the generator): strip separators -> restore
        '~' to '-' -> outer urlsafe b64 decode -> Fernet decrypt -> JSON.
        """
        fernet = _load_fernet()
        if fernet is None:
            self.log.error("cryptography package is required for license keys")
            return None
        try:
            token = _b64decode_unpadded(_normalize_license_key(license_key))
            decrypted = fernet.decrypt(token)
            payload = json.loads(decrypted.decode())
        except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
            # ValueError covers cryptography.fernet.InvalidToken.
            self.log.info("License key decryption failed: %s", exc)
            return None
        except Exception as exc:  # Fernet may raise non-ValueError errors
            self.log.info("License key decryption failed: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _parse_date(value: str) -> Optional[Any]:
        """Parse a YYYY-MM-DD date string, None on failure."""
        from datetime import datetime

        try:
            return datetime.strptime(value, _DATE_FMT).date()
        except (TypeError, ValueError):
            return None
