"""Identity models: users, API keys and refresh sessions (M5).

Creator OS had no notion of *who* was calling it before M5. Every request was
anonymous, so authorisation could not be enforced and the audit trail could not
be attributed. These three tables are the minimum needed to fix that:

``User``
    A principal with a role. Passwords are stored as PBKDF2-HMAC-SHA256 digests
    (see ``app.services.security.passwords``) — never in plain text, never
    reversible.
``ApiKey``
    A long-lived credential for automation and CI. Only a SHA-256 digest of the
    key is stored; the plaintext is shown exactly once at creation time.
``RefreshSession``
    Server-side record backing a refresh token so a session can actually be
    revoked. A stateless JWT alone cannot be logged out.

Roles reuse the existing ``ROLE_PERMISSIONS`` map in
``app.services.enterprise.auth`` rather than introducing a second, competing
permission model.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from app.domain.models.base import BaseModel

#: Default role granted to a self-registered user.
DEFAULT_ROLE = "viewer"


class User(BaseModel):
    """An authenticated principal."""

    __tablename__ = "users"

    username = Column(String(150), nullable=False, unique=True, index=True)
    email = Column(String(320), nullable=True, unique=True, index=True)
    #: ``pbkdf2_sha256$<iterations>$<salt>$<digest>``. Never the raw password.
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default=DEFAULT_ROLE, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_login_at = Column(DateTime, nullable=True)
    #: Consecutive failed logins; drives temporary lockout.
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)

    api_keys = relationship(
        "ApiKey",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sessions = relationship(
        "RefreshSession",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username!r} role={self.role!r}>"


class ApiKey(BaseModel):
    """A hashed, revocable machine credential."""

    __tablename__ = "api_keys"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String(150), nullable=False)
    #: SHA-256 hex digest of the full key. The plaintext is never persisted.
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    #: First characters of the key, shown in listings so a key is identifiable.
    prefix = Column(String(16), nullable=False, index=True)
    #: Optional narrowing: a key may hold *fewer* permissions than its owner's
    #: role, never more. Comma-separated; empty means "inherit the role".
    scopes = Column(String(500), nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="api_keys")

    __table_args__ = (Index("ix_api_keys_user_active", "user_id", "is_active"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApiKey {self.name!r} prefix={self.prefix!r}>"


class RefreshSession(BaseModel):
    """Server-side refresh-token record, so logout and revocation are real."""

    __tablename__ = "refresh_sessions"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: SHA-256 digest of the refresh token's JWT id.
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True, index=True)
    user_agent = Column(String(300), nullable=True)
    client_ip = Column(String(64), nullable=True)

    user = relationship("User", back_populates="sessions")

    __table_args__ = (Index("ix_refresh_sessions_user_revoked", "user_id", "revoked_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RefreshSession user_id={self.user_id} revoked={bool(self.revoked_at)}>"
