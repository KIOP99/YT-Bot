"""
core/security.py
----------------
Password hashing (Argon2 with bcrypt fallback), JWT creation/verification,
TOTP support, and CSRF token utilities.
"""

from __future__ import annotations

import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from core.config import settings

# ── Argon2 hasher (memory-hard, recommended over bcrypt) ─────────────────────
_hasher = PasswordHasher(
    time_cost=2,       # iterations
    memory_cost=65536, # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain: str) -> str:
    """Hash a password with Argon2id. Returns the full encoded hash string."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Verify a plain-text password against an Argon2 hash.
    Returns False on any mismatch; does NOT raise.
    """
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True if the hash was created with old parameters and should be updated."""
    return _hasher.check_needs_rehash(hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(
    user_id: int,
    username: str,
    extra: Optional[dict] = None,
) -> str:
    """Create a signed JWT access token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(minutes=settings.session_expire_minutes),
        **(extra or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """
    Decode and verify a JWT token.
    Raises jwt.exceptions.* on failure (caller should handle).
    """
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )


# ── CSRF ─────────────────────────────────────────────────────────────────────

def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_urlsafe(32)


def verify_csrf_token(token: str, expected: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(token, expected)


# ── TOTP (optional 2FA) ───────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    """Generate a new random TOTP secret (base32)."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, username: str) -> str:
    """Return an otpauth:// URI for QR code generation."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name=settings.totp_issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code with a ±1-step window."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


# ── Credential generation ────────────────────────────────────────────────────

def generate_secure_username(length: int = 10) -> str:
    """Generate a random alphanumeric username."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "ytbot_" + "".join(secrets.choice(alphabet) for _ in range(length))


def generate_secure_password(length: int = 4) -> str:
    """
    Generate a 2 to 4 character/digit passcode (e.g. '8492' or 'BR26').
    Enforces min 2 chars, max 4 chars.
    """
    length = max(2, min(4, length))
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
