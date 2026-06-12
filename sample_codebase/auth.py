"""Authentication module for the sample storefront."""

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

_USERS: dict[str, dict] = {}
_TOKENS: dict[str, dict] = {}

TOKEN_TTL = timedelta(hours=12)


def create_user(email: str, password: str, role: str = "member") -> dict:
    """Register a new user with a salted password hash.

    Args:
        email: Unique email address for the account.
        password: Plaintext password; must be at least 8 characters. Only the
            salted SHA-256 hash is stored.
        role: Access role, defaults to "member".

    Returns:
        The user record with keys: user_id, email, role, created_at. The
        password hash is never included.

    Raises:
        ValueError: If the email is already registered or the password is
            shorter than 8 characters.
    """
    if email in _USERS:
        raise ValueError(f"email already registered: {email}")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")

    salt = os.urandom(16)
    digest = hashlib.sha256(salt + password.encode()).hexdigest()
    user = {
        "user_id": str(uuid4()),
        "email": email,
        "role": role,
        "salt": salt.hex(),
        "password_hash": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _USERS[email] = user
    return {k: user[k] for k in ("user_id", "email", "role", "created_at")}


def authenticate(email: str, password: str) -> str:
    """Verify credentials and issue a session token.

    Args:
        email: The account's email address.
        password: The plaintext password to check.

    Returns:
        An opaque session token string, valid for 12 hours.

    Raises:
        PermissionError: If the email is unknown or the password does not
            match. The same error is raised for both cases so callers cannot
            probe for registered emails.
    """
    user = _USERS.get(email)
    if user is None:
        raise PermissionError("invalid credentials")

    salt = bytes.fromhex(user["salt"])
    digest = hashlib.sha256(salt + password.encode()).hexdigest()
    if not hmac.compare_digest(digest, user["password_hash"]):
        raise PermissionError("invalid credentials")

    token = str(uuid4())
    _TOKENS[token] = {
        "user_id": user["user_id"],
        "expires_at": datetime.now(timezone.utc) + TOKEN_TTL,
    }
    return token


def revoke_token(token: str) -> bool:
    """Invalidate a session token immediately.

    Args:
        token: The session token to revoke.

    Returns:
        True if the token existed and was revoked, False if it was unknown
        or already revoked. Revoking is idempotent and never raises.
    """
    return _TOKENS.pop(token, None) is not None
