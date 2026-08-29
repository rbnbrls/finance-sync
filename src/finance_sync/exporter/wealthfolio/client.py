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

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from httpx import AsyncBaseTransport

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
        retry_408:       Retry with backoff when the server answers HTTP
                         408 (its own request-timeout cap aborted a slow
                         holdings recalculation).  Default True.
        retry_408_attempts:   Max attempts for a 408 retry (default 3).
        retry_408_base_delay: Base backoff in seconds (default 2.0,
                             doubled per attempt).
    """

    base_url: str
    password: str
    request_timeout: float = 60.0
    verify_ssl: bool = True
    # Retry a request when Wealthfolio answers HTTP 408 (its own
    # WF_REQUEST_TIMEOUT_MS cap aborted a slow holdings recalculation).
    # The server keeps working after the abort, so a retry commonly
    # succeeds (observed 17s vs 30s cap on the prod instance).
    retry_408: bool = True
    retry_408_attempts: int = 3
    retry_408_base_delay: float = 2.0

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

    def __init__(
        self,
        config: WealthfolioClientConfig,
        transport: AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client.

        Args:
            config:    Connection configuration (base URL + password).
            transport: Optional httpx transport to inject (used by tests
                       and the network-privacy audit to record every
                       outbound request).  Defaults to the standard
                       httpx async transport.
        """
        self._config = config
        self._is_authenticated: bool = False

        # Build the httpx async client
        kwargs: dict[str, Any] = {
            "base_url": config.base_url.rstrip("/"),
            "timeout": config.request_timeout,
            "verify": config.verify_ssl,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

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

    async def create_account(
        self,
        *,
        name: str,
        currency: str,
        provider_account_id: str,
        account_type: str = "SECURITIES",
        tracking_mode: str = "HOLDINGS",
    ) -> dict[str, Any]:
        """Create a Wealthfolio account with the requested tracking mode."""
        self._ensure_authenticated()
        normalized_type = account_type.upper()
        if normalized_type not in {"CASH", "SECURITIES"}:
            message = f"Unsupported Wealthfolio account type: {account_type}"
            raise ValueError(message)
        normalized_tracking_mode = tracking_mode.upper()
        if normalized_tracking_mode not in {"TRANSACTIONS", "HOLDINGS"}:
            message = f"Unsupported tracking mode: {tracking_mode}"
            raise ValueError(message)
        response = await self._client.post(
            f"{self.API_PREFIX}/accounts",
            json={
                "name": name,
                "accountType": normalized_type,
                "group": (
                    "Cash" if normalized_type == "CASH" else "Investments"
                ),
                "currency": currency,
                "isDefault": False,
                "isActive": True,
                "isArchived": False,
                "trackingMode": normalized_tracking_mode,
                "platformId": None,
                "accountNumber": None,
                "meta": '{"managedBy":"finance-sync"}',
                "provider": "FINANCE_SYNC",
                "providerAccountId": provider_account_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def update_account_tracking_mode(
        self, account: dict[str, Any], tracking_mode: str
    ) -> dict[str, Any]:
        """Change an existing account to the mode required by its export."""
        self._ensure_authenticated()
        normalized = tracking_mode.upper()
        response = await self._client.put(
            f"{self.API_PREFIX}/accounts/{account['id']}",
            json={
                "id": account["id"],
                "name": account["name"],
                "accountType": account["accountType"],
                "currency": account["currency"],
                "isDefault": account.get("isDefault", False),
                "isActive": account.get("isActive", True),
                "isArchived": account.get("isArchived", False),
                "trackingMode": normalized,
                "group": account.get("group"),
                "provider": account.get("provider"),
                "providerAccountId": account.get("providerAccountId"),
                "meta": account.get("meta"),
            },
        )
        response.raise_for_status()
        return response.json()

    async def ensure_account(
        self,
        *,
        name: str,
        currency: str,
        provider_account_id: str,
        account_type: str = "SECURITIES",
        tracking_mode: str = "HOLDINGS",
    ) -> dict[str, Any]:
        """Return the stable finance-sync account mapping, creating it once."""
        accounts = await self.get_accounts()
        for account in accounts:
            if (
                str(account.get("provider") or "").upper() == "FINANCE_SYNC"
                and account.get("providerAccountId") == provider_account_id
            ):
                return account
        return await self.create_account(
            name=name,
            currency=currency,
            provider_account_id=provider_account_id,
            account_type=account_type,
            tracking_mode=tracking_mode,
        )

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
        payload = response.json()
        # Wealthfolio 2.x returns counts in ``summary``; older compatible
        # servers returned them at the top level. Expose one stable contract.
        summary = payload.get("summary", payload)
        return {
            "imported": int(summary.get("imported", 0)),
            "skipped": int(
                summary.get("skipped", 0) + summary.get("duplicates", 0)
            ),
            "failed": int(
                0
                if summary.get("success", True)
                else summary.get("total", len(activities))
            ),
            "raw": payload,
        }

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

    async def save_manual_holdings(
        self,
        holdings: list[dict[str, Any]],
        account_id: str,
        *,
        cash_balances: dict[str, str] | None = None,
        snapshot_date: str,
    ) -> dict[str, Any]:
        """Save a current manual holdings snapshot.

        Wealthfolio's CSV snapshot importer stores historical positions but
        does not use the supplied price as the current quote.  The account
        holdings form uses ``POST /snapshots`` and manual quotes, which is
        the contract needed for connector-owned current valuations.
        """
        self._ensure_authenticated()
        payload = {
            "accountId": account_id,
            "holdings": holdings,
            "cashBalances": cash_balances or {},
            "snapshotDate": snapshot_date,
        }
        # POST /snapshots triggers a full holdings recalculation, the slow
        # path that Wealthfolio aborts with HTTP 408 at its own
        # WF_REQUEST_TIMEOUT_MS cap.  Retry with backoff so a legitimate
        # slow recalculation is not surfaced as a hard sync failure.
        response = await self._post_with_408_retry(
            f"{self.API_PREFIX}/snapshots", json=payload
        )
        response.raise_for_status()

        # The manual snapshot records quantities; current prices come from
        # Wealthfolio's manual quote table.  Resolve the assets after the
        # snapshot so symbols newly created by this request are included.
        assets = await self.get_assets()
        by_symbol: dict[str, dict[str, Any]] = {}
        for asset in assets:
            if not asset.get("id"):
                continue
            for raw_symbol in (asset.get("displayCode"), asset.get("symbol")):
                if raw_symbol:
                    symbol = str(raw_symbol)
                    by_symbol.setdefault(symbol, asset)
                    by_symbol.setdefault(_normalise_asset_symbol(symbol), asset)
        for holding in holdings:
            price = holding.get("unitPrice")
            symbol = str(holding.get("symbol") or "")
            asset = by_symbol.get(symbol) or by_symbol.get(
                _normalise_asset_symbol(symbol)
            )
            if price is None or asset is None:
                continue
            source_value = holding.get("sourceValue")
            if source_value is not None:
                quantity = Decimal(str(holding["quantity"]))
                stored_quantity = quantity.quantize(Decimal("0.01"))
                if stored_quantity:
                    price = str(Decimal(str(source_value)) / stored_quantity)
            existing_quotes = await self.get_quote_history(str(asset["id"]))
            for existing in existing_quotes:
                if (
                    existing.get("dataSource") == "MANUAL"
                    and str(existing.get("timestamp", ""))[:10] == snapshot_date
                ):
                    await self.delete_quote(str(existing["id"]))
            quote = {
                "id": f"{asset['id']}_{datetime.now(UTC).timestamp()}_MANUAL",
                "createdAt": datetime.now(UTC).isoformat(),
                "dataSource": "MANUAL",
                "timestamp": datetime.now(UTC).isoformat(),
                "assetId": asset["id"],
                "open": price,
                "high": price,
                "low": price,
                "volume": 0,
                "close": price,
                "adjclose": price,
                "currency": holding.get("currency") or "EUR",
            }
            quote_response = await self._client.put(
                f"{self.API_PREFIX}/market-data/quotes/{asset['id']}",
                json=quote,
            )
            quote_response.raise_for_status()

        # Re-save after quotes are present so the live account valuation is
        # recalculated immediately, including on a first-time asset import.
        if holdings:
            response = await self._post_with_408_retry(
                f"{self.API_PREFIX}/snapshots", json=payload
            )
            response.raise_for_status()
        return response.json() if response.content else {}

    async def get_quote_history(self, asset_id: str) -> list[dict[str, Any]]:
        """Fetch stored quotes for an asset."""
        self._ensure_authenticated()
        response = await self._client.get(
            f"{self.API_PREFIX}/market-data/quotes/history",
            params={"symbol": asset_id},
        )
        response.raise_for_status()
        return response.json()

    async def delete_quote(self, quote_id: str) -> None:
        """Delete one quote owned by the connector."""
        self._ensure_authenticated()
        response = await self._client.delete(
            f"{self.API_PREFIX}/market-data/quotes/id/{quote_id}"
        )
        response.raise_for_status()

    async def get_assets(self) -> list[dict[str, Any]]:
        """Fetch Wealthfolio assets used to attach manual quotes."""
        self._ensure_authenticated()
        response = await self._client.get(f"{self.API_PREFIX}/assets")
        response.raise_for_status()
        return response.json()

    async def check_holdings_import(
        self,
        holdings: list[dict[str, Any]],
        account_id: str,
    ) -> dict[str, Any]:
        """Validate holdings snapshots without writing them."""
        self._ensure_authenticated()
        response = await self._post_with_408_retry(
            f"{self.API_PREFIX}/snapshots/import/check",
            json={"accountId": account_id, "snapshots": holdings},
        )
        response.raise_for_status()
        return response.json()

    async def get_holdings(self, account_id: str) -> list[dict[str, Any]]:
        """Read Wealthfolio's current holdings for reconciliation."""
        self._ensure_authenticated()
        response = await self._client.get(
            f"{self.API_PREFIX}/holdings/list",
            params={"accountId": account_id},
        )
        response.raise_for_status()
        return response.json()

    async def search_activities(
        self, account_id: str, *, page_size: int = 1000
    ) -> dict[str, Any]:
        """Read activity counts for a production-safe smoke check."""
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/activities/search",
            json={
                "page": 0,
                "pageSize": page_size,
                "accountIdFilter": account_id,
            },
        )
        response.raise_for_status()
        return response.json()

    # ── Internal helpers ────────────────────────────────────────────

    async def _post_with_408_retry(
        self,
        url: str,
        *,
        json: dict[str, Any],
    ) -> httpx.Response:
        """POST *url*, retrying with backoff when the server answers 408.

        Wealthfolio aborts slow requests at its own ``WF_REQUEST_TIMEOUT_MS``
        cap (30s default) and answers HTTP 408 while the underlying work
        keeps running.  A retry after a short backoff normally completes the
        request (observed: the snapshot POST failed at 30s then succeeded at
        17s on the production instance).  Only the slow recalculation
        endpoints (snapshots / holdings) use this helper; ordinary fast
        endpoints keep fail-fast semantics.
        """
        attempts = (
            self._config.retry_408_attempts if self._config.retry_408 else 1
        )
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            response = await self._client.post(url, json=json)
            if response.status_code != 408 or attempt == attempts:
                return response
            delay = self._config.retry_408_base_delay * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
        assert response is not None  # loop above always returns on last attempt
        return response  # pragma: no cover - defensive for type checkers

    def _ensure_authenticated(self) -> None:
        """Raise if the client is not authenticated."""
        if not self._is_authenticated:
            msg = "Not authenticated. Call authenticate() first."
            raise WealthfolioAuthError(msg)


def _normalise_asset_symbol(symbol: str) -> str:
    """Return the comparison symbol Wealthfolio exposes for a security."""
    value = symbol.strip().upper()
    if len(value) == 12 and value[:2].isalpha() and value[2:].isalnum():
        return value
    return value.split(".", 1)[0]
