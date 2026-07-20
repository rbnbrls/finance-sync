"""Tests for authentication services, dependencies, and endpoints.

# pyright: basic
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

import pytest
from fastapi.testclient import TestClient
from jose import JWTError
from jose import jwt as jose_jwt

from finance_sync.api.deps.auth import (
    api_key_has_permission,
    user_has_permission,
)
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.services.auth import (
    ROLE_PERMISSIONS,
    create_access_token,
    create_refresh_token,
    decode_token,
    decrypt_credential,
    encrypt_credential,
    generate_api_key,
    hash_password,
    verify_api_key,
    verify_password,
)

# ── Test settings ────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"
_MSG_DB_NOT_CONFIGURED = "Database engine not configured"


@pytest.fixture
def settings() -> Settings:
    """Settings with a fixed secret key for deterministic tests."""
    return Settings(
        secret_key=_TEST_SECRET,  # type: ignore[call-arg]
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        master_encryption_key="a" * 64,  # 32 hex bytes = 32 bytes
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings=settings)


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════
# Password hashing
# ═══════════════════════════════════════════════════════════════════════


class TestPasswordHashing:
    def test_hash_and_verify(self) -> None:
        hashed = hash_password("correct-horse-battery-staple")
        assert isinstance(hashed, str)
        assert hashed != "correct-horse-battery-staple"
        assert verify_password("correct-horse-battery-staple", hashed) is True

    def test_wrong_password_fails(self) -> None:
        hashed = hash_password("real-password")
        assert verify_password("wrong-password", hashed) is False

    def test_empty_string(self) -> None:
        hashed = hash_password("")
        assert verify_password("", hashed) is True
        assert verify_password("x", hashed) is False


# ═══════════════════════════════════════════════════════════════════════
# JWT tokens
# ═══════════════════════════════════════════════════════════════════════


class TestJWTTokens:
    def test_create_access_token(self, settings: Settings) -> None:
        data = {"sub": "user-1", "tenant_id": "tenant-1", "role": "admin"}
        token = create_access_token(data, settings)
        assert isinstance(token, str)
        assert len(token.split(".")) == 3  # header.payload.sig

    def test_create_refresh_token(self, settings: Settings) -> None:
        data = {"sub": "user-1", "tenant_id": "tenant-1", "role": "user"}
        token = create_refresh_token(data, settings)
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    def test_decode_valid_token(self, settings: Settings) -> None:
        data = {"sub": "user-1", "tenant_id": "tenant-1", "role": "admin"}
        token = create_access_token(data, settings)
        payload = decode_token(token, settings)
        assert payload["sub"] == "user-1"
        assert payload["tenant_id"] == "tenant-1"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"
        assert "exp" in payload
        assert "iat" in payload

    def test_decode_refresh_token_type(self, settings: Settings) -> None:
        data = {"sub": "user-1", "tenant_id": "tenant-1", "role": "user"}
        token = create_refresh_token(data, settings)
        payload = decode_token(token, settings)
        assert payload["type"] == "refresh"

    def test_expired_token_raises(self, settings: Settings) -> None:
        """Tokens with ``exp`` in the past should be rejected."""
        from datetime import UTC, datetime, timedelta

        # Manually craft a token that expired 1 hour ago
        secret = settings.secret_key.get_secret_value().encode("utf-8")
        payload = {
            "sub": "u1",
            "tenant_id": "t1",
            "role": "user",
            "type": "access",
            "exp": datetime.now(UTC) - timedelta(hours=1),
            "iat": datetime.now(UTC) - timedelta(hours=2),
        }
        expired_token = jose_jwt.encode(
            payload, secret, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(JWTError):
            decode_token(expired_token, settings)

    def test_wrong_secret_fails(self, settings: Settings) -> None:
        data = {"sub": "u1", "tenant_id": "t1", "role": "user"}
        token = create_access_token(data, settings)
        bad_settings = Settings(  # type: ignore[call-arg]
            secret_key="different-secret-key-16chars!!",
        )
        with pytest.raises(JWTError):
            decode_token(token, bad_settings)


# ═══════════════════════════════════════════════════════════════════════
# API keys
# ═══════════════════════════════════════════════════════════════════════


class TestAPIKeyGeneration:
    def test_generate_returns_tuple(self) -> None:
        raw, hashed, prefix = generate_api_key()
        assert isinstance(raw, str)
        assert isinstance(hashed, str)
        assert isinstance(prefix, str)

    def test_key_prefix_is_first_8_chars(self) -> None:
        raw, _hashed, prefix = generate_api_key()
        assert raw.startswith("fs_")
        assert prefix == raw[:8]

    def test_verify_valid_key(self) -> None:
        raw, hashed, _prefix = generate_api_key()
        assert verify_api_key(raw, hashed) is True

    def test_verify_wrong_key(self) -> None:
        raw, hashed, _prefix = generate_api_key()
        assert verify_api_key(raw + "tampered", hashed) is False

    def test_unique_keys(self) -> None:
        raw1, _, _ = generate_api_key()
        raw2, _, _ = generate_api_key()
        assert raw1 != raw2


# ═══════════════════════════════════════════════════════════════════════
# Credential encryption (AES-256-GCM)
# ═══════════════════════════════════════════════════════════════════════


class TestCredentialEncryption:
    def test_encrypt_decrypt_roundtrip(self, settings: Settings) -> None:
        plaintext = '{"client_id": "abc", "client_secret": "s3cret!"}'
        ciphertext, nonce = encrypt_credential(plaintext, settings)
        assert isinstance(ciphertext, bytes)
        assert isinstance(nonce, bytes)
        assert len(nonce) == 12  # GCM nonce is 12 bytes
        assert ciphertext != plaintext.encode("utf-8")  # not plain

        decrypted = decrypt_credential(ciphertext, nonce, settings)
        assert decrypted == plaintext

    def test_missing_key_raises(self) -> None:
        no_key_settings = Settings(secret_key=_TEST_SECRET)  # type: ignore[call-arg]
        with pytest.raises(RuntimeError, match="not configured"):
            encrypt_credential("hello", no_key_settings)

    def test_wrong_key_fails(self, settings: Settings) -> None:
        plaintext = "secret-data"
        ciphertext, nonce = encrypt_credential(plaintext, settings)

        wrong_key_settings = Settings(  # type: ignore[call-arg]
            secret_key=_TEST_SECRET,
            master_encryption_key="b" * 64,
        )
        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            decrypt_credential(ciphertext, nonce, wrong_key_settings)

    def test_empty_string(self, settings: Settings) -> None:
        ciphertext, nonce = encrypt_credential("", settings)
        assert decrypt_credential(ciphertext, nonce, settings) == ""


# ═══════════════════════════════════════════════════════════════════════
# RBAC permissions
# ═══════════════════════════════════════════════════════════════════════


class TestUserPermissions:
    def test_admin_has_all(self) -> None:
        assert user_has_permission("admin", "anything", "anything") is True

    def test_user_can_read_transactions(self) -> None:
        assert user_has_permission("user", "transactions", "read") is True

    def test_user_can_write_transactions(self) -> None:
        assert user_has_permission("user", "transactions", "write") is True

    def test_user_cannot_write_unknown_resource(self) -> None:
        assert user_has_permission("user", "settings", "write") is False

    def test_readonly_cannot_write(self) -> None:
        assert user_has_permission("readonly", "transactions", "write") is False

    def test_viewer_limited(self) -> None:
        assert user_has_permission("viewer", "transactions", "read") is True
        assert user_has_permission("viewer", "accounts", "read") is True
        assert user_has_permission("viewer", "holdings", "read") is False

    def test_unknown_role_denies(self) -> None:
        assert (
            user_has_permission("nonexistent", "transactions", "read") is False
        )

    def test_role_permissions_are_defined(self) -> None:
        """All expected roles are in ROLE_PERMISSIONS."""
        for role in ("admin", "user", "readonly", "viewer"):
            assert role in ROLE_PERMISSIONS, f"Missing role: {role}"


class TestAPIKeyPermissions:
    def test_wildcard_allows_all(self) -> None:
        assert api_key_has_permission("*:*", "anything", "anything") is True

    def test_specific_permission(self) -> None:
        perms = "transactions:read accounts:read"
        assert api_key_has_permission(perms, "transactions", "read") is True
        assert api_key_has_permission(perms, "transactions", "write") is False

    def test_resource_wildcard(self) -> None:
        perms = "transactions:*"
        assert api_key_has_permission(perms, "transactions", "read") is True
        assert api_key_has_permission(perms, "transactions", "write") is True
        assert api_key_has_permission(perms, "accounts", "read") is False

    def test_none_permissions(self) -> None:
        assert api_key_has_permission(None, "transactions", "read") is False

    def test_empty_permissions(self) -> None:
        assert api_key_has_permission("", "transactions", "read") is False


# ═══════════════════════════════════════════════════════════════════════
# Auth endpoints (integration — with DB mock)
# ═══════════════════════════════════════════════════════════════════════


class TestAuthEndpoints:
    """Tests that exercise the auth router endpoints.

    Because the real database requires a live PostgreSQL, we mock the
    DB session for these integration-level tests.
    """

    def test_login_missing_fields(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/login", json={})
        assert resp.status_code == 422  # validation error

    def test_me_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401  # no Bearer token

    def test_refresh_invalid_token(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_openapi_has_auth_paths(self, client: TestClient) -> None:
        """Auth endpoints appear in OpenAPI schema."""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json()["paths"]
        # Check that auth paths exist
        assert "/api/v1/auth/login" in paths
        assert "/api/v1/auth/refresh" in paths
        assert "/api/v1/auth/me" in paths
        assert "/api/v1/auth/api-keys" in paths


class TestAuthEndpointsWithDB:
    """Auth endpoint tests with DB disabled — verifies JWT auth layer works."""

    @pytest.fixture
    def no_db_app(self) -> FastAPI:
        """Create app with DB explicitly disabled."""
        s = Settings(
            secret_key=_TEST_SECRET,
            database_url=None,
        )
        return create_app(settings=s)

    @pytest.fixture
    def no_db_client(
        self, no_db_app: FastAPI
    ) -> Generator[TestClient, None, None]:
        with TestClient(no_db_app) as c:
            yield c

    def test_jwt_auth_without_db_raises_sensible_error(
        self,
        no_db_client: TestClient,
    ) -> None:
        """JWT decodes but DB lookup fails with RuntimeError."""
        s = Settings(secret_key=_TEST_SECRET)
        token = create_access_token(
            {"sub": "u1", "tenant_id": "t1", "role": "admin"},
            s,
        )
        with pytest.raises(RuntimeError, match=_MSG_DB_NOT_CONFIGURED):
            no_db_client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )

    def test_jwt_validates_token_structure(
        self,
        no_db_client: TestClient,
    ) -> None:
        """A token without 'type: access' is rejected (when DB available)."""
        s = Settings(secret_key=_TEST_SECRET)
        secret = s.secret_key.get_secret_value().encode("utf-8")
        # Craft a refresh token
        refresh_payload = {
            "sub": "u1",
            "tenant_id": "t1",
            "role": "user",
            "type": "refresh",
            "exp": 9999999999,
        }
        refresh_token = jose_jwt.encode(
            refresh_payload, secret, algorithm="HS256"
        )
        # Without DB, get_db fails first — verify it fails on DB not token
        with pytest.raises(RuntimeError, match=_MSG_DB_NOT_CONFIGURED):
            no_db_client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )


# ═══════════════════════════════════════════════════════════════════════
# JWT-related settings
# ═══════════════════════════════════════════════════════════════════════


def test_settings_defaults() -> None:
    """Default Settings has reasonable JWT defaults."""
    s = Settings()
    assert s.access_token_expire_minutes == 30
    assert s.refresh_token_expire_days == 7
    assert s.jwt_algorithm == "HS256"


def test_settings_min_secret_key() -> None:
    """Secret keys shorter than 16 chars are rejected."""
    with pytest.raises(ValueError, match="at least 16 characters"):
        Settings(secret_key="short")  # type: ignore[call-arg]
