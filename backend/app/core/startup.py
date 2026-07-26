"""Startup configuration validation (M5).

Before M5 the backend would happily boot in production with authentication
off, Swagger exposed, wildcard CORS, rate limiting disabled and the shell
executor enabled. Nothing warned; nothing refused. That is the single easiest
way for a hardened codebase to end up deployed insecurely.

This module turns the deployment contract into an executable check:

* In **production** an unsafe setting is an ``error`` and the process refuses
  to start (unless ``ALLOW_INSECURE_PRODUCTION=true`` is set explicitly, which
  is itself logged loudly).
* In **development** the same finding is a ``warning``: it is printed once at
  startup and never blocks local work.

:func:`validate_settings` is pure — it takes a settings object and returns
findings — so it is straightforward to test every branch without spawning an
application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Sequence

from app.infrastructure.logging.logger import get_logger

logger = get_logger("startup")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: Origins that must never be trusted by a production deployment.
_LOCAL_ORIGIN_MARKERS = ("localhost", "127.0.0.1", "file://", "app://")


class StartupValidationError(RuntimeError):
    """Raised when production configuration is unsafe and must not start."""

    def __init__(self, findings: Sequence["Finding"]) -> None:
        self.findings = list(findings)
        detail = "\n".join(f"  - [{f.key}] {f.message}" for f in self.findings)
        super().__init__(
            "Refusing to start: unsafe production configuration.\n"
            f"{detail}\n"
            "Fix these settings, or set ALLOW_INSECURE_PRODUCTION=true to "
            "override (not recommended)."
        )


@dataclass(frozen=True)
class Finding:
    """One configuration problem."""

    key: str
    message: str
    severity: str
    remediation: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "message": self.message,
            "severity": self.severity,
            "remediation": self.remediation,
        }


def _sev(is_production: bool) -> str:
    return SEVERITY_ERROR if is_production else SEVERITY_WARNING


def validate_settings(settings: Any) -> List[Finding]:
    """Return every configuration finding for ``settings``.

    Production-only concerns are downgraded to warnings outside production, so
    the same function documents the deployment contract in both modes.
    """
    findings: List[Finding] = []
    production = bool(getattr(settings, "is_production", False))
    severity = _sev(production)

    # --- Authentication ---------------------------------------------------
    if not settings.AUTH_ENABLED:
        findings.append(
            Finding(
                "AUTH_ENABLED",
                "Authentication is disabled: every API caller is treated as a "
                "local admin.",
                severity,
                "Set AUTH_ENABLED=true and configure AUTH_SECRET_KEY.",
            )
        )
    else:
        secret = settings.AUTH_SECRET_KEY or ""
        if not secret:
            findings.append(
                Finding(
                    "AUTH_SECRET_KEY",
                    "AUTH_ENABLED is true but no signing secret is configured; "
                    "tokens cannot be issued.",
                    SEVERITY_ERROR,
                    "Set AUTH_SECRET_KEY to a random 32+ character value.",
                )
            )
        elif len(secret) < 32:
            findings.append(
                Finding(
                    "AUTH_SECRET_KEY",
                    f"Signing secret is only {len(secret)} characters; it is "
                    "brute-forceable.",
                    severity,
                    "Use at least 32 characters of high-entropy randomness.",
                )
            )
        elif secret.lower() in {"change-me", "changeme", "secret", "development"}:
            findings.append(
                Finding(
                    "AUTH_SECRET_KEY",
                    "Signing secret is a well-known placeholder value.",
                    SEVERITY_ERROR,
                    "Generate a unique secret per deployment.",
                )
            )

        if settings.AUTH_BOOTSTRAP_PASSWORD and production:
            findings.append(
                Finding(
                    "AUTH_BOOTSTRAP_PASSWORD",
                    "A bootstrap admin password is present in the environment.",
                    SEVERITY_WARNING,
                    "Clear it once the initial admin has been created.",
                )
            )

    # --- Transport / HTTP surface ----------------------------------------
    if production and settings.ENABLE_DOCS:
        findings.append(
            Finding(
                "ENABLE_DOCS",
                "Interactive API docs are exposed in production.",
                SEVERITY_WARNING,
                "Set ENABLE_DOCS=false unless the docs are intentionally public.",
            )
        )

    origins = list(settings.CORS_ORIGINS or [])
    if "*" in origins:
        findings.append(
            Finding(
                "CORS_ORIGINS",
                "CORS allows every origin while credentials are permitted.",
                severity,
                "List the exact browser origins that may call this API.",
            )
        )
    elif production:
        local = [
            origin
            for origin in origins
            if any(marker in origin for marker in _LOCAL_ORIGIN_MARKERS)
        ]
        if local:
            findings.append(
                Finding(
                    "CORS_ORIGINS",
                    f"Production CORS still trusts development origins: {local}.",
                    SEVERITY_WARNING,
                    "Remove localhost/file:// origins from the production list.",
                )
            )

    if production and list(settings.ALLOWED_HOSTS or []) == ["*"]:
        findings.append(
            Finding(
                "ALLOWED_HOSTS",
                "Any Host header is accepted, enabling host-header injection "
                "and DNS rebinding.",
                SEVERITY_WARNING,
                "Set ALLOWED_HOSTS to the hostnames this service is served on.",
            )
        )

    if not settings.RATE_LIMIT_ENABLED:
        findings.append(
            Finding(
                "RATE_LIMIT_ENABLED",
                "Rate limiting is disabled.",
                severity,
                "Set RATE_LIMIT_ENABLED=true.",
            )
        )

    if production and not settings.SECURITY_HSTS_ENABLED:
        findings.append(
            Finding(
                "SECURITY_HSTS_ENABLED",
                "HSTS is not enabled.",
                SEVERITY_WARNING,
                "Enable it when the API is served over TLS.",
            )
        )

    # --- Persistence -------------------------------------------------------
    if production and settings.is_sqlite:
        findings.append(
            Finding(
                "DATABASE_URL",
                "SQLite is in use in production: single-writer, no network "
                "access, and it cannot back more than one process.",
                SEVERITY_WARNING,
                "Use PostgreSQL for multi-process or multi-user deployments.",
            )
        )

    # M6-F6: connection-pool capacity is what caps request concurrency,
    # because every in-flight request holds a connection for its whole
    # lifetime. Measured at 100 concurrent clients: capacity 40 and 60 both
    # shed 40 requests as 503s; capacity 80 served all 500 cleanly.
    if not settings.is_sqlite:
        pool_capacity = int(getattr(settings, "DB_POOL_SIZE", 0)) + int(
            getattr(settings, "DB_MAX_OVERFLOW", 0)
        )
        if 0 < pool_capacity < 80:
            findings.append(
                Finding(
                    "DB_POOL_SIZE",
                    f"Database pool capacity is {pool_capacity} "
                    "(DB_POOL_SIZE + DB_MAX_OVERFLOW). Each in-flight request "
                    "holds one connection, so sustained concurrency above that "
                    "is shed as 503s once the pool timeout elapses.",
                    SEVERITY_WARNING,
                    "Raise DB_POOL_SIZE/DB_MAX_OVERFLOW to at least 80 "
                    "combined, and keep capacity x replicas below the "
                    "PostgreSQL max_connections.",
                )
            )

    if settings.DB_ECHO and production:
        findings.append(
            Finding(
                "DB_ECHO",
                "SQL echo is on in production: every statement is logged.",
                SEVERITY_WARNING,
                "Set DB_ECHO=false.",
            )
        )

    # --- Dangerous executors ---------------------------------------------
    if settings.ALLOW_SHELL_EXECUTOR:
        if not settings.SHELL_ALLOWED_COMMANDS:
            findings.append(
                Finding(
                    "ALLOW_SHELL_EXECUTOR",
                    "The shell executor is enabled with an empty allowlist, "
                    "which permits arbitrary local command execution.",
                    SEVERITY_ERROR,
                    "Set SHELL_ALLOWED_COMMANDS, or disable the executor.",
                )
            )
        else:
            findings.append(
                Finding(
                    "ALLOW_SHELL_EXECUTOR",
                    "The shell executor is enabled (allowlisted).",
                    SEVERITY_WARNING,
                    "Confirm every workflow author is trusted with local execution.",
                )
            )

    for flag, label in (
        ("ALLOW_PYTHON_EXECUTOR", "Python"),
        ("ALLOW_JAVASCRIPT_EXECUTOR", "JavaScript"),
    ):
        if getattr(settings, flag, False):
            if not settings.SCRIPT_SANDBOX_ENABLED:
                findings.append(
                    Finding(
                        flag,
                        f"The {label} node is enabled with the process sandbox "
                        "turned off; scripts run in-process with the backend's "
                        "own privileges.",
                        SEVERITY_ERROR if production else SEVERITY_WARNING,
                        "Set SCRIPT_SANDBOX_ENABLED=true.",
                    )
                )
            else:
                findings.append(
                    Finding(
                        flag,
                        f"The {label} node is enabled. The sandbox limits CPU, "
                        "memory and filesystem access but is not a security "
                        "boundary against a determined attacker "
                        "(see docs/SECURITY.md).",
                        SEVERITY_WARNING,
                        "Only enable this when workflow authors are trusted.",
                    )
                )

    if settings.ALLOW_DATABASE_EXECUTOR:
        findings.append(
            Finding(
                "ALLOW_DATABASE_EXECUTOR",
                "The database node can run SQL against the application "
                "database.",
                SEVERITY_WARNING,
                "Disable it unless workflows genuinely need raw SQL.",
            )
        )

    if settings.HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS:
        findings.append(
            Finding(
                "HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS",
                "HTTP nodes may reach private networks, enabling SSRF against "
                "internal services and cloud metadata endpoints.",
                severity,
                "Set HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS=false.",
            )
        )

    return findings


def enforce_startup_validation(settings: Any) -> List[Finding]:
    """Validate, log and (in production) refuse to start when unsafe.

    Returns the findings so ``/health/ready`` and the system endpoints can
    report them.
    """
    findings = validate_settings(settings)

    for finding in findings:
        message = "Config check [%s]: %s"
        if finding.severity == SEVERITY_ERROR:
            logger.error(message, finding.key, finding.message)
        else:
            logger.warning(message, finding.key, finding.message)
        if finding.remediation:
            logger.info("  remediation: %s", finding.remediation)

    errors = [f for f in findings if f.severity == SEVERITY_ERROR]
    if errors:
        override = os.getenv("ALLOW_INSECURE_PRODUCTION", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if override:
            logger.error(
                "ALLOW_INSECURE_PRODUCTION is set: starting with %s unsafe "
                "setting(s). This is not a supported configuration.",
                len(errors),
            )
        else:
            raise StartupValidationError(errors)

    if not findings:
        logger.info("Configuration validation passed with no findings.")
    return findings
