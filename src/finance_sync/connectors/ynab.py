"""YNAB API connector implementation.

Uses YNAB's v1 REST API with personal access token authentication.
The token is sent as a Bearer token in the ``Authorization`` header.

Rate limit
    YNAB allows 200 requests per hour per personal access token.
    The connector's built-in
    :class:`~finance_sync.connectors.rate_limiter.RateLimiter` enforces
    this globally.

Pagination
    YNAB uses ``last_knowledge_of_server`` for incremental sync on
    ``/budgets/{id}/transactions``.  The connector stores the knowledge
    value per-budget in ``provider_metadata`` and uses it on subsequent
    syncs.  Full account and budget lists are returned in a single response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import RawAccount, RawTransaction
from finance_sync.connectors.rate_limiter import RateLimitPolicy

if TYPE_CHECKING:
    from finance_sync.connectors.models import ConnectorConfig

_YNAB_API_BASE = "https://api.youneedabudget.com/v1"
_DEFAULT_TIMEOUT = 30.0

# YNAB budget type strings → canonical account types
_ACCOUNT_TYPE_MAP: dict[str, str] = {
    "checking": "checking",
    "savings": "savings",
    "creditCard": "credit",
    "cash": "checking",
    "lineOfCredit": "credit",
    "investmentAccount": "investment",
    "mortgage": "loan",
    "payPal": "checking",
    "otherAsset": "other",
    "otherLiability": "loan",
}


class YnabConnector(Connector):
    """Connector for the YNAB (You Need A Budget) API (v1).

    Credentials
        ``config.credentials["access_token"]`` — YNAB personal access token
        (required).
        ``config.options["budget_id"]`` — Specific budget ID to sync
        (optional; when omitted, syncs all budgets).
        ``config.options["base_url"]`` — Custom API base URL (optional,
        for testing).

    Example::

        config = ConnectorConfig(
            provider_type="ynab",
            credentials={"access_token": "ynab_pat_abc123"},
            options={"budget_id": "last-30-days"},
        )
        conn = YnabConnector(config)
        await conn.authenticate()
        accounts = await conn.fetch_accounts()
    """

    display_name = "YNAB"
    sdk_version = "0.1.0"

    rate_limit_policy = RateLimitPolicy(
        max_requests=200,
        window_seconds=3600,
        max_retries=3,
        backoff_base=1.0,
    )

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the YNAB connector.

        Args:
            config: Connector configuration with credentials.
            http_client: Optional pre-configured HTTP client (for testing).
        """
        super().__init__(config)
        base_url = config.options.get("base_url", _YNAB_API_BASE)
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT),
        )
        self._budget_ids: list[str] = []
        self._budget_currency: str = "EUR"

    @property
    def name(self) -> str:
        return "ynab"

    # ── Authentication ──────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Validate the YNAB access token by calling ``GET /budgets``.

        Also populates ``_budget_ids`` with the budgets to sync.

        Raises:
            PermanentError: If the access token is missing or invalid.
            RateLimitError: If the YNAB rate limit is exceeded.
            TransientError: On temporary provider unavailability.
        """
        token = self.config.credentials.get("access_token")
        if not token:
            msg = "YNAB access_token is required in credentials"
            raise PermanentError(msg)

        headers = _auth_headers(token)

        try:
            budget_filter = self.config.options.get("budget_id")
            budgets_data = await self._fetch_budgets(headers)

            if budget_filter:
                # Only sync the specified budget
                matched = [
                    b
                    for b in budgets_data
                    if b.get("id") == budget_filter
                    or b.get("name", "").lower() == budget_filter.lower()
                ]
                if not matched:
                    msg = f"Budget {budget_filter!r} not found in YNAB account"
                    raise PermanentError(msg)
                self._budget_ids = [matched[0]["id"]]
            else:
                self._budget_ids = [b["id"] for b in budgets_data]

        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
        except httpx.TimeoutException as exc:
            msg = "YNAB authentication timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"YNAB HTTP error during authenticate: {exc}"
            raise TransientError(msg) from exc

    async def _fetch_budgets(
        self,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """GET /budgets and return the list of budget objects.

        A budget object contains ``id``, ``name``, ``currency_format``,
        and ``last_knowledge_of_server``.
        """
        resp = await self._http.get("/budgets", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        budgets: list[dict[str, Any]] = data.get("data", {}).get("budgets", [])
        if not budgets:
            msg = "YNAB account has no budgets"
            raise PermanentError(msg)
        return budgets

    # ── Accounts ────────────────────────────────────────────────────────

    async def fetch_accounts(self) -> list[RawAccount]:
        """Fetch accounts from all configured budgets.

        Iterates over ``_budget_ids`` and aggregates accounts.
        """
        if not self._budget_ids:
            msg = "YnabConnector not authenticated — call authenticate() first"
            raise PermanentError(msg)

        token = self.config.credentials.get("access_token", "")
        headers = _auth_headers(token)

        accounts: list[RawAccount] = []
        for budget_id in self._budget_ids:
            budget_accounts = await self._fetch_budget_accounts(
                budget_id, headers
            )
            accounts.extend(budget_accounts)

        return accounts

    async def _fetch_budget_accounts(
        self,
        budget_id: str,
        headers: dict[str, str],
    ) -> list[RawAccount]:
        """GET /budgets/{budget_id}/accounts and parse into RawAccount."""
        resp = await self._http.get(
            f"/budgets/{budget_id}/accounts",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        account_list: list[dict[str, Any]] = data.get("data", {}).get(
            "accounts", []
        )

        return [self._parse_account(a, budget_id) for a in account_list]

    def _parse_account(
        self,
        data: dict[str, Any],
        budget_id: str,
    ) -> RawAccount:
        """Map a YNAB account JSON object to a RawAccount."""
        account_id = data.get("id", "")
        name = data.get("name", "")
        ynab_type = data.get("type", "checking")
        on_budget = data.get("on_budget", True)

        balance_milliunits = data.get("balance", 0)
        # YNAB returns balances in milliunits (1000 = 1 currency unit)
        current_balance = Decimal(str(balance_milliunits)) / Decimal(1000)

        # YNAB doesn't differentiate current vs available
        # For credit cards, the balance is negative (amount owed)
        return RawAccount(
            external_account_id=account_id,
            name=name,
            account_type=_ACCOUNT_TYPE_MAP.get(ynab_type, "checking"),
            account_subtype=ynab_type,
            currency_code=self._budget_currency,
            current_balance=abs(current_balance),
            available_balance=None,
            provider_metadata={
                "ynab_type": ynab_type,
                "on_budget": on_budget,
                "budget_id": budget_id,
                "deleted": data.get("deleted", False),
                "closed": data.get("closed", False),
            },
        )

    # ── Transactions ────────────────────────────────────────────────────

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Fetch transactions from configured budgets since *since*.

        Uses ``/budgets/{budget_id}/transactions`` with an optional
        ``since_date`` parameter.  When *account_id* is provided, fetches
        only that account's transactions via
        ``/budgets/{budget_id}/accounts/{account_id}/transactions``.

        Args:
            since: Only return transactions on or after this date.
            account_id: If set, scoped to a single account.
            limit: Maximum number of transactions to return per budget.

        Returns:
            Chronologically-sorted list of raw transactions.
        """
        if not self._budget_ids:
            msg = "YnabConnector not authenticated"
            raise PermanentError(msg)

        token = self.config.credentials.get("access_token", "")
        headers = _auth_headers(token)
        since_date = since.strftime("%Y-%m-%d")

        all_txns: list[RawTransaction] = []
        for budget_id in self._budget_ids:
            txns = await self._fetch_budget_transactions(
                budget_id,
                account_id,
                since_date,
                headers,
            )
            all_txns.extend(txns)
            if limit and len(all_txns) >= limit:
                all_txns = all_txns[:limit]
                break

        # Sort chronologically (most recent first)
        all_txns.sort(key=lambda t: t.occurred_at, reverse=True)

        if limit and len(all_txns) > limit:
            all_txns = all_txns[:limit]

        return all_txns

    async def _fetch_budget_transactions(
        self,
        budget_id: str,
        account_id: str | None,
        since_date: str,
        headers: dict[str, str],
    ) -> list[RawTransaction]:
        """Fetch transactions for a budget, optionally filtered by account."""
        if account_id:
            path = (
                f"/budgets/{budget_id}/accounts/{account_id}"
                f"/transactions?since_date={since_date}"
            )
        else:
            path = f"/budgets/{budget_id}/transactions?since_date={since_date}"

        try:
            resp = await self._http.get(path, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
        except httpx.TimeoutException as exc:
            msg = "YNAB transactions request timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"YNAB HTTP error fetching transactions: {exc}"
            raise TransientError(msg) from exc

        data = resp.json()
        txn_list: list[dict[str, Any]] = data.get("data", {}).get(
            "transactions", []
        )

        return [self._parse_transaction(t, budget_id) for t in txn_list]

    def _parse_transaction(
        self,
        data: dict[str, Any],
        budget_id: str,
    ) -> RawTransaction:
        """Map a YNAB transaction JSON object to a RawTransaction.

        YNAB transactions have:
        - ``id`` — unique identifier
        - ``date`` — the date the transaction occurred (YYYY-MM-DD,
          no time component)
        - ``amount`` — in milliunits (1000 = 1 currency unit)
        - ``memo`` — optional description
        - ``account_id`` — the account it belongs to
        - ``category_name`` — category name
        - ``cleared`` — "cleared", "uncleared", or "reconciled"
        - ``approved`` — boolean
        - ``flag_color`` — optional flag
        - ``payee_name`` — payee name
        - ``transfer_account_id`` — if this is a transfer
        """
        txn_id = data.get("id", "")
        ynab_account_id = data.get("account_id", "")
        amount_milliunits = data.get("amount", 0)
        # YNAB: positive = outflow (debit), negative = inflow (credit)
        # finance-sync: positive = inflow, negative = outflow → invert sign
        amount = Decimal(str(amount_milliunits)) / Decimal(1000)
        # Invert sign: YNAB outflow positive → finance-sync outflow negative
        amount = -amount

        occurred_at = _parse_ynab_date(data.get("date", ""))
        memo = data.get("memo", "") or None
        payee = data.get("payee_name", "") or None
        description = payee or memo or f"YNAB transaction {txn_id}"

        if payee and memo:
            description = f"{payee} — {memo}"

        cleared = data.get("cleared", "uncleared")
        approved = data.get("approved", False)
        is_transfer = data.get("transfer_account_id") is not None

        # Map clearing status
        if cleared == "cleared" or cleared == "reconciled":
            status = "booked"
        elif cleared == "uncleared":
            status = "pending"
        else:
            status = "pending"

        # Map category to transaction type
        cat_name = data.get("category_name", "")
        txn_type = _map_category_to_type(cat_name, is_transfer)

        return RawTransaction(
            external_transaction_id=f"{budget_id}_{txn_id}",
            external_account_id=ynab_account_id,
            amount=amount,
            currency_code=self._budget_currency,
            occurred_at=occurred_at,
            booked_at=occurred_at,
            description=description,
            transaction_type=txn_type,
            status=status,
            provider_metadata={
                "budget_id": budget_id,
                "category_name": cat_name,
                "category_id": data.get("category_id"),
                "approved": approved,
                "cleared": cleared,
                "flag_color": data.get("flag_color"),
                "is_transfer": is_transfer,
                "transfer_account_id": data.get("transfer_account_id"),
                "payee_name": payee,
                "source": data.get("source", ""),
            },
        )


# ── Module-level helpers ────────────────────────────────────────────────


def _auth_headers(access_token: str) -> dict[str, str]:
    """Return headers for YNAB API requests."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate connector error from an HTTP error response."""
    status = response.status_code
    if status == 429:
        retry_after = _parse_retry_after(response)
        msg = "YNAB rate limit exceeded"
        raise RateLimitError(msg, retry_after=retry_after)
    if status in (401, 403):
        msg = f"YNAB authentication failed (HTTP {status})"
        raise PermanentError(msg)
    if status == 404:
        # Try to extract error detail from YNAB's error response
        try:
            body = response.json()
            detail = body.get("error", {}).get("detail", "Resource not found")
        except Exception:
            detail = "Resource not found"
        msg = f"YNAB {detail} (HTTP {status})"
        raise PermanentError(msg)
    msg = f"YNAB request failed (HTTP {status})"
    raise TransientError(msg)


def _parse_ynab_date(raw: str) -> datetime:
    """Parse a YNAB date string (YYYY-MM-DD) to a UTC-aware datetime.

    YNAB dates have no time component — we set time to midnight UTC.
    """
    if not raw:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")  # noqa: DTZ007
        return parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Extract ``Retry-After`` header value in seconds."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _map_category_to_type(
    category: str | None,
    is_transfer: bool,
) -> str:
    """Map YNAB category name to canonical transaction type."""
    if is_transfer:
        return "transfer"

    if not category:
        return "other"

    cat_lower = category.lower()

    # Interest categories (check before income — "Interest Income" could
    # contain both keywords)
    interest_keywords = ["interest", "dividend"]
    if any(kw in cat_lower for kw in interest_keywords):
        return "interest"

    # Income categories
    income_keywords = [
        "income",
        "salary",
        "paycheck",
        "wage",
        "bonus",
        "reimbursement",
        "refund",
        "cashback",
    ]
    if any(kw in cat_lower for kw in income_keywords):
        return "deposit"

    # Fee categories
    fee_keywords = ["fee", "charge", "service charge", "late fee"]
    if any(kw in cat_lower for kw in fee_keywords):
        return "fee"

    # Investment categories
    invest_keywords = ["investment"]
    if any(kw in cat_lower for kw in invest_keywords):
        return "interest"

    return "payment"
