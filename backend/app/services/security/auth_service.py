"""Authentication service: registration, login, tokens and API keys (M5).

All credential-handling logic lives here so the routers stay thin and the rules
are testable without HTTP. Notable behaviours:

* **Uniform failure.** Unknown username and wrong password produce the same
  error and burn the same amount of CPU (a dummy hash is verified for unknown
  users), so the endpoint cannot be used to enumerate accounts.
* **Lockout.** Consecutive failures lock an account for a cooling-off period
  instead of allowing unlimited online guessing.
* **Refresh rotation.** Using a refresh token revokes it and issues a new one.
  Replaying a consumed token fails.
* **First-run bootstrap.** When auth is enabled and no users exist, an initial
  admin can be created from configuration so a fresh deployment is reachable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, ForbiddenError, UnauthorizedError, ValidationError
from app.domain.models.base import utcnow
from app.domain.models.identity import DEFAULT_ROLE, ApiKey, RefreshSession, User
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.services.enterprise.auth import ROLE_PERMISSIONS
from app.services.security.passwords import (
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.services.security.principal import (
    AUTH_METHOD_API_KEY,
    AUTH_METHOD_JWT,
    Principal,
)
from app.services.security.tokens import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    TokenError,
    decode_token,
    encode_token,
    hash_token_id,
)

logger = get_logger("security.auth")

#: Verified for unknown usernames so login timing does not leak account
#: existence. Any syntactically valid PBKDF2 hash works.
_DUMMY_HASH = (
    "pbkdf2_sha256$600000$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


class AuthService:
    """User lifecycle, credential verification and token management."""

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #
    def get_by_username(self, db: Session, username: str) -> Optional[User]:
        if not username:
            return None
        return (
            db.query(User)
            .filter(User.username == username.strip().lower())
            .one_or_none()
        )

    def get_by_id(self, db: Session, user_id: int) -> Optional[User]:
        return db.get(User, user_id)

    def user_count(self, db: Session) -> int:
        return db.query(User).count()

    def normalise_username(self, username: str) -> str:
        candidate = (username or "").strip().lower()
        if len(candidate) < 3:
            raise ValidationError("Username must be at least 3 characters.")
        if len(candidate) > 150:
            raise ValidationError("Username must be at most 150 characters.")
        if not all(char.isalnum() or char in "._-@" for char in candidate):
            raise ValidationError(
                "Username may only contain letters, digits and the characters . _ - @"
            )
        return candidate

    def validate_role(self, role: str) -> str:
        candidate = (role or DEFAULT_ROLE).strip().lower()
        if candidate not in ROLE_PERMISSIONS:
            raise ValidationError(
                f"Unknown role {role!r}.",
                details={"valid_roles": sorted(ROLE_PERMISSIONS)},
            )
        return candidate

    def create_user(
        self,
        db: Session,
        *,
        username: str,
        password: str,
        role: str = DEFAULT_ROLE,
        email: Optional[str] = None,
        is_active: bool = True,
    ) -> User:
        """Create a user. Raises ``ConflictError`` when the name is taken."""
        normalised = self.normalise_username(username)
        validated_role = self.validate_role(role)
        validate_password_strength(password)

        if self.get_by_username(db, normalised) is not None:
            raise ConflictError(f"Username {normalised!r} is already registered.")

        user = User(
            username=normalised,
            email=(email or "").strip().lower() or None,
            password_hash=hash_password(password),
            role=validated_role,
            is_active=is_active,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ConflictError("That username or email is already registered.") from exc
        db.refresh(user)
        logger.info(
            "Created user %s with role %s", user.username, user.role,
            extra={"user_id": user.id, "role": user.role},
        )
        return user

    def set_password(self, db: Session, user: User, new_password: str) -> User:
        validate_password_strength(new_password)
        user.password_hash = hash_password(new_password)
        user.failed_login_count = 0
        user.locked_until = None
        db.add(user)
        db.commit()
        db.refresh(user)
        # Any stolen session must not survive a password change.
        self.revoke_all_sessions(db, user.id)
        return user

    def set_role(self, db: Session, user: User, role: str) -> User:
        user.role = self.validate_role(role)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def set_active(self, db: Session, user: User, is_active: bool) -> User:
        user.is_active = bool(is_active)
        db.add(user)
        db.commit()
        db.refresh(user)
        if not is_active:
            self.revoke_all_sessions(db, user.id)
        return user

    def list_users(self, db: Session, skip: int = 0, limit: int = 50) -> List[User]:
        return (
            db.query(User)
            .order_by(User.id.asc())
            .offset(max(0, skip))
            .limit(max(1, min(limit, 200)))
            .all()
        )

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #
    def _is_locked(self, user: User) -> bool:
        if user.locked_until is None:
            return False
        return user.locked_until > utcnow()

    def _register_failure(self, db: Session, user: User) -> None:
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.AUTH_MAX_FAILED_LOGINS:
            user.locked_until = utcnow() + timedelta(
                seconds=settings.AUTH_LOCKOUT_SECONDS
            )
            logger.warning(
                "Locked account %s after %s failed logins",
                user.username,
                user.failed_login_count,
                extra={"user_id": user.id},
            )
        db.add(user)
        db.commit()

    def authenticate(self, db: Session, username: str, password: str) -> User:
        """Verify credentials and return the user, or raise ``UnauthorizedError``."""
        user = self.get_by_username(db, (username or "").strip().lower())

        if user is None:
            # Spend comparable CPU so timing does not reveal that the user is
            # unknown, then fail with the same message as a bad password.
            verify_password(password or "", _DUMMY_HASH)
            raise UnauthorizedError("Invalid username or password.")

        if self._is_locked(user):
            raise UnauthorizedError(
                "Account is temporarily locked after repeated failed logins.",
                details={"locked_until": user.locked_until.isoformat()},
            )

        if not verify_password(password or "", user.password_hash):
            self._register_failure(db, user)
            raise UnauthorizedError("Invalid username or password.")

        if not user.is_active:
            raise ForbiddenError("This account is disabled.")

        # Successful login: clear counters and opportunistically upgrade the
        # hash if the iteration count has since been raised.
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = utcnow()
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    # ------------------------------------------------------------------ #
    # Tokens
    # ------------------------------------------------------------------ #
    def _secret(self) -> str:
        secret = settings.AUTH_SECRET_KEY
        if not secret:
            raise ForbiddenError(
                "Authentication is enabled but AUTH_SECRET_KEY is not configured."
            )
        return secret

    def issue_access_token(self, user: User) -> str:
        return encode_token(
            {"sub": str(user.id), "username": user.username, "role": user.role},
            self._secret(),
            expires_in=settings.AUTH_ACCESS_TOKEN_TTL_SECONDS,
            token_type=TOKEN_TYPE_ACCESS,
            issuer=settings.AUTH_TOKEN_ISSUER,
            audience=settings.AUTH_TOKEN_AUDIENCE,
        )

    def issue_refresh_token(
        self,
        db: Session,
        user: User,
        *,
        user_agent: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> str:
        """Issue a refresh token and record the session so it can be revoked."""
        token = encode_token(
            {"sub": str(user.id), "username": user.username, "role": user.role},
            self._secret(),
            expires_in=settings.AUTH_REFRESH_TOKEN_TTL_SECONDS,
            token_type=TOKEN_TYPE_REFRESH,
            issuer=settings.AUTH_TOKEN_ISSUER,
            audience=settings.AUTH_TOKEN_AUDIENCE,
        )
        claims = decode_token(token, self._secret(), expected_type=TOKEN_TYPE_REFRESH)
        session = RefreshSession(
            user_id=user.id,
            token_hash=hash_token_id(claims["jti"]),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc).replace(
                tzinfo=None
            ),
            user_agent=(user_agent or "")[:300] or None,
            client_ip=(client_ip or "")[:64] or None,
        )
        db.add(session)
        db.commit()
        return token

    def issue_token_pair(
        self,
        db: Session,
        user: User,
        *,
        user_agent: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "access_token": self.issue_access_token(user),
            "refresh_token": self.issue_refresh_token(
                db, user, user_agent=user_agent, client_ip=client_ip
            ),
            "token_type": "bearer",
            "expires_in": int(settings.AUTH_ACCESS_TOKEN_TTL_SECONDS),
            "user": self.serialize_user(user),
        }

    def rotate_refresh_token(
        self,
        db: Session,
        refresh_token: str,
        *,
        user_agent: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Consume a refresh token and issue a fresh pair.

        The presented token is revoked before the new one is issued, so a
        replayed token is rejected.
        """
        try:
            claims = decode_token(
                refresh_token,
                self._secret(),
                expected_type=TOKEN_TYPE_REFRESH,
                issuer=settings.AUTH_TOKEN_ISSUER,
                audience=settings.AUTH_TOKEN_AUDIENCE,
            )
        except TokenError as exc:
            raise UnauthorizedError(f"Invalid refresh token: {exc}") from exc

        token_hash = hash_token_id(claims["jti"])
        session = (
            db.query(RefreshSession)
            .filter(RefreshSession.token_hash == token_hash)
            .one_or_none()
        )
        if session is None:
            raise UnauthorizedError("Refresh session is not recognised.")
        if session.expires_at <= utcnow():
            raise UnauthorizedError("Refresh session has expired.")

        # Claim the session with a single conditional UPDATE.
        #
        # Reading `revoked_at`, deciding, then writing is a check-then-act race:
        # concurrent requests presenting the same token all observe NULL and all
        # proceed, so one stolen token yields several valid sessions. A probe
        # with 8 concurrent rotations produced 3 successes before this change.
        #
        # `UPDATE ... WHERE revoked_at IS NULL` is atomic in both SQLite and
        # PostgreSQL, so exactly one caller sees rowcount 1 and wins.
        now = utcnow()
        claimed = (
            db.query(RefreshSession)
            .filter(
                RefreshSession.token_hash == token_hash,
                RefreshSession.revoked_at.is_(None),
            )
            .update({RefreshSession.revoked_at: now}, synchronize_session=False)
        )
        db.commit()

        if not claimed:
            # Someone else already consumed this token. Either it was replayed
            # (theft) or two legitimate clients raced; both warrant dropping
            # every session for the user rather than guessing.
            logger.warning(
                "Replayed refresh token for user %s; revoking all sessions",
                session.user_id,
                extra={"user_id": session.user_id},
            )
            self.revoke_all_sessions(db, session.user_id)
            raise UnauthorizedError("Refresh token has already been used.")

        user = self.get_by_id(db, session.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is no longer active.")

        return self.issue_token_pair(
            db, user, user_agent=user_agent, client_ip=client_ip
        )

    def revoke_refresh_token(self, db: Session, refresh_token: str) -> bool:
        """Revoke one session. Returns True when a live session was revoked."""
        try:
            claims = decode_token(
                refresh_token,
                self._secret(),
                expected_type=TOKEN_TYPE_REFRESH,
                verify_exp=False,
            )
        except TokenError:
            return False
        session = (
            db.query(RefreshSession)
            .filter(RefreshSession.token_hash == hash_token_id(claims["jti"]))
            .one_or_none()
        )
        if session is None or session.revoked_at is not None:
            return False
        session.revoked_at = utcnow()
        db.add(session)
        db.commit()
        return True

    def revoke_all_sessions(self, db: Session, user_id: int) -> int:
        """Revoke every live session for a user. Returns the count revoked."""
        now = utcnow()
        revoked = (
            db.query(RefreshSession)
            .filter(
                RefreshSession.user_id == user_id,
                RefreshSession.revoked_at.is_(None),
            )
            .update({RefreshSession.revoked_at: now}, synchronize_session=False)
        )
        db.commit()
        return int(revoked or 0)

    def purge_expired_sessions(self, db: Session) -> int:
        """Delete sessions that expired more than a day ago."""
        cutoff = utcnow() - timedelta(days=1)
        deleted = (
            db.query(RefreshSession)
            .filter(RefreshSession.expires_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted or 0)

    def principal_from_access_token(self, db: Session, token: str) -> Principal:
        """Resolve a bearer token to a :class:`Principal`."""
        try:
            claims = decode_token(
                token,
                self._secret(),
                expected_type=TOKEN_TYPE_ACCESS,
                issuer=settings.AUTH_TOKEN_ISSUER,
                audience=settings.AUTH_TOKEN_AUDIENCE,
            )
        except TokenError as exc:
            raise UnauthorizedError(str(exc)) from exc

        try:
            user_id = int(claims.get("sub", ""))
        except (TypeError, ValueError) as exc:
            raise UnauthorizedError("Token subject is not a user id.") from exc

        user = self.get_by_id(db, user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is no longer active.")

        # The role is read from the database, not the token, so a demotion
        # takes effect immediately instead of at token expiry.
        return Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            auth_method=AUTH_METHOD_JWT,
            session_id=claims.get("jti"),
        )

    # ------------------------------------------------------------------ #
    # API keys
    # ------------------------------------------------------------------ #
    def create_api_key(
        self,
        db: Session,
        user: User,
        *,
        name: str,
        scopes: Optional[List[str]] = None,
        expires_in_days: Optional[int] = None,
    ) -> Tuple[ApiKey, str]:
        """Create an API key. Returns ``(record, plaintext)``.

        The plaintext is returned once and never stored.
        """
        label = (name or "").strip()
        if not label:
            raise ValidationError("An API key name is required.")
        if len(label) > 150:
            raise ValidationError("API key name must be at most 150 characters.")

        cleaned_scopes: List[str] = []
        for scope in scopes or []:
            candidate = str(scope).strip().lower()
            if not candidate:
                continue
            if candidate not in self.all_permissions():
                raise ValidationError(
                    f"Unknown scope {scope!r}.",
                    details={"valid_scopes": sorted(self.all_permissions())},
                )
            cleaned_scopes.append(candidate)

        expires_at = None
        if expires_in_days is not None:
            if expires_in_days <= 0 or expires_in_days > 3650:
                raise ValidationError("expires_in_days must be between 1 and 3650.")
            expires_at = utcnow() + timedelta(days=int(expires_in_days))

        plaintext = generate_api_key()
        record = ApiKey(
            user_id=user.id,
            name=label,
            key_hash=hash_api_key(plaintext),
            prefix=api_key_prefix(plaintext),
            scopes=",".join(sorted(set(cleaned_scopes))),
            expires_at=expires_at,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info(
            "Issued API key %r for user %s", label, user.username,
            extra={"user_id": user.id, "api_key_id": record.id},
        )
        return record, plaintext

    def list_api_keys(self, db: Session, user_id: int) -> List[ApiKey]:
        return (
            db.query(ApiKey)
            .filter(ApiKey.user_id == user_id)
            .order_by(ApiKey.id.desc())
            .all()
        )

    def revoke_api_key(self, db: Session, user_id: int, key_id: int) -> bool:
        record = (
            db.query(ApiKey)
            .filter(ApiKey.id == key_id, ApiKey.user_id == user_id)
            .one_or_none()
        )
        if record is None or not record.is_active:
            return False
        record.is_active = False
        db.add(record)
        db.commit()
        return True

    def principal_from_api_key(self, db: Session, key: str) -> Principal:
        """Resolve a raw API key to a :class:`Principal`."""
        if not key:
            raise UnauthorizedError("An API key is required.")
        record = (
            db.query(ApiKey)
            .filter(ApiKey.key_hash == hash_api_key(key))
            .one_or_none()
        )
        if record is None or not record.is_active:
            raise UnauthorizedError("API key is not recognised.")
        if record.expires_at is not None and record.expires_at <= utcnow():
            raise UnauthorizedError("API key has expired.")

        user = self.get_by_id(db, record.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is no longer active.")

        # Best-effort usage stamp; never fail a request because of it.
        try:
            record.last_used_at = utcnow()
            db.add(record)
            db.commit()
        except Exception:  # pragma: no cover - defensive
            db.rollback()

        scopes = frozenset(s for s in (record.scopes or "").split(",") if s)
        return Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            auth_method=AUTH_METHOD_API_KEY,
            scopes=scopes,
            api_key_id=record.id,
        )

    # ------------------------------------------------------------------ #
    # Bootstrap and serialisation
    # ------------------------------------------------------------------ #
    def all_permissions(self) -> set:
        return {perm for perms in ROLE_PERMISSIONS.values() for perm in perms}

    def bootstrap_admin(self, db: Session) -> Optional[User]:
        """Create the configured initial admin when no users exist.

        Returns the created user, or ``None`` when bootstrap is not configured
        or the instance already has users.
        """
        if not settings.AUTH_BOOTSTRAP_USERNAME or not settings.AUTH_BOOTSTRAP_PASSWORD:
            return None
        if self.user_count(db) > 0:
            return None
        user = self.create_user(
            db,
            username=settings.AUTH_BOOTSTRAP_USERNAME,
            password=settings.AUTH_BOOTSTRAP_PASSWORD,
            role="admin",
        )
        logger.warning(
            "Bootstrapped initial admin %r from configuration. "
            "Change this password and clear AUTH_BOOTSTRAP_PASSWORD.",
            user.username,
        )
        return user

    def serialize_user(self, user: User) -> Dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "permissions": sorted(ROLE_PERMISSIONS.get(user.role, [])),
            "last_login_at": user.last_login_at.isoformat()
            if user.last_login_at
            else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }

    def serialize_api_key(self, record: ApiKey) -> Dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "prefix": record.prefix,
            "scopes": [s for s in (record.scopes or "").split(",") if s],
            "is_active": record.is_active,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "last_used_at": record.last_used_at.isoformat()
            if record.last_used_at
            else None,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }


auth_service = AuthService()
