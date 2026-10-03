"""
services/encryption.py
-----------------------
Fernet symmetric encryption for storing OAuth tokens securely.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from core.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(settings.fernet_key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plain string, returning a base64-encoded ciphertext string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """
    Decrypt a Fernet ciphertext string.
    Raises InvalidToken if the key is wrong or data is tampered.
    """
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def safe_decrypt(ciphertext: str | None) -> str | None:
    """Return decrypted value or None on failure (for optional fields)."""
    if not ciphertext:
        return None
    try:
        return decrypt(ciphertext)
    except (InvalidToken, Exception):
        return None
