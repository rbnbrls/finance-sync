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
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

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


def resolve_wealthfolio_server_url(
    base_url: str, *, running_in_container: bool | None = None
) -> str:
    """Resolve host-local Wealthfolio URLs from a Dockerized finance-sync.

    The destination wizard and scheduled exporter run inside the finance-sync
    container.  A user-entered ``localhost`` URL therefore points back to
    finance-sync, while the browser's ``localhost`` points to the published
    Wealthfolio port on the Docker host.  Docker Desktop provides
    ``host.docker.internal`` for this boundary; Compose adds the equivalent
    host-gateway mapping on Linux.

    Explicit container mode is available for deterministic unit tests.  When
    omitted, the standard ``/.dockerenv`` marker is used.
    """
    if running_in_container is None:
        running_in_container = os.path.exists("/.dockerenv")
    if not running_in_container:
        return base_url

    parsed = urlsplit(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    # Keep credentials excluded by _safe_url and preserve the configured port,
    # path and query if a compatible caller supplies them.
    hostname = "host.docker.internal"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    resolved: SplitResult = parsed._replace(netloc=netloc)
    return urlunsplit(resolved)


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
            "base_url": resolve_wealthfolio_server_url(
                config.base_url.rstrip("/")
            ),
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
        tracking_mode: str = "TRANSACTIONS",
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
        tracking_mode: str = "TRANSACTIONS",
    ) -> dict[str, Any]:
        """Return the stable finance-sync account mapping, creating it once."""
        accounts = await self.get_accounts()
        for account in accounts:
            if (
                str(account.get("provider") or "").upper() == "FINANCE_SYNC"
                and account.get("providerAccountId") == provider_account_id
            ):
                if (
                    account.get("trackingMode")
                    and str(account["trackingMode"]).upper()
                    != tracking_mode.upper()
                ):
                    return await self.update_account_tracking_mode(
                        account, tracking_mode
                    )
                return account
        return await self.create_account(
            name=name,
            currency=currency,
            provider_account_id=provider_account_id,
            account_type=account_type,
            tracking_mode=tracking_mode,
        )

    async def delete_account(self, account_id: str) -> None:
        """Delete one Wealthfolio account and its account-owned data."""
        self._ensure_authenticated()
        response = await self._client.delete(
            f"{self.API_PREFIX}/accounts/{account_id}"
        )
        response.raise_for_status()

    async def delete_accounts_not_owned_by_finance_sync(
        self,
        provider_account_ids: set[str],
    ) -> int:
        """Remove accounts that are not part of the current export dataset.

        Wealthfolio is a projection of finance-sync for this exporter.  The
        provider account identity is the ownership boundary; names and the
        broad ``FINANCE_SYNC`` provider label are not sufficient because old
        smoke/test accounts may use a different identity format.
        """
        accounts = await self.get_accounts()
        removed = 0
        for account in accounts:
            account_id = account.get("id")
            if not account_id:
                continue
            provider_account_id = str(account.get("providerAccountId") or "")
            if provider_account_id in provider_account_ids:
                continue
            await self.delete_account(str(account_id))
            removed += 1
        return removed

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
        response = await self._post_with_408_retry(
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
        # Wealthfolio's check endpoint is not merely a validation boolean:
        # it returns the hydrated import rows, including resolved ``assetId``
        # and instrument metadata.  Importing the original rows loses that
        # resolution and causes trades to be persisted as cash activities.
        checked = await self.check_activities_import(activities)
        if isinstance(checked, list):
            # The check endpoint hydrates/resolves assets but some Wealthfolio
            # versions omit connector provenance in the returned rows. Merge
            # back missing source fields before import so reconciliation and
            # idempotency survive the check -> import round trip.
            hydrated: list[dict[str, Any]] = []
            for original, resolved in zip(activities, checked, strict=False):
                resolved_row = cast(dict[str, Any], resolved)
                merged: dict[str, Any] = dict(resolved_row)
                for key in (
                    "sourceSystem",
                    "sourceRecordId",
                    "sourceGroupId",
                    "idempotencyKey",
                    "importRunId",
                ):
                    if original.get(key) not in (None, ""):
                        merged[key] = original[key]
                for key, value in original.items():
                    if merged.get(key) in (None, "") and value not in (
                        None,
                        "",
                    ):
                        merged[key] = value
                hydrated.append(merged)
            activities = hydrated
        # Some Wealthfolio versions return hydrated rows without ``assetId``
        # even though the asset has already been created/resolved by the
        # check endpoint.  The import endpoint then accepts the row but stores
        # a blank asset id; the later portfolio snapshot fails with
        # ``Invalid asset_id for position``.  Resolve the asset explicitly
        # from the stable ISIN/display code before importing.
        asset_activity_types = {
            "BUY",
            "SELL",
            "DIVIDEND",
            "CAPITAL_GAIN",
            "REINVEST",
        }
        if any(
            activity.get("activityType") in asset_activity_types
            and not activity.get("assetId")
            for activity in activities
        ):
            assets = await self.get_assets()
            by_code: dict[str, dict[str, Any]] = {}
            for asset in assets:
                if not asset.get("id"):
                    continue
                for value in (
                    asset.get("displayCode"),
                    asset.get("instrumentSymbol"),
                    asset.get("symbol"),
                    asset.get("isin"),
                ):
                    if value:
                        by_code.setdefault(str(value).strip().upper(), asset)
            resolved_activities: list[dict[str, Any]] = []
            for activity in activities:
                resolved = dict(activity)
                if not resolved.get("assetId") and resolved.get(
                    "activityType"
                ) not in {
                    "DEPOSIT",
                    "WITHDRAWAL",
                    "CREDIT",
                    "TAX",
                    "FEE",
                    "INTEREST",
                    "TRANSFER_IN",
                    "TRANSFER_OUT",
                }:
                    candidates = (
                        resolved.get("isin"),
                        resolved.get("symbol"),
                    )
                    asset = next(
                        (
                            by_code.get(str(candidate).strip().upper())
                            for candidate in candidates
                            if candidate
                        ),
                        None,
                    )
                    if asset is None:
                        symbol = next(
                            (
                                str(candidate).strip().upper()
                                for candidate in candidates
                                if candidate
                            ),
                            "",
                        )
                        if symbol:
                            requested_type = str(
                                resolved.get("instrumentType") or "EQUITY"
                            ).upper()
                            asset = await self.create_asset(
                                symbol=symbol,
                                currency=str(
                                    resolved.get("quoteCcy")
                                    or resolved.get("currency")
                                    or "EUR"
                                ),
                                name=resolved.get("symbolName"),
                                instrument_type=(
                                    requested_type
                                    if requested_type
                                    in {
                                        "EQUITY",
                                        "CRYPTO",
                                        "FX",
                                        "OPTION",
                                        "METAL",
                                        "BOND",
                                    }
                                    else "EQUITY"
                                ),
                                isin=resolved.get("isin"),
                                exchange_mic=resolved.get("exchangeMic"),
                                provider_id=resolved.get("providerId"),
                                provider_symbol=resolved.get("providerSymbol"),
                            )
                            for value in (
                                asset.get("displayCode"),
                                asset.get("instrumentSymbol"),
                                asset.get("symbol"),
                                asset.get("isin"),
                            ):
                                if value:
                                    by_code.setdefault(
                                        str(value).strip().upper(), asset
                                    )
                    if asset is not None:
                        resolved["assetId"] = asset["id"]
                resolved_activities.append(resolved)
            activities = resolved_activities
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
        manual_quotes: list[tuple[str, dict[str, Any]]] = []
        # The manual snapshot endpoint accepts ``assetId`` (and
        # ``averageCost``), not the CSV-only ``unitPrice`` field.  Resolve
        # existing assets before saving so Wealthfolio updates the intended
        # positions instead of creating anonymous snapshot assets.
        assets = await self.get_assets()
        by_symbol = {
            str(value): asset
            for asset in assets
            if asset.get("id")
            for value in (
                asset.get("displayCode"),
                asset.get("instrumentSymbol"),
                asset.get("symbol"),
                asset.get("isin"),
            )
            if value
        }
        resolved_holdings: list[dict[str, Any]] = []
        for holding in holdings:
            resolved = dict(holding)
            symbol = str(holding.get("symbol") or "")
            asset = by_symbol.get(
                str(holding.get("_securityIsin") or "")
            ) or by_symbol.get(symbol)
            if asset is not None:
                resolved["assetId"] = asset["id"]
            resolved.pop("_securityIsin", None)
            resolved.pop("unitPrice", None)
            # ``POST /snapshots`` uses ``averageCost`` for the cost basis.
            # Never silently drop it: without this field Wealthfolio accepts
            # the request but produces no security holdings on recalculation.
            if resolved.get("averageCost") is None:
                resolved.pop("averageCost", None)
            resolved_holdings.append(resolved)
        payload = {
            "accountId": account_id,
            "holdings": resolved_holdings,
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
            for raw_symbol in (
                asset.get("displayCode"),
                asset.get("symbol"),
                asset.get("instrumentSymbol"),
                asset.get("isin"),
            ):
                if raw_symbol:
                    symbol = str(raw_symbol)
                    by_symbol.setdefault(symbol, asset)
                    by_symbol.setdefault(_normalise_asset_symbol(symbol), asset)
        for holding in holdings:
            price = holding.get("unitPrice")
            symbol = str(holding.get("symbol") or "")
            asset = (
                by_symbol.get(str(holding.get("_securityIsin") or ""))
                or by_symbol.get(symbol)
                or by_symbol.get(_normalise_asset_symbol(symbol))
            )
            if price is None or asset is None:
                continue
            # Connector-owned snapshots are authoritative for the current
            # valuation. Keep the asset in MANUAL mode so Wealthfolio does
            # not retry remote providers for an ISIN-only symbol.
            await self.update_quote_mode(str(asset["id"]), "MANUAL")
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
                "source": "MANUAL",
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
            manual_quotes.append((str(asset["id"]), quote))
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
            # A zero-price quantity correction is intentionally the latest
            # activity, so Wealthfolio may restore its 0.01 fallback quote
            # during the final snapshot recalculation.  Write the broker
            # quotes once more after that recalculation.
            for asset_id, quote in manual_quotes:
                quote_response = await self._client.put(
                    f"{self.API_PREFIX}/market-data/quotes/{asset_id}",
                    json=quote,
                )
                quote_response.raise_for_status()
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

    async def upsert_quote(self, asset_id: str, quote: dict[str, Any]) -> None:
        """Store one connector-owned quote for an asset idempotently."""
        self._ensure_authenticated()
        quote_date = str(quote.get("timestamp", ""))[:10]
        for existing in await self.get_quote_history(asset_id):
            if (
                (existing.get("source") or existing.get("dataSource"))
                == "FINANCE_SYNC"
                and str(existing.get("timestamp", ""))[:10] == quote_date
                and existing.get("id")
            ):
                await self.delete_quote(str(existing["id"]))
        response = await self._client.put(
            f"{self.API_PREFIX}/market-data/quotes/{asset_id}", json=quote
        )
        response.raise_for_status()

    async def get_assets(self) -> list[dict[str, Any]]:
        """Fetch Wealthfolio assets used to attach manual quotes."""
        self._ensure_authenticated()
        response = await self._client.get(f"{self.API_PREFIX}/assets")
        response.raise_for_status()
        return response.json()

    async def update_quote_mode(
        self, asset_id: str, quote_mode: str
    ) -> dict[str, Any]:
        """Set an asset's market-data mode (``MARKET`` or ``MANUAL``)."""
        self._ensure_authenticated()
        response = await self._client.put(
            f"{self.API_PREFIX}/assets/pricing-mode/{asset_id}",
            json={"quoteMode": quote_mode},
        )
        response.raise_for_status()
        return response.json()

    async def create_asset(
        self,
        *,
        symbol: str,
        currency: str,
        name: str | None = None,
        instrument_type: str = "EQUITY",
        isin: str | None = None,
        exchange_mic: str | None = None,
        provider_id: str | None = None,
        provider_symbol: str | None = None,
    ) -> dict[str, Any]:
        """Create a missing investment asset for an imported instrument."""
        self._ensure_authenticated()
        display_code = symbol.strip().upper()
        identity = {
            key: value
            for key, value in {
                "isin": isin,
                "instrumentExchangeMic": exchange_mic,
                "providerId": provider_id,
                "providerSymbol": provider_symbol,
            }.items()
            if value
        }
        response = await self._client.post(
            f"{self.API_PREFIX}/assets",
            json={
                "kind": "INVESTMENT",
                "name": name or display_code,
                "displayCode": display_code,
                "isActive": True,
                "quoteMode": (
                    "MARKET"
                    if any(
                        identity.get(key)
                        for key in (
                            "isin",
                            "instrumentExchangeMic",
                            "providerId",
                            "providerSymbol",
                        )
                    )
                    else "MANUAL"
                ),
                "quoteCcy": currency.upper(),
                "instrumentType": instrument_type.upper(),
                "instrumentSymbol": display_code,
                "instrumentExchangeMic": None,
                "providerId": "FINANCE_SYNC",
                "providerSymbol": display_code,
                **identity,
            },
        )
        if response.is_error:
            detail = (
                "Asset creation failed "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )
            raise WealthfolioAPIError(detail)
        return response.json()

    async def add_exchange_rate(
        self,
        *,
        from_currency: str,
        to_currency: str,
        rate: str,
        source: str = "FINANCE_SYNC",
    ) -> dict[str, Any]:
        """Create a Wealthfolio FX pair used by historical FX quotes."""
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/exchange-rates",
            json={
                "fromCurrency": from_currency,
                "toCurrency": to_currency,
                "rate": rate,
                "source": source,
            },
        )
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

    async def get_performance_history(
        self, account_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """Trigger Wealthfolio's historical valuation/performance read."""
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/performance/history",
            json={
                "itemType": "account",
                "itemId": account_id,
                "startDate": start_date,
                "endDate": end_date,
            },
        )
        response.raise_for_status()
        return response.json()

    async def search_activities(
        self, account_id: str, *, page: int = 0, page_size: int = 1000
    ) -> dict[str, Any]:
        """Read activity counts for a production-safe smoke check."""
        self._ensure_authenticated()
        response = await self._client.post(
            f"{self.API_PREFIX}/activities/search",
            json={
                "page": page,
                "pageSize": page_size,
                "accountIdFilter": account_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def get_all_activities(self, account_id: str) -> list[dict[str, Any]]:
        """Read all activities for an account, not just the first page."""
        page = 0
        rows: list[dict[str, Any]] = []
        while True:
            payload = await self.search_activities(account_id, page=page)
            page_rows = payload.get("data", payload.get("activities", []))
            if not isinstance(page_rows, list) or not page_rows:
                break
            typed_page_rows = cast("list[Any]", page_rows)
            rows.extend(
                cast("dict[str, Any]", row)
                for row in typed_page_rows
                if isinstance(row, dict)
            )
            raw_meta = payload.get("meta")
            meta = (
                cast("dict[str, Any]", raw_meta)
                if isinstance(raw_meta, dict)
                else {}
            )
            total = meta.get("totalRowCount")
            if isinstance(total, int) and len(rows) >= total:
                break
            if len(typed_page_rows) < 1000:
                break
            page += 1
        return rows

    async def delete_activity(self, activity_id: str) -> None:
        """Delete one activity during an explicit destination rebuild."""
        self._ensure_authenticated()
        response = await self._client.delete(
            f"{self.API_PREFIX}/activities/{activity_id}"
        )
        response.raise_for_status()

    async def delete_activities(self, account_id: str) -> int:
        """Delete all activities belonging to one account."""
        rows = await self.get_all_activities(account_id)
        removed = 0
        for row in rows:
            if row.get("id"):
                await self.delete_activity(str(row["id"]))
                removed += 1
        return removed

    async def delete_activities_not_in(
        self,
        account_id: str,
        external_transaction_ids: set[str],
        *,
        preserved_comment_prefixes: tuple[str, ...] = (),
    ) -> int:
        """Remove destination activities not present in the source dataset."""
        rows = await self.get_all_activities(account_id)
        removed = 0
        for row in rows:
            comment = str(row.get("comment") or "")
            if any(
                comment.startswith(prefix)
                for prefix in preserved_comment_prefixes
            ):
                continue
            source_id = (
                comment.split("ID:", 1)[-1].strip() if "ID:" in comment else ""
            )
            if source_id not in external_transaction_ids and row.get("id"):
                await self.delete_activity(str(row["id"]))
                removed += 1
        return removed

    async def delete_cash_reconciliation_activities(
        self, account_id: str, source_prefix: str
    ) -> int:
        """Remove prior cash corrections before recalculation."""
        rows = await self.get_all_activities(account_id)
        removed = 0
        for row in rows:
            comment = str(row.get("comment") or "")
            if comment.startswith(source_prefix) and row.get("id"):
                await self.delete_activity(str(row["id"]))
                removed += 1
        return removed

    async def delete_activities_by_comment_prefix(
        self, account_id: str, source_prefix: str
    ) -> int:
        """Remove connector-owned activities identified by a comment prefix."""
        rows = await self.get_all_activities(account_id)
        removed = 0
        for row in rows:
            if str(row.get("comment") or "").startswith(
                source_prefix
            ) and row.get("id"):
                await self.delete_activity(str(row["id"]))
                removed += 1
        return removed

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
        17s on the production instance).  Only slow Wealthfolio POST
        endpoints use this helper; ordinary fast endpoints keep fail-fast
        semantics.
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
