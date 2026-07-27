"""HTTP API client for Wealthfolio self-hosted instances.

Provides authenticated access to Wealthfolio's REST API for importing
activities and holdings programmatically, without going through the
browser CSV import wizard.

Usage::

    client = WealthfolioClient(
        config=WealthfolioClientConfig(
            base_url="http://192.168.3.50:8080",
            password="your-password",
        ),
    )
    await client.authenticate()
    result = await client.import_activities(activities)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

# ═══════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════


class WealthfolioClientError(Exception):
    """Base exception for Wealthfolio client errors."""


class WealthfolioAuthError(WealthfolioClientError):
    """Authentication failed (wrong password, connection error, etc.)."""


class WealthfolioAPIError(WealthfolioClientError):
    """Wealthfolio API returned an error response."""


# ═══════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class WealthfolioClientConfig:
    """Configuration for the Wealthfolio HTTP API client.

    Attributes:
        base_url:        Base URL of the Wealthfolio instance
                         (e.g. ``http://192.168.3.50:8080``).
        password:        Password for authentication.
        request_timeout: HTTP request timeout in seconds (default 60).
        verify_ssl:      Whether to verify SSL certificates (default True).
    """

    base_url: str
    password: str
    request_timeout: float = 60.0
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        if not self.base_url:
            msg = "base_url must be a non-empty URL"
            raise ValueError(msg)
        if not self.password:
            msg = "password must be non-empty"
            raise ValueError(msg)


# ═══════════════════════════════════════════════════════════════════════
# Client
# ═══════════════════════════════════════════════════════════════════════


class WealthfolioClient:
    """HTTP client for the Wealthfolio REST API.

    Handles authentication via password-based login and provides methods
    for importing activities, holdings, and managing accounts.

    Thread-safe: each instance uses its own ``httpx.AsyncClient``.
    """

    API_PREFIX = "/api/v1"

    def __init__(self, config: WealthfolioClientConfig) -> None:
        self._config = config
        self._is_authenticated: bool = False

        # Build the httpx async client
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=config.request_timeout,
            verify=config.verify_ssl,
        )

    # ── Properties ──────────────────────────────────────────────────

    @property
    def is_authenticated(self) -> bool:
        """Whether the client has been successfully authenticated."""
        return self._is_authenticated

    @property
    def base_url(self) -> str:
        """The Wealthfolio instance base URL."""
        return self._config.base_url

    # ── Auth ────────────────────────────────────────────────────────

    async def check_auth_status(self) -> dict[str, Any]:
        """Check the authentication status of the Wealthfolio instance.

        Returns:
            A dict with ``requiresPassword`` and ``oidcEnabled`` flags.
        """
        response = await self._client.get(f"{self.API_PREFIX}/auth/status")
        response.raise_for_status()
        return response.json()

    async def authenticate(self) -> bool:
        """Authenticate with the Wealthfolio instance.

        Sends the password to the login endpoint.  On success the
        session cookie is stored automatically by ``httpx``.

        Returns:
            ``True`` if authentication was successful.

        Raises:
            WealthfolioAuthError: If authentication fails or the
                Wealthfolio instance is unreachable.
        """
        try:
            response = await self._client.post(
                f"{self.API_PREFIX}/auth/login",
                json={"password": self._config.password},
            )
            if response.status_code == 200:
                self._is_authenticated = True
                return True

            # Try to extract error details
            try:
                body = response.json()
                message = body.get("message", "Unknown error")
            except Exception:
                message = f"HTTP {response.status_code}"

            self._is_authenticated = False
            msg = f"Authentication failed: {message}"
            raise WealthfolioAuthError(msg)

        except httpx.RequestError as exc:
            self._is_authenticated = False
            msg = f"Connection failed: {exc}"
            raise WealthfolioAuthError(msg) from exc

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        await self._client.aclose()

    async def __aenter__(self) -> WealthfolioClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Public API: Accounts ────────────────────────────────────────

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Fetch all accounts from Wealthfolio.

        Returns:
            A list of account dicts with keys like ``id``, ``name``,
            ``currency``, ``isActive``.
        """
        self._ensure_authenticated()
        response = await self._client.get(f"{self.API_PREFIX}/accounts")
        response.raise_for_status()
        return response.json()

    # ── Public API: Activities ──────────────────────────────────────

    async def check_activities_import(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Check activities before importing (validation step).

        Args:
            activities: List of activity dicts to validate.

        Returns:
            Validation result with ``valid`` and ``issues`` keys.
        """
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/activities/import/check",
            json={"activities": activities},
        )
        response.raise_for_status()
        return response.json()

    async def import_activities(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Import activities into Wealthfolio.

        This is the primary method for pushing transaction data into
        Wealthfolio.  Activities should be in Wealthfolio's import
        format (see Wealthfolio CSV import docs for field details).

        Args:
            activities: List of activity dicts to import. Each activity
                        should have fields like ``accountId``,
                        ``activityType``, ``symbol``, ``quantity``,
                        ``unitPrice``, ``amount``, ``currency``,
                        ``date``, ``comment``.

        Returns:
            Import result with ``imported``, ``skipped``, ``failed``
            counts.
        """
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/activities/import",
            json={"activities": activities},
        )
        response.raise_for_status()
        return response.json()

    async def push_activities(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Convenience: check then import activities.

        Runs the check endpoint first, then imports if validation passes.

        Args:
            activities: List of activity dicts to import.

        Returns:
            Import result dict.
        """
        self._ensure_authenticated()
        await self.check_activities_import(activities)
        return await self.import_activities(activities)

    # ── Public API: Holdings / Snapshots ────────────────────────────

    async def import_holdings(
        self,
        holdings: list[dict[str, Any]],
        account_id: str,
    ) -> dict[str, Any]:
        """Import holdings/snapshots into Wealthfolio.

        Args:
            holdings: List of holding dicts with fields like ``symbol``,
                      ``quantity``, ``avgCost``, ``currency``, ``date``.
            account_id: Target Wealthfolio account ID.

        Returns:
            Import result dict.
        """
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/snapshots/import",
            json={
                "accountId": account_id,
                "snapshots": holdings,
            },
        )
        response.raise_for_status()
        return response.json()

    # ── Internal helpers ────────────────────────────────────────────

    def _ensure_authenticated(self) -> None:
        """Raise if the client is not authenticated."""
        if not self._is_authenticated:
            msg = "Not authenticated. Call authenticate() first."
            raise WealthfolioAuthError(msg)
