"""Admin license key generator (standalone tool, NOT the app runtime).

Spec source: modules_specification.txt MODULE 13 / CLASS KeyGenerator.

Encrypts the key payload {hwid, expiry, days, note, version, created}
with Fernet (PBKDF2HMAC-SHA256, salt=ENCRYPTION_SALT, 100k iterations,
password=ADMIN_PASSWORD), applies the spec's outer urlsafe base64 layer,
formats the result in dash-separated groups of 4, and appends the key to
key_history.csv.

Dash-safety note: the outer urlsafe base64 layer may itself contain '-'
characters, which would collide with the dash group separators. Payload
'-' is therefore translated to '~' before segmenting;
core/license_manager.py:_normalize_license_key reverses the translation
before decoding. The derivation below must stay BYTE-IDENTICAL to
core/license_manager.py:_derive_fernet_key (RULE 1: this tool is
standalone; the logic is duplicated on purpose and covered by round-trip
tests in tests/unit/test_license_manager.py).
"""

from __future__ import annotations

import base64
import csv
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Union

from core.time_helper import utc_now

MODULE_NAME = "key_generator"

ADMIN_PASSWORD = "IAMKING"
ADMIN_HINT = "IKNG"
KEY_VERSION = "1"
ENCRYPTION_SALT = b"AutoDoku2025Salt"

_PBKDF2_ITERATIONS = 100_000
_DATE_FMT = "%Y-%m-%d"
_GROUP_SIZE = 4
_GROUP_SEPARATOR = "-"
_SANITIZED_DASH = "~"

HISTORY_HEADER = ["date", "hwid", "days", "user_note", "key", "expiry_date"]


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _derive_fernet_key() -> bytes:
    """Derive the shared Fernet key (PBKDF2HMAC-SHA256, spec constants).

    Keep BYTE-IDENTICAL to core/license_manager.py:_derive_fernet_key.
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


def _response(
    success: bool,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    started: Optional[float] = None,
) -> Dict[str, Any]:
    """Standard-shaped response (mirrors BaseModule.make_response)."""
    return {
        "success": success,
        "data": data if data is not None else {},
        "error": error,
        "warnings": [],
        "module": MODULE_NAME,
        "duration_ms": _ms(started) if started is not None else 0.0,
    }


class KeyGenerator:
    """Generate license keys for given HWIDs (admin use only)."""

    def __init__(self, history_path: Optional[Union[str, Path]] = None) -> None:
        """Initialize generator.

        Args:
            history_path: CSV history file location. Defaults to
                key_history.csv next to this tool.
        """
        self.history_path = (
            Path(history_path)
            if history_path is not None
            else Path(__file__).resolve().parent / "key_history.csv"
        )

    def verify_admin_password(self, entered_password: str) -> bool:
        """Check whether the entered password is correct."""
        return entered_password == ADMIN_PASSWORD

    def generate_key(
        self, hwid: str, days: int, user_note: str, admin_password: str
    ) -> Dict[str, Any]:
        """Generate a license key for the given HWID and duration.

        Args:
            hwid: Target machine HWID (e.g. A3F7-9B2C-...).
            days: License duration in days from now.
            user_note: Free-form admin note (e.g. customer name).
            admin_password: Must equal ADMIN_PASSWORD.

        Returns:
            Response with data.key (formatted), expiry, hwid, days; or an
            error when the password is wrong / inputs are invalid.
        """
        started = time.perf_counter()
        if admin_password != ADMIN_PASSWORD:
            return _response(False, error="Wrong admin password", started=started)
        try:
            days = int(days)
        except (TypeError, ValueError):
            return _response(
                False, error=f"Invalid days value: {days!r}", started=started
            )
        if days < 0:
            return _response(
                False, error="Invalid days value: must be >= 0", started=started
            )
        if not hwid or not str(hwid).strip():
            return _response(False, error="Invalid hwid: empty", started=started)

        expiry_date = utc_now() + timedelta(days=days)
        expiry_string = expiry_date.strftime(_DATE_FMT)
        created_string = utc_now().strftime(_DATE_FMT)

        key_data = {
            "hwid": str(hwid),
            "expiry": expiry_string,
            "days": days,
            "note": str(user_note or ""),
            "version": KEY_VERSION,
            "created": created_string,
        }
        key_json = json.dumps(key_data)

        from cryptography.fernet import Fernet

        fernet = Fernet(_derive_fernet_key())
        encrypted = fernet.encrypt(key_json.encode())
        payload = base64.urlsafe_b64encode(encrypted).decode()

        # Dash-safety: payload '-' would collide with group separators.
        payload = payload.replace("-", _SANITIZED_DASH)
        license_key = _GROUP_SEPARATOR.join(
            payload[i : i + _GROUP_SIZE] for i in range(0, len(payload), _GROUP_SIZE)
        )

        self.save_to_history(hwid, days, user_note, license_key)

        return _response(
            True,
            {
                "key": license_key,
                "hwid": str(hwid),
                "days": days,
                "expiry": expiry_string,
                "note": str(user_note or ""),
                "version": KEY_VERSION,
            },
            started=started,
        )

    def save_to_history(self, hwid: str, days: int, user_note: str, key: str) -> None:
        """Append the generated key to the history CSV file."""
        expiry_string = (utc_now() + timedelta(days=int(days))).strftime(_DATE_FMT)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.history_path.exists()
        with self.history_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(HISTORY_HEADER)
            writer.writerow(
                [
                    utc_now().strftime(_DATE_FMT),
                    hwid,
                    days,
                    user_note,
                    key,
                    expiry_string,
                ]
            )


def main() -> int:
    """Interactive CLI for the admin key generator."""
    import argparse

    parser = argparse.ArgumentParser(description="Autopilot license key tool")
    parser.add_argument("--hwid", required=True, help="Target machine HWID")
    parser.add_argument("--days", type=int, required=True, help="License days")
    parser.add_argument("--note", default="", help="Customer/admin note")
    parser.add_argument("--password", required=True, help="Admin password")
    parser.add_argument("--history", default=None, help="History CSV path")
    args = parser.parse_args()

    generator = KeyGenerator(history_path=args.history)
    result = generator.generate_key(args.hwid, args.days, args.note, args.password)
    if not result["success"]:
        print(f"ERROR: {result['error']}")
        return 1
    print(f"License key ({result['days']} days, expires {result['expiry']}):")
    print(result["data"]["key"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
