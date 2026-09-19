"""Bunq API connector implementation.

Uses bunq's v1 API with API-key authentication. When ``full_auth`` is enabled,
the connector performs installation, device registration and the signed
session flow required by the official sandbox. A lightweight session-only mode
remains available for existing configurations and recorded/static fixtures.

Rate limit
    bunq allows 60 requests per minute per user.  The connector's
    built-in :class:`~finance_sync.connectors.rate_limiter.RateLimiter`
    enforces this globally.

Pagination
    bunq uses cursor-based pagination via ``Pagination.future_url``.
    The connector follows next-page URLs transparently in
    ``fetch_accounts`` and ``fetch_transactions``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import httpx

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import (
    CategorySuggestion,
    ProviderMetadata,
    RawAccount,
    RawCardTransaction,
    RawScheduledPayment,
    RawTransaction,
    SourceReference,
)
from finance_sync.connectors.rate_limiter import RateLimitPolicy
from finance_sync.services.category_options import canonicalize_category

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.connectors.models import ConnectorConfig

_BUNQ_API_BASE = "https://api.bunq.com/v1"
_DEFAULT_COUNT = 200
logger = logging.getLogger(__name__)


# Bunq exposes the merchant category code (MCC), rather than a human-readable
# spending category, on payment objects.  Keep the translation at the source
# boundary so every exporter receives the same category suggestion.
_MCC_CATEGORIES: dict[str, str] = {
    "4111": "transportation",
    "4121": "transportation",
    "4131": "transportation",
    "4722": "travel",
    "4789": "transportation",
    "4900": "utilities",
    "5411": "groceries",
    "5422": "groceries",
    "5441": "groceries",
    "5451": "groceries",
    "5462": "groceries",
    "5499": "groceries",
    "5541": "transportation",
    "5542": "transportation",
    "5812": "food_and_dining",
    "5814": "food_and_dining",
    "5815": "entertainment",
    "5816": "entertainment",
    "5817": "entertainment",
    "5818": "entertainment",
    "5912": "health",
    "7011": "travel",
    "3000": "travel",
    "4511": "travel",
    "4729": "travel",
    "4784": "transportation",
    "5960": "personal_care",
    "7230": "personal_care",
    "7298": "personal_care",
    "7299": "personal_care",
    "7372": "software",
    "7379": "software",
    "7832": "entertainment",
    "7841": "entertainment",
    "7991": "entertainment",
    "7995": "entertainment",
    "8062": "health",
    "8099": "health",
    "8211": "education",
    "8220": "education",
    "8299": "education",
    "8398": "gifts_and_donations",
    "8661": "gifts_and_donations",
    "9311": "fees_and_charges",
    "9399": "fees_and_charges",
    "6011": "finance",
    "6300": "finance",
}

_MERCHANT_CATEGORIES: dict[str, str] = {
    "commandcode": "software",
    "openai": "software",
    "chatgpt": "software",
    "github": "software",
    "aws": "software",
    "google cloud": "software",
    "albert heijn": "groceries",
    "jumbo": "groceries",
    "lidl": "groceries",
    "ah to go": "groceries",
    "ns ": "transportation",
    "uber": "transportation",
    "bolt": "transportation",
    "booking.com": "travel",
    "airbnb": "travel",
    "school": "education",
    "university": "education",
    "belastingdienst": "fees_and_charges",
    "belasting": "fees_and_charges",
    "donation": "gifts_and_donations",
    "charity": "gifts_and_donations",
    "salaris": "employment",
    "salary": "employment",
    "payroll": "employment",
    "spotify": "entertainment",
    "netflix": "entertainment",
}


def _extract_mcc(data: dict[str, Any], merchant: dict[str, Any]) -> str | None:
    """Read the MCC names used by Bunq payment and card-payment payloads."""
    value = (
        data.get("merchant_category_code")
        or data.get("mcc")
        or merchant.get("merchant_category_code")
        or merchant.get("mcc")
    )
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _extract_bunq_category(data: dict[str, Any]) -> str | None:
    """Read an optional human category from provider-specific payloads.

    The public Payment schema documents MCC on the counterparty label, while
    some production payloads include the category in an additional or nested
    field. Accept both shapes without assuming the field is always present.
    """
    candidates: list[Any] = [
        data.get("category"),
        data.get("category_name"),
        data.get("payment_category"),
        data.get("additional_transaction_information"),
        data.get("additional_transaction_information_category"),
    ]
    for candidate in candidates:
        values = candidate if isinstance(candidate, list) else [candidate]
        for value in values:
            if isinstance(value, dict):
                value = (
                    value.get("category")
                    or value.get("name")
                    or value.get("value")
                )
            if value and not str(value).isdigit():
                return str(value).strip()
    return None


def _category_suggestion(
    mcc: str | None,
    *merchant_text: str | None,
    category: str | None = None,
) -> CategorySuggestion:
    """Create one stable, provider-provenanced Bunq category.

    Some Bunq ``Payment`` payloads omit MCC entirely (notably Mastercard
    payments from the monetary-account endpoint).  In that case use a
    conservative merchant rule and finally ``other_expenses`` so downstream
    apps do not show an unclassified transaction.
    """
    canonical_category = canonicalize_category(category)
    if canonical_category is not None:
        value = canonical_category
        source = "bunq_category"
        confidence = 0.95
    elif mcc is not None:
        value = canonicalize_category(
            _MCC_CATEGORIES.get(mcc, "other_expenses")
        ) or "other_expenses"
        source = "bunq_mcc"
        confidence = 0.75
    else:
        haystack = " ".join(text or "" for text in merchant_text).casefold()
        value = canonicalize_category(next(
            (
                category
                for merchant, category in _MERCHANT_CATEGORIES.items()
                if merchant in haystack
            ),
            "other_expenses",
        )) or "other_expenses"
        source = (
            "bunq_merchant_rule"
            if value != "other_expenses"
            else "bunq_fallback"
        )
        confidence = 0.65 if source == "bunq_merchant_rule" else 0.2
    return CategorySuggestion(
        value=value,
        source=source,
        confidence=confidence,
        taxonomy="personal",
    )


class BunqConnector(Connector):
    """Connector for the bunq banking API (v1).

    Credentials
        ``config.credentials[\"api_key\"]`` — bunq API key (required).
        ``config.options[\"base_url\"]`` — custom API base URL (optional,
        for sandbox/testing).

    Example::

        config = ConnectorConfig(
            provider_type=\"bunq\",
            credentials={\"api_key\": \"bunq_api_key_abc123\"},
            options={\"sandbox\": True},
        )
        conn = BunqConnector(config)
        await conn.authenticate()
        accounts = await conn.fetch_accounts()
    """

    display_name = "Bunq"
    sdk_version = "0.1.0"
    remediation_strategies = {
        "transaction_history_gap": {
            "endpoint_family": "transaction_history",
            "batch_limit": 1,
        }
    }
    capabilities = {
        "merchant_data": "partial",
        "mcc_category": "partial",
        "card_transactions": "partial",
        "refunds_chargebacks": "partial",
        "notes": "partial",
        "attachments": "detail_only",
        "scheduled_payments": "complete",
        "transfer_links": "partial",
    }
    metadata_capabilities = (
        "transaction_category_catalog",
        "merchant_category_code",
        "monetary_account_metadata",
    )

    # Keep a safety margin below Bunq's documented 60 requests/minute. The
    # limiter is shared by every request made by this connector, including
    # installation/session calls and pagination, not only sync wrappers.
    rate_limit_policy = RateLimitPolicy(
        max_requests=50,
        window_seconds=60,
        max_retries=3,
        backoff_base=1.0,
    )

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the bunq connector.

        Args:
            config: Connector configuration with credentials.
            http_client: Optional pre-configured HTTP client (for testing).
        """
        super().__init__(config)
        base_url = _BUNQ_API_BASE
        if "base_url" in config.options:
            base_url = config.options["base_url"]
        self._base_url = base_url
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
        )
        self._session_token: str | None = None
        self._user_id: int | None = None
        #: Opaque persisted connector state (bunq installation material).
        #: Injected by the orchestrator before a run and read back after.
        self._state: dict[str, Any] = {}
        self._category_catalog: list[dict[str, Any]] = []
        self._category_catalog_loaded = False
        # bunq requires the full installation flow (RSA key exchange →
        # /installation → /device-server → signed /session-server) for
        # every new API key; the legacy session-only path only works for
        # already-registered installations / static fixtures.  Default to
        # the full flow and make session-only an explicit opt-out.
        self._full_auth = bool(config.options.get("full_auth", True))

    @property
    def name(self) -> str:
        return "bunq"

    # ── Persistent state (bunq installation material) ─────────────────

    def set_state(self, state: dict[str, Any]) -> None:
        """Replace the persisted connector state (e.g. from a prior run)."""
        self._state = dict(state or {})
        raw_catalog = self._state.get("category_catalog")
        self._category_catalog = (
            [item for item in raw_catalog if isinstance(item, dict)]
            if isinstance(raw_catalog, list)
            else []
        )
        self._category_catalog_loaded = bool(self._category_catalog)

    def get_state(self) -> dict[str, Any]:
        """Return the connector state that should be persisted after a run."""
        state = dict(self._state)
        if self._category_catalog:
            state["category_catalog"] = list(self._category_catalog)
        return state

    # ── Authentication ──────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Create a bunq session-server using the configured API key.

        Raises:
            PermanentError: If the API key is missing or invalid.
            RateLimitError: If the bunq rate limit is exceeded.
            TransientError: On temporary provider unavailability.
        """
        api_key = self.config.credentials.get("api_key")
        if not api_key:
            msg = "bunq api_key is required in credentials"
            raise PermanentError(msg)

        try:
            if self._full_auth:
                session_data = await self._bootstrap_session(api_key)
            else:
                session_data = await self._create_session(api_key)
            self._session_token = session_data["token"]
            self._user_id = session_data["user_id"]
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
        except httpx.TimeoutException as exc:
            msg = "bunq session creation timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"bunq HTTP error during authenticate: {exc}"
            raise TransientError(msg) from exc

    async def _create_session(
        self,
        api_key: str,
    ) -> dict[str, Any]:
        """POST /session-server with the API key.

        Returns a dict with ``token`` and ``user_id``.
        """
        headers = _base_headers()
        body: dict[str, object] = {"secret": api_key}
        resp = await self._request(
            "POST",
            "/session-server", json=body, headers=headers
        )
        data = resp.json()

        session_token: str | None = None
        user_id: int | None = None

        for item in data.get("Response", []):
            if "Token" in item:
                session_token = item["Token"]["token"]
            if "UserPerson" in item:
                user_id = int(item["UserPerson"]["id"])
            if "UserCompany" in item:
                user_id = int(item["UserCompany"]["id"])

        if not session_token or not user_id:
            msg = "bunq session-server response missing token or user_id"
            raise PermanentError(msg)

        return {"token": session_token, "user_id": user_id}

    async def _bootstrap_session(self, api_key: str) -> dict[str, Any]:
        """Register an installation/device (once) and create a signed session.

        The installation material (client RSA private key + installation
        token) is persisted via :meth:`get_state` so subsequent syncs reuse
        the same device identity instead of registering a new device on every
        tick — bunq limits the number of devices per API key.  The state is
        only committed into ``self._state`` after the *entire* bootstrap
        succeeds, so a partially-failed install is retried from scratch.
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        try:
            private_key_pem = self._state.get("client_private_key_pem")
            installation_token = self._state.get("installation_token")
            if private_key_pem and installation_token:
                # Reuse the persisted installation — the device is already
                # registered under it, so only the signed session is needed.
                from cryptography.hazmat.primitives.asymmetric.rsa import (
                    RSAPrivateKey,
                )

                private_key = cast(
                    RSAPrivateKey,
                    serialization.load_pem_private_key(
                        private_key_pem.encode("ascii"),
                        password=None,
                    ),
                )
                fresh_installation = False
            else:
                private_key = rsa.generate_private_key(
                    public_exponent=65537, key_size=2048
                )
                public_key = private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                installation = await self._request(
                    "POST",
                    "/installation",
                    json={"client_public_key": public_key.decode("ascii")},
                    headers=_base_headers(),
                )
                installation_token = _bunq_token(installation.json())
                fresh_installation = True

            async def signed_post(path: str, body: dict[str, object]) -> Any:
                payload = json.dumps(body, separators=(",", ":")).encode()
                signature = private_key.sign(
                    payload,
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
                headers = _base_headers()
                headers.update(
                    {
                        "Content-Type": "application/json",
                        "X-Bunq-Client-Authentication": installation_token,
                        "X-Bunq-Client-Signature": base64.b64encode(
                            signature
                        ).decode("ascii"),
                    }
                )
                response = await self._request(
                    "POST",
                    path, content=payload, headers=headers
                )
                return response.json()

            if fresh_installation:
                await signed_post(
                    "/device-server",
                    {
                        "description": "finance-sync",
                        "secret": api_key,
                        "permitted_ips": _normalise_permitted_ips(
                            self.config.options.get("permitted_ips", [])
                        ),
                    },
                )
            session = await signed_post("/session-server", {"secret": api_key})

            if fresh_installation:
                # Commit the installation material only after the whole
                # bootstrap succeeded (device registered, session created).
                self._state["client_private_key_pem"] = (
                    private_key.private_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PrivateFormat.PKCS8,
                        encryption_algorithm=serialization.NoEncryption(),
                    ).decode("ascii")
                )
                self._state["installation_token"] = installation_token
            return _bunq_session(session)
        except (PermanentError, httpx.HTTPStatusError):
            # A rejected installation is not worth retrying: clear any
            # stale state so the next run registers a fresh one.  (The
            # HTTPStatusError is converted to PermanentError by
            # ``authenticate()``.)
            self._state = {}
            raise

    def _auth_headers(self) -> dict[str, str]:
        """Return request headers with the session token."""
        if not self._session_token:
            msg = "BunqConnector not authenticated — call authenticate() first"
            raise PermanentError(msg)

        headers = _base_headers()
        headers["X-Bunq-Client-Authentication"] = self._session_token
        return headers

    # ── Accounts ────────────────────────────────────────────────────────

    async def fetch_accounts(self) -> list[RawAccount]:
        """Fetch bank, savings and joint accounts via paginated API."""
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)

        accounts: list[RawAccount] = []
        accounts.extend(
            await self._fetch_monetary_accounts("MonetaryAccountBank")
        )
        accounts.extend(
            await self._fetch_monetary_accounts("MonetaryAccountSavings")
        )
        # Joint accounts are exposed by bunq through their own collection;
        # they are not included reliably in the generic monetary-account
        # response.  Without this call shared accounts remain invisible.
        accounts.extend(
            await self._fetch_monetary_accounts(
                "MonetaryAccountJoint", endpoint="monetary-account-joint"
            )
        )
        return accounts

    async def _fetch_monetary_accounts(
        self,
        account_type: str,
        *,
        endpoint: str = "monetary-account",
    ) -> list[RawAccount]:
        """Fetch monetary accounts of a given type, handling pagination."""
        items: list[RawAccount] = []
        url = f"/user/{self._user_id}/{endpoint}?count={_DEFAULT_COUNT}"
        seen_urls: set[str] = set()

        while url:
            if not self._mark_pagination_url(url, seen_urls):
                break
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                account_data = entry.get(account_type)
                if account_data is None:
                    continue
                items.append(self._parse_account(account_data, account_type))
            url = self._next_page_url(data)

        return items

    @staticmethod
    def _parse_account(
        data: dict[str, Any],
        bunq_type: str,
    ) -> RawAccount:
        """Map a bunq monetary-account JSON object to a RawAccount."""
        account_id = str(data["id"])
        description = data.get("description") or data.get("name", "")

        if bunq_type == "MonetaryAccountSavings":
            acct_type = "savings"
        elif bunq_type == "MonetaryAccountBank":
            acct_type = "checking"
        elif bunq_type == "MonetaryAccountJoint":
            acct_type = "joint"
        else:
            acct_type = "other"

        balance_data = data.get("balance", {})
        current_balance = (
            Decimal(balance_data["value"])
            if balance_data.get("value")
            else None
        )
        currency = balance_data.get("currency", "EUR")

        iban: str | None = None
        for alias in data.get("alias", []):
            if alias.get("type") == "IBAN":
                iban = alias.get("value")
                break

        return RawAccount(
            external_account_id=account_id,
            name=description,
            account_type=acct_type,
            account_subtype=None,
            currency_code=currency,
            current_balance=current_balance,
            available_balance=None,
            iso_currency_code=currency,
            provider_metadata={
                "bunq_type": bunq_type,
                "iban": iban,
                "status": data.get("status"),
                "sub_type": data.get("sub_type"),
            },
        )

    async def fetch_categories(self) -> list[dict[str, Any]]:
        """Fetch bunq's account-specific transaction category catalog.

        Categories are auxiliary metadata: a missing/unsupported endpoint must
        not make an otherwise valid payment sync fail. The raw catalog is
        persisted in connector state for diagnostics and future classification
        rules, while transaction rows keep only the safe category fields.
        """
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)
        if self._category_catalog_loaded:
            return list(self._category_catalog)
        try:
            response = await self._request(
                "GET",
                f"/user/{self._user_id}/additional-transaction-information-category",
                headers=self._auth_headers(),
            )
            payload = response.json()
        except (PermanentError, TransientError) as exc:
            self._category_catalog_loaded = True
            logger.warning("bunq_category_catalog_unavailable: %s", str(exc))
            return list(self._category_catalog)

        raw_items = (
            payload.get("Response", payload)
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(raw_items, list):
            self._category_catalog_loaded = True
            return list(self._category_catalog)
        catalog: list[dict[str, Any]] = []
        for item in raw_items:
            value = item
            if isinstance(item, dict) and len(item) == 1:
                value = next(iter(item.values()))
            if not isinstance(value, dict):
                continue
            category = value.get("category")
            if category:
                catalog.append(
                    {
                        "category": str(category),
                        "type": value.get("type"),
                        "status": value.get("status"),
                        "description": value.get("description"),
                        "description_translated": value.get(
                            "description_translated"
                        ),
                        "order": value.get("order"),
                    }
                )
        self._category_catalog = catalog
        self._category_catalog_loaded = True
        logger.info("bunq_category_catalog_loaded: %s", len(catalog))
        return list(catalog)

    # ── Transactions ────────────────────────────────────────────────────

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Fetch payments for one or all accounts.

        When *account_id* is provided, only fetches for that account.
        Otherwise fetches for every known monetary account.
        """
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)

        await self.fetch_categories()

        if account_id:
            account_ids: Sequence[str] = [account_id]
        else:
            raw_accounts = await self.fetch_accounts()
            account_ids = [a.external_account_id for a in raw_accounts]

        all_txns: list[RawTransaction] = []
        for aid in account_ids:
            txns = await self._fetch_account_payments(aid, since, limit)
            all_txns.extend(txns)
            if limit and len(all_txns) >= limit:
                all_txns = all_txns[:limit]
                break

        return all_txns

    async def _fetch_account_payments(
        self,
        account_id: str,
        since: datetime,
        limit: int | None,
    ) -> list[RawTransaction]:
        """Fetch payments for a single monetary account with pagination.

        Filters out transactions older than *since* client-side to
        support bunq's server-side date-range limitations.
        """
        items: list[RawTransaction] = []
        url = (
            f"/user/{self._user_id}/monetary-account/{account_id}/payment"
            f"?count={_DEFAULT_COUNT}"
        )
        seen_urls: set[str] = set()

        while url:
            if not self._mark_pagination_url(url, seen_urls):
                break
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                payment = entry.get("Payment")
                if payment is None:
                    continue
                txn = self._parse_payment(payment, account_id)
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items
            url = self._next_page_url(data)

        return items

    @staticmethod
    def _parse_payment(
        data: dict[str, Any],
        account_id: str,
    ) -> RawTransaction:
        """Map a bunq Payment JSON object to a RawTransaction."""
        payment_id = str(data["id"])
        amount_data: dict[str, Any] = data.get("amount", {})
        amount = Decimal(amount_data.get("value", "0"))
        currency = amount_data.get("currency", "EUR")

        created = _parse_bunq_datetime(data.get("created", ""))
        updated = _parse_bunq_datetime(data.get("updated", ""))

        description = data.get("description", "") or None
        payment_type = data.get("type", "")
        status_raw = data.get("status", "")

        counterparty: dict[str, Any] = data.get("counterparty_alias") or {}
        counterparty_iban = counterparty.get("value", "")
        merchant: dict[str, Any] = data.get("merchant") or {}
        mcc = _extract_mcc(data, merchant)
        bunq_category = _extract_bunq_category(data)
        category_suggestion = _category_suggestion(
            mcc,
            description,
            data.get("merchant_name"),
            merchant.get("name"),
            category=bunq_category,
        )

        attachments = data.get("attachment", [])
        source_references = [
            SourceReference(
                object_type="payment",
                external_ids=[payment_id],
                provider_revisions=[str(data.get("updated", ""))],
            )
        ]
        for attachment in attachments:
            attachment_id = str(
                attachment.get("id") or attachment.get("attachment_id") or ""
            )
            if attachment_id:
                source_references.append(
                    SourceReference(
                        object_type="attachment",
                        external_ids=[attachment_id],
                        provider_revisions=[str(data.get("updated", ""))],
                    )
                )
        refund_data = data.get("refund") or data.get("refund_amount")
        if isinstance(refund_data, dict):
            refund_details = cast("dict[str, Any]", refund_data)
            refund_amount = Decimal(str(refund_details.get("value", "0")))
            refund_currency = str(refund_details.get("currency", currency))
        elif refund_data is not None:
            refund_amount = Decimal(str(refund_data))
            refund_currency = currency
        else:
            refund_amount = None
            refund_currency = None

        # Keep the small, destination-relevant Bunq extensions in a stable
        # contract.  The complete response is represented by
        # ``source_record_hash`` for change detection; sensitive/provider-
        # specific fields are deliberately not copied wholesale.
        note = data.get("note") or data.get("notes")
        note_text = note if isinstance(note, str) else None
        contract_fields = {
            "type": payment_type,
            "status": status_raw,
            "created": data.get("created"),
            "updated": data.get("updated"),
            "sub_type": data.get("sub_type"),
            "counterparty_alias_type": counterparty.get("type"),
            "merchant_id": merchant.get("id"),
            "mcc": mcc,
            "bunq_category": bunq_category,
            "category": category_suggestion.value
            if category_suggestion is not None
            else None,
            "attachment_count": len(attachments),
            "note_present": bool(note_text),
        }
        contract_fields = {
            key: value
            for key, value in contract_fields.items()
            if value is not None
        }

        return RawTransaction(
            external_transaction_id=payment_id,
            external_account_id=account_id,
            amount=amount,
            currency_code=currency,
            occurred_at=created,
            booked_at=updated or created,
            description=description,
            transaction_type=_map_transaction_type(payment_type),
            # Monetary-account Payment responses from bunq commonly omit a
            # status for already-settled rows (notably Mastercard payments).
            # They are not pending merely because the optional field is
            # absent; explicit PENDING/REJECTED/etc. values still win.
            status=_map_status(status_raw or "ACCEPTED"),
            original_type=str(payment_type) or None,
            original_status=str(status_raw) or None,
            merchant_name=(
                data.get("merchant_name") or merchant.get("name") or None
            ),
            merchant_city=data.get("merchant_city") or merchant.get("city"),
            merchant_country=(
                data.get("merchant_country") or merchant.get("country")
            ),
            merchant_category_code=(
                mcc
            ),
            counterparty_name=counterparty.get("name") or None,
            counterparty_account_reference=counterparty_iban or None,
            source_record_hash=hashlib.sha256(
                json.dumps(data, sort_keys=True, default=str).encode()
            ).hexdigest(),
            source_references=source_references,
            refund_amount=refund_amount,
            refund_currency_code=refund_currency,
            cashflow_suggestion=category_suggestion,
            classification_source=(
                category_suggestion.source
                if category_suggestion is not None
                else None
            ),
            provider_metadata_contract=ProviderMetadata(
                schema_version="bunq-payment-v1",
                source_object_type="Payment",
                fields=contract_fields,
            ),
            provider_metadata={
                "payment_type": payment_type,
                "counterparty_iban": counterparty_iban,
                "attachment_count": len(attachments),
                "sub_type": data.get("sub_type"),
                "note": note_text,
                "counterparty_alias_type": counterparty.get("type"),
                "merchant_id": merchant.get("id"),
                "mcc_raw": mcc,
                "bunq_category_raw": bunq_category,
                "category": category_suggestion.value
                if category_suggestion is not None
                else None,
            },
        )

    # ── Scheduled / recurring payments ─────────────────────────────────

    async def fetch_scheduled_payments(
        self,
        *,
        account_id: str | None = None,
    ) -> list[RawScheduledPayment]:
        """Fetch scheduled / recurring payments for one or all accounts.

        Uses the bunq ``/schedule-payment`` endpoint which returns payment
        templates with recurrence rules.

        When *account_id* is provided, only fetches for that account.
        Otherwise fetches for every known monetary account.
        """
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)

        if account_id:
            account_ids: Sequence[str] = [account_id]
        else:
            raw_accounts = await self.fetch_accounts()
            account_ids = [a.external_account_id for a in raw_accounts]

        all_schedules: list[RawScheduledPayment] = []
        for aid in account_ids:
            schedules = await self._fetch_account_schedule_payments(aid)
            all_schedules.extend(schedules)

        return all_schedules

    async def _fetch_account_schedule_payments(
        self,
        account_id: str,
    ) -> list[RawScheduledPayment]:
        """Fetch schedule-payment entries for a single monetary account."""
        items: list[RawScheduledPayment] = []
        url = (
            f"/user/{self._user_id}/monetary-account/{account_id}"
            f"/schedule-payment"
            f"?count={_DEFAULT_COUNT}"
        )
        seen_urls: set[str] = set()

        while url:
            if not self._mark_pagination_url(url, seen_urls):
                break
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                schedule = entry.get("SchedulePayment")
                if schedule is None:
                    continue
                items.append(self._parse_schedule_payment(schedule, account_id))
            url = self._next_page_url(data)

        return items

    @staticmethod
    def _parse_schedule_payment(
        data: dict[str, Any],
        account_id: str,
    ) -> RawScheduledPayment:
        """Map a bunq SchedulePayment JSON object to a RawScheduledPayment."""
        schedule_id = str(data.get("id", ""))

        # The payment template inside the schedule
        payment_data: dict[str, Any] = data.get("payment", {})
        amount_data = payment_data.get("amount", {})
        amount = Decimal(amount_data.get("value", "0"))
        currency = amount_data.get("currency", "EUR")
        description = payment_data.get("description", "") or None

        # Counterparty info
        counterparty: dict[str, Any] = (
            payment_data.get("counterparty_alias") or {}
        )
        counterparty_iban = counterparty.get("value", "")
        counterparty_name = counterparty.get("name", "")

        # Schedule recurrence info
        schedule_data: dict[str, Any] = data.get("schedule", {})
        schedule_time_unit = schedule_data.get("time_unit", "")
        raw_frequency = schedule_data.get("interval", 1)
        schedule_start_raw: dict[str, Any] = schedule_data.get("start") or {}
        schedule_end_raw: dict[str, Any] = schedule_data.get("end") or {}
        schedule_start = _parse_bunq_datetime(
            schedule_start_raw.get("value", "")
        )
        schedule_end = _parse_bunq_datetime(schedule_end_raw.get("value", ""))
        schedule_status = schedule_data.get("status", "")

        # Count executions from the schedule
        schedule_instances = data.get("schedule_instance", [])
        execution_count = len(schedule_instances) if schedule_instances else 0

        return RawScheduledPayment(
            external_schedule_id=schedule_id,
            external_account_id=account_id,
            amount=amount,
            currency_code=currency,
            frequency=schedule_time_unit,
            interval=raw_frequency,
            next_execution_date=(
                _parse_bunq_datetime(
                    cast(
                        "dict[str, Any]",
                        schedule_data.get("next_execution") or {},
                    ).get("value", "")
                )
                or schedule_start
            ),
            end_date=schedule_end if schedule_end.timestamp() > 0 else None,
            execution_count=execution_count,
            counterparty_name=counterparty_name or None,
            counterparty_iban=counterparty_iban or None,
            description=description,
            status=_map_schedule_status(schedule_status),
            provider_metadata={
                "schedule_type": schedule_data.get("type"),
                "schedule_status": schedule_status,
                "schedule_start": schedule_start.isoformat()
                if schedule_start.timestamp() > 0
                else None,
                "schedule_end": schedule_end.isoformat()
                if schedule_end and schedule_end.timestamp() > 0
                else None,
                "payment_id": str(payment_data.get("id", "")),
            },
        )

    # ── Card transactions ──────────────────────────────────────────────

    async def fetch_card_transactions(
        self,
        since: datetime,
        *,
        limit: int | None = None,
    ) -> list[RawCardTransaction]:
        """Fetch card transactions for all known cards.

        Bunq exposes card payments via ``/card/{card_id}/card-payment``.
        This method first fetches all active cards for the user, then
        fetches payments for each card.

        Args:
            since: Only return transactions on or after this time.
            limit: Maximum number of transactions to return.

        Returns:
            A chronologically-sorted list of raw card transactions.
        """
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)

        cards = await self._fetch_cards()
        all_txns: list[RawCardTransaction] = []

        for card in cards:
            card_id = card["id"]
            txns = await self._fetch_card_payments(card_id, since, limit)
            all_txns.extend(txns)
            if limit and len(all_txns) >= limit:
                all_txns = all_txns[:limit]
                break

        # Sort chronologically (most recent first)
        all_txns.sort(key=lambda t: t.occurred_at, reverse=True)
        return all_txns

    async def _fetch_cards(self) -> list[dict[str, Any]]:
        """Fetch all cards for the authenticated user."""
        items: list[dict[str, Any]] = []
        url = f"/user/{self._user_id}/card?count={_DEFAULT_COUNT}"
        seen_urls: set[str] = set()

        while url:
            if not self._mark_pagination_url(url, seen_urls):
                break
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                card_data = entry.get("Card")
                if card_data is None:
                    continue
                items.append(
                    {
                        "id": str(card_data["id"]),
                        "type": card_data.get("type", ""),
                        "status": card_data.get("status", ""),
                        "name": card_data.get("name", ""),
                        "last_four": card_data.get("last_four", ""),
                    }
                )
            url = self._next_page_url(data)

        return items

    async def _fetch_card_payments(
        self,
        card_id: str,
        since: datetime,
        limit: int | None,
    ) -> list[RawCardTransaction]:
        """Fetch card-payment entries for a single card with pagination.

        Filters out transactions older than *since* client-side.
        """
        items: list[RawCardTransaction] = []
        url = f"/card/{card_id}/card-payment?count={_DEFAULT_COUNT}"
        seen_urls: set[str] = set()

        while url:
            if not self._mark_pagination_url(url, seen_urls):
                break
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                payment = entry.get("CardPayment")
                if payment is None:
                    continue
                txn = self._parse_card_payment(payment, card_id)
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items
            url = self._next_page_url(data)

        return items

    @staticmethod
    def _parse_card_payment(
        data: dict[str, Any],
        card_id: str,
    ) -> RawCardTransaction:
        """Map a bunq CardPayment JSON object to a RawCardTransaction."""
        payment_id = str(data.get("id", ""))
        amount_data: dict[str, Any] = data.get("amount", {})
        amount = Decimal(amount_data.get("value", "0"))
        currency = amount_data.get("currency", "EUR")

        created = _parse_bunq_datetime(data.get("created", ""))
        updated = _parse_bunq_datetime(data.get("updated", ""))

        merchant_name = data.get("merchant_name") or (
            data.get("merchant", {}).get("name")
        )
        merchant_city = data.get("merchant_city") or (
            data.get("merchant", {}).get("city")
        )
        merchant_country = data.get("merchant_country") or data.get(
            "merchant", {}
        ).get("country")
        merchant = data.get("merchant") or {}
        mcc = _extract_mcc(data, merchant)
        bunq_category = _extract_bunq_category(data)
        category_suggestion = _category_suggestion(
            mcc,
            data.get("description"),
            data.get("merchant_name"),
            merchant.get("name"),
            category=bunq_category,
        )

        card_data: dict[str, Any] = data.get("card", {}) or {}
        card_uuid = str(card_data.get("id", "")) or str(data.get("card_id", ""))
        card_type = data.get("card_type") or card_data.get("type", "")

        auth_status = data.get("authorisation_status", data.get("status", ""))
        description = data.get("description", "") or None
        status_raw = str(data.get("status", auth_status) or "")
        source_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()

        return RawCardTransaction(
            external_card_transaction_id=payment_id,
            external_account_id=card_id,
            amount=amount,
            currency_code=currency,
            merchant_name=merchant_name or None,
            merchant_city=merchant_city or None,
            merchant_country=merchant_country or None,
            mcc=str(mcc) if mcc is not None else None,
            card_id=card_uuid or card_id,
            card_type=card_type or None,
            card_last_four=data.get("card_last_four") or None,
            occurred_at=created,
            booked_at=updated or created,
            authorization_type=_map_auth_status(auth_status),
            description=description,
            status=_map_card_payment_status(auth_status),
            provider_metadata_contract=ProviderMetadata(
                schema_version="bunq-card-payment-v1",
                source_object_type="CardPayment",
                fields={"status": status_raw, "authorization": auth_status},
            ),
            merchant_category_code=str(mcc) if mcc is not None else None,
            original_status=status_raw or None,
            authorization_status=str(auth_status) or None,
            settlement_status=(
                str(data.get("settlement_status"))
                if data.get("settlement_status") is not None
                else None
            ),
            source_record_hash=source_hash,
            cashflow_suggestion=category_suggestion,
            classification_source=(
                category_suggestion.source
                if category_suggestion is not None
                else None
            ),
            refund_amount=(
                Decimal(str(data["refund_amount"]))
                if data.get("refund_amount") is not None
                else None
            ),
            refund_currency_code=(
                str(data.get("refund_currency", currency))
                if data.get("refund_amount") is not None
                else None
            ),
            source_references=[
                SourceReference(
                    object_type="card_payment",
                    external_ids=[payment_id],
                    provider_revisions=[str(data.get("updated", ""))],
                )
            ],
            provider_metadata={
                "auth_status": auth_status,
                "original_card_id": card_id,
                "card_uuid": card_uuid,
                "mcc_raw": mcc,
                "bunq_category_raw": bunq_category,
                "merchant_raw": data.get("merchant", {}),
            },
        )

    # ── Pagination helper ───────────────────────────────────────────────

    @staticmethod
    def _mark_pagination_url(url: str, seen_urls: set[str]) -> bool:
        """Return false when bunq returns a pagination URL we've seen.

        A provider response with a repeated ``future_url`` must not make a
        sync loop forever.  This has occurred in production when bunq
        returned a cursor that did not advance; stopping at the repeated
        cursor preserves the data already received and lets the sync report
        a bounded result instead of exhausting the API rate limit.
        """
        if url in seen_urls:
            logger.warning("bunq_pagination_cycle_detected")
            return False
        seen_urls.add(url)
        return True

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform one Bunq request through the shared throttle/retry path.

        This is intentionally lower-level than the base connector fetch
        wrappers: Bunq's installation, session, pagination and card calls
        all count against the same per-user quota.
        """

        async def request_once() -> object:
            try:
                response = await self._http.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc.response)
                raise  # pragma: no cover - _raise_for_status always raises
            except httpx.TimeoutException as exc:
                msg = "bunq request timed out"
                raise TransientError(msg) from exc
            except httpx.HTTPError as exc:
                msg = f"bunq HTTP error: {exc}"
                raise TransientError(msg) from exc

        if self._rate_limiter is None:
            return cast("httpx.Response", await request_once())
        response = await self._rate_limiter.retry(request_once)
        return cast("httpx.Response", response)

    async def _request_paginated(
        self,
        url: str,
    ) -> dict[str, Any]:
        """Make a paginated API request with auth headers.

        Handles rate-limit and auth-expired responses.
        """
        headers = self._auth_headers()

        response = await self._request("GET", url, headers=headers)
        return response.json()

    def _next_page_url(self, data: dict[str, Any]) -> str | None:
        """Extract the next-page URL from a paginated response.

        Uses the connector's configured base URL so custom endpoints
        (bunq sandbox or another explicitly configured endpoint) page against
        themselves instead of jumping back to the production API.
        """
        pagination = data.get("Pagination") or data.get("PaginatedResponse")
        if not pagination:
            return None
        future_url = pagination.get("future_url")
        if not future_url:
            return None
        future_url = str(future_url)
        if future_url.startswith(("http://", "https://")):
            return future_url
        # bunq returns future_url as an absolute path (/v1/...).
        # Strip the leading /v1 since the base URL already includes it.
        return f"{self._base_url}{future_url.removeprefix('/v1')}"


