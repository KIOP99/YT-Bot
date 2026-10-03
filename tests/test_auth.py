"""
tests/test_auth.py
------------------
Tests for password hashing and JWT operations.
"""

import pytest
import time

from core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
    generate_secure_password,
    generate_secure_username,
    generate_csrf_token,
    verify_csrf_token,
)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        plain = "SuperSecretPassword123!"
        hashed = hash_password(plain)
        assert hashed != plain
        assert verify_password(plain, hashed)

    def test_wrong_password(self):
        hashed = hash_password("correct-password")
        assert not verify_password("wrong-password", hashed)

    def test_different_hashes_for_same_password(self):
        """Argon2 uses random salt so each hash should be unique."""
        p = "password"
        h1 = hash_password(p)
        h2 = hash_password(p)
        assert h1 != h2
        # But both should verify
        assert verify_password(p, h1)
        assert verify_password(p, h2)


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token(user_id=1, username="admin")
        payload = decode_access_token(token)
        assert payload["sub"] == "1"
        assert payload["username"] == "admin"

    def test_extra_claims(self):
        token = create_access_token(1, "admin", extra={"role": "superuser"})
        payload = decode_access_token(token)
        assert payload["role"] == "superuser"

    def test_tampered_token(self):
        import jwt
        token = create_access_token(1, "admin")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(jwt.exceptions.InvalidTokenError):
            decode_access_token(tampered)


class TestCredentialGeneration:
    def test_secure_password_length(self):
        pwd = generate_secure_password(4)
        assert 2 <= len(pwd) <= 4

    def test_secure_username_prefix(self):
        uname = generate_secure_username()
        assert uname.startswith("ytbot_")

    def test_uniqueness(self):
        passwords = {generate_secure_password() for _ in range(10)}
        assert len(passwords) == 10


class TestCSRF:
    def test_valid_tokens_match(self):
        token = generate_csrf_token()
        assert verify_csrf_token(token, token)

    def test_different_tokens_dont_match(self):
        t1 = generate_csrf_token()
        t2 = generate_csrf_token()
        assert not verify_csrf_token(t1, t2)
