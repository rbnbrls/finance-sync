"""Credential management helpers for the finance-sync-sdk.

Plugins can use these utilities to manage authentication secrets in a
consistent way across the ecosystem.
"""

from __future__ import annotations


class CredentialProvider:
    """Abstract credential retrieval strategy.

    The host application provides a ``CredentialProvider`` to plugins,
    allowing them to request secrets without knowing how they are stored
    (environment variables, vault, encrypted store, etc.).

    Usage in a plugin::

        async def authenticate(self) -> None:
            api_key = await self.credential_provider.get("api_key")
            if not api_key:
                raise PermanentError("api_key is required")
            self._http.headers["Authorization"] = f"Bearer {api_key}"
    """

    async def get(self, key: str) -> str | None:
        """Retrieve a credential by *key*.

        Returns ``None`` if the credential is not available.
        """
        raise NotImplementedError

    async def get_all(self) -> dict[str, str]:
        """Retrieve all available credentials."""
        raise NotImplementedError


class EnvCredentialProvider(CredentialProvider):
    """Credential provider backed by environment variables.

    Maps credential keys to environment variable names using an optional
    prefix.

    Usage::

        provider = EnvCredentialProvider(prefix="MYBANK_")
        api_key = await provider.get("API_KEY")  # reads MYBANK_API_KEY
    """

    def __init__(self, prefix: str = "", mapping: dict[str, str] | None = None) -> None:
        self._prefix = prefix
        self._mapping = mapping or {}

    async def get(self, key: str) -> str | None:
        import os

        env_var = self._mapping.get(key, f"{self._prefix}{key}")
        return os.environ.get(env_var)

    async def get_all(self) -> dict[str, str]:
        import os

        return {k: v for k, v in os.environ.items() if k.startswith(self._prefix)}


class DictCredentialProvider(CredentialProvider):
    """In-memory credential provider for testing/demo."""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self._credentials = dict(credentials or {})

    async def get(self, key: str) -> str | None:
        return self._credentials.get(key)

    async def get_all(self) -> dict[str, str]:
        return dict(self._credentials)

    def set(self, key: str, value: str) -> None:
        """Set a credential value (mutates in place)."""
        self._credentials[key] = value