# ── Module-level helpers ────────────────────────────────────────────────


def _base_headers() -> dict[str, str]:
    """Return headers common to all bunq API requests.

    Note: the X-Bunq-Region header is deliberately NOT sent — bunq's API
    rejects it with HTTP 400 ("Your device's region setting are not
    supported by bunq") on both sandbox and production endpoints while the
    same request without it succeeds (verified live 2026-08-17).
    """
    return {
        "X-Bunq-Client-Request-Id": _request_id(),
        "X-Bunq-Geolocation": "0 0 0 0 NL",
        "X-Bunq-Language": "en_US",
        "Cache-Control": "no-cache",
        "User-Agent": "finance-sync/0.1",
    }


def _normalise_permitted_ips(raw: object) -> list[str]:
    """Normalise the ``permitted_ips`` option to a list of IP strings.

    Accepts a list (API / programmatic config) or a comma-separated
    string (dashboard text input).
    """
    if isinstance(raw, str):
        return [ip.strip() for ip in raw.split(",") if ip.strip()]
    if isinstance(raw, (list, tuple)):
        return [
            str(ip).strip()
            for ip in cast("Sequence[str]", raw)
            if str(ip).strip()
        ]
    return []


def _bunq_token(data: dict[str, Any]) -> str:
    """Extract a token from a bunq response envelope."""
    for item in data.get("Response", []):
        if "Token" in item and item["Token"].get("token"):
            return str(item["Token"]["token"])
    msg = "bunq installation response missing token"
    raise PermanentError(msg)


