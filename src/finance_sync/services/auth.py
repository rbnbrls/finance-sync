"""Cryptographic and authentication services for finance-sync."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import bcrypt
from jose import jwt

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings

# ── Password hashing ──────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode(
        "ascii"
    )


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))


# ── JWT tokens (HS256) ────────────────────────────────────────────────

_TOKEN_EXCLUDE: set[str] = {"exp", "iat", "nbf", "jti"}


def _secret_bytes(settings: Settings) -> bytes:
    return settings.secret_key.get_secret_value().encode("utf-8")


def create_access_token(
    data: dict[str, Any],
    settings: Settings,
) -> str:
    """Create a short-lived JWT access token.

    *data* should contain at least ``sub`` (user id), ``tenant_id``,
    and ``role``.  A new ``exp`` claim is added based on
    ``settings.access_token_expire_minutes``.
    """
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    to_encode.update(
        {"exp": expire, "type": "access", "iat": datetime.now(UTC)}
    )
    return jwt.encode(
        to_encode, _secret_bytes(settings), algorithm=settings.jwt_algorithm
    )


def create_refresh_token(
    data: dict[str, Any],
    settings: Settings,
) -> str:
    """Create a longer-lived JWT refresh token."""
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(
        days=settings.refresh_token_expire_days
    )
    to_encode.update(
        {"exp": expire, "type": "refresh", "iat": datetime.now(UTC)}
    )
    return jwt.encode(
        to_encode, _secret_bytes(settings), algorithm=settings.jwt_algorithm
    )


def decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """Decode and validate a JWT token.

    Returns the token payload, or raises a ``JWTError`` on expiry /
    invalid signature.
    """
    return jwt.decode(
        token,
        _secret_bytes(settings),
        algorithms=[settings.jwt_algorithm],
    )


# ── API key helpers ───────────────────────────────────────────────────


def generate_api_key() -> tuple[str, str, str]:
    """Generate a cryptographically random API key.

    Returns ``(raw_key, bcrypt_hash, key_prefix)``.

    The raw key is shown exactly once; only the hash is persisted.
    *prefix* is the first 8 characters — useful for log correlation.
    """
    raw = f"fs_{secrets.token_urlsafe(32)}"
    prefix = raw[:8]
    hashed = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode(
        "ascii"
    )
    return raw, hashed, prefix


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """Return True if *raw_key* matches the bcrypt *stored_hash*."""
    return bcrypt.checkpw(raw_key.encode("utf-8"), stored_hash.encode("ascii"))


# ── Envelope encryption (AES-256-GCM) ─────────────────────────────────

_AES_KEY_BYTES = 32  # AES-256
_GCM_NONCE_BYTES = 12


def _load_master_key(settings: Settings) -> bytes:
    """Return the raw 32-byte AES-256 master key from settings."""
    raw = settings.master_encryption_key
    if raw is None:
        msg = (
            "MASTER_ENCRYPTION_KEY is not configured — credential "
            "encryption is unavailable"
        )
        raise RuntimeError(msg)
    key_hex = raw.get_secret_value().encode("ascii")
    key = bytes.fromhex(key_hex.decode("ascii"))
    if len(key) != _AES_KEY_BYTES:
        msg = (
            f"MASTER_ENCRYPTION_KEY must be {_AES_KEY_BYTES} bytes "
            f"({_AES_KEY_BYTES * 2} hex chars), got {len(key)} bytes"
        )
        raise ValueError(msg)
    return key


def encrypt_credential(
    plaintext: str,
    settings: Settings,
) -> tuple[bytes, bytes]:
    """Envelope-encrypt *plaintext* with AES-256-GCM.

    Returns ``(ciphertext, nonce)``.  The nonce (12 bytes) must be stored
    alongside the ciphertext in the database.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _load_master_key(settings)
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(_GCM_NONCE_BYTES)
    # AESGCM.encrypt returns ciphertext + 16-byte GCM auth tag
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ciphertext, nonce


def decrypt_credential(
    ciphertext: bytes,
    nonce: bytes,
    settings: Settings,
) -> str:
    """Decrypt an AES-256-GCM envelope.

    Raises ``cryptography.exceptions.InvalidTag`` on tampered data.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _load_master_key(settings)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")


# ── RBAC permission helpers ───────────────────────────────────────────

# Resource:action permission strings.
# API keys can carry a subset; user roles have a fixed mapping.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*:*"},
    "user": {
        "transactions:read",
        "transactions:write",
        "accounts:read",
        "holdings:read",
        "balances:read",
        "securities:read",
        "sync:read",
        "sync:write",
        "webhooks:read",
        "webhooks:write",
        "webhooks:delete",
    },
    "readonly": {
        "transactions:read",
        "accounts:read",
        "holdings:read",
        "balances:read",
        "securities:read",
        "sync:read",
        "webhooks:read",
    },
    "viewer": {
        "transactions:read",
        "accounts:read",
        "balances:read",
    },
}


def user_has_permission(role: str, resource: str, action: str) -> bool:
    """Return True if *role* is allowed ``{resource}:{action}``."""
    perms = ROLE_PERMISSIONS.get(role, set())
    required = f"{resource}:{action}"
    if "*:*" in perms:
        return True
    if required in perms:
        return True
    # Wildcard resource match: resource:*
    return f"{resource}:*" in perms


def api_key_has_permission(
    key_permissions: str | None,
    resource: str,
    action: str,
) -> bool:
    """Return True if the API key's permission string allows access."""
    if key_permissions is None:
        return False
    required = f"{resource}:{action}"
    for perm in key_permissions.split():
        if perm == "*:*":
            return True
        if perm == required:
            return True
        if perm == f"{resource}:*":
            return True
    return False