def _bunq_session(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the session token and user id from a response envelope."""
    token: str | None = None
    user_id: int | None = None
    for item in data.get("Response", []):
        if "Token" in item:
            token = item["Token"].get("token")
        user = item.get("UserPerson") or item.get("UserCompany")
        if user:
            user_id = int(user["id"])
    if not token or not user_id:
        msg = "bunq session-server response missing token or user_id"
        raise PermanentError(msg)
    return {"token": token, "user_id": user_id}


def _request_id() -> str:
    """Return a unique client-request-id per call."""
    import uuid

    return uuid.uuid4().hex[:16]


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate connector error from an HTTP error response."""
    status = response.status_code
    if status == 429:
        retry_after = _parse_retry_after(response)
        msg = "bunq rate limit exceeded"
        raise RateLimitError(msg, retry_after=retry_after)
    if status in (401, 403):
        msg = f"bunq authentication failed (HTTP {status})"
        raise PermanentError(msg)
    msg = f"bunq request failed (HTTP {status})"
    raise TransientError(msg)


def _parse_bunq_datetime(raw: str) -> datetime:
    """Parse a bunq timestamp to a UTC-aware datetime.

    Bunq formats::

        "2025-06-01 12:30:00.123456"
        "2025-06-01 12:30:00"
    """
    if not raw:
        return datetime.fromtimestamp(0, tz=UTC)

    if "." in raw:
        main, frac = raw.split(".", 1)
        frac = frac[:6]
        cleaned = f"{main}.{frac}"
    else:
        cleaned = raw

    # bunq returns naive UTC timestamps — parse and attach UTC
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        return datetime.fromtimestamp(0, tz=UTC)

    return parsed.replace(tzinfo=UTC)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Extract ``Retry-After`` header value in seconds."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _map_transaction_type(bunq_type: str) -> str:
    """Map bunq payment type string to canonical transaction type."""
    mapping = {
        "BILLING": "payment",
        "PAYMENT": "payment",
        "TRANSFER": "transfer",
        "WITHDRAWAL": "withdrawal",
        "DEPOSIT": "deposit",
        "INTEREST": "interest",
        "FEE": "fee",
        "DIRECT_DEBIT": "payment",
        "SCT": "transfer",
        "SDD": "payment",
        "BUNQME": "payment",
        "REQUEST": "payment",
    }
    return mapping.get(bunq_type.upper(), "other")


def _map_status(raw: str) -> str:
    """Map bunq payment status to canonical status."""
    mapping = {
        "ACCEPTED": "booked",
        "PENDING": "pending",
        "REJECTED": "cancelled",
        "CANCELLED": "cancelled",
        "REVERSED": "reversed",
    }
    return mapping.get(raw.upper(), "pending")


def _map_schedule_status(raw: str) -> str:
    """Map bunq schedule status to canonical schedule status."""
    mapping = {
        "ACTIVE": "active",
        "INACTIVE": "paused",
        "CANCELLED": "cancelled",
        "COMPLETED": "completed",
        "FAILED": "failed",
    }
    return mapping.get(raw.upper(), "active")


def _map_auth_status(raw: str) -> str:
    """Map bunq card authorisation status to canonical auth type."""
    mapping = {
        "AUTHORISATION": "authorization",
        "AUTHORIZATION": "authorization",
        "SETTLEMENT": "settlement",
        "REFUND": "refund",
        "CHARGEBACK": "chargeback",
        "CANCELLED": "other",
        "REVERSED": "chargeback",
    }
    return mapping.get(raw.upper(), "authorization")


def _map_card_payment_status(raw: str) -> str:
    """Map bunq card payment status to canonical transaction status."""
    mapping = {
        "AUTHORISATION": "pending",
        "AUTHORIZATION": "pending",
        "SETTLEMENT": "booked",
        "REFUND": "booked",
        "CHARGEBACK": "booked",
        "COMPLETED": "booked",
        "CANCELLED": "cancelled",
        "REVERSED": "reversed",
    }
    return mapping.get(raw.upper(), "pending")
