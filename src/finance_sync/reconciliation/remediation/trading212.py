"""Trading212 remediation strategy for unresolved instrument metadata."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.models.credential import Credential
from finance_sync.models.security import Security
from finance_sync.models.transaction import Transaction
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.reconciliation.remediation.verification import (
    VerificationResult,
)
from finance_sync.services.auth import decrypt_credential

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class Trading212InstrumentMetadataStrategy:
    key = "trading212_instrument_metadata"
    endpoint_family = "metadata"
    quota_cost = 1

    def supports(self, item: Any) -> bool:
        return (
            str(item.provider_key).lower() == "trading212"
            and str(item.remediation_strategy) == self.key
            and bool(item.affected_entity_id)
        )

    def __init__(self, session: AsyncSession, settings: Any) -> None:
        self.session = session
        self.settings = settings

    async def execute(self, item: Any, connector: Any = None) -> None:
        del connector
        row = await self.session.get(
            UnresolvedSecurity, item.affected_entity_id
        )
        if (
            row is not None
            and getattr(row, "tenant_id", item.tenant_id) != item.tenant_id
        ):
            row = None
        if row is None or row.resolved_security_id is not None:
            return
        credential = await self._credential(item, row)
        if credential is None:
            message = "active Trading212 connection not found"
            raise RuntimeError(message)
        payload = json.loads(
            decrypt_credential(
                credential.encrypted_payload, credential.nonce, self.settings
            )
        )
        options = json.loads(credential.description or "{}")
        options.pop("_label", None)
        instance = ConnectorRegistry().get_connector(
            ConnectorConfig(
                provider_type="trading212",
                credentials={str(k): str(v) for k, v in payload.items()},
                options=options,
            )
        )
        await instance.authenticate()
        instruments = await cast("Any", instance).fetch_instruments()
        wanted = str(row.external_security_id or row.raw_ticker or "").upper()
        match = next(
            (
                value
                for value in instruments
                if str(value.get("ticker") or value.get("symbol") or "").upper()
                == wanted
            ),
            None,
        )
        if match is None:
            return
        row.raw_isin = (
            str(match.get("isin") or match.get("ISIN") or "") or row.raw_isin
        )
        row.raw_ticker = (
            str(match.get("ticker") or match.get("symbol") or "")
            or row.raw_ticker
        )
        row.raw_name = (
            str(match.get("name") or match.get("shortName") or "")
            or row.raw_name
        )
        row.raw_currency_code = (
            str(match.get("currencyCode") or match.get("currency") or "")
            or row.raw_currency_code
        )
        row.raw_metadata = json.dumps(match, sort_keys=True, default=str)
        row.resolution_method = "trading212_metadata"
        if row.raw_isin:
            security = await self.session.scalar(
                select(Security).where(Security.isin == row.raw_isin.upper())
            )
            if security is not None:
                row.resolved_security_id = str(security.id)
                row.resolution_method = "auto_isin"
                await self._link_transactions(row, security)
        await self.session.flush()

    async def verify(self, item: Any) -> VerificationResult:
        row = await self.session.get(
            UnresolvedSecurity, item.affected_entity_id
        )
        if (
            row is not None
            and getattr(row, "tenant_id", item.tenant_id) != item.tenant_id
        ):
            return VerificationResult(
                False, "unresolved security is outside tenant scope"
            )
        resolved = row is None or row.resolved_security_id is not None
        return VerificationResult(
            resolved,
            "unresolved security resolved"
            if resolved
            else "unresolved security remains",
        )

    async def execute_batch(
        self, items: list[Any], connector: Any = None
    ) -> dict[str, str]:
        """Fetch one instrument master and map every compatible item."""
        del connector
        if not items:
            return {}
        first = await self.session.get(
            UnresolvedSecurity, items[0].affected_entity_id
        )
        if (
            first is not None
            and getattr(first, "tenant_id", items[0].tenant_id)
            != items[0].tenant_id
        ):
            first = None
        if first is None:
            return {str(item.id): "success" for item in items}
        credential = await self._credential(items[0], first)
        if credential is None:
            return {str(item.id): "retry" for item in items}
        payload = json.loads(
            decrypt_credential(
                credential.encrypted_payload, credential.nonce, self.settings
            )
        )
        options = json.loads(credential.description or "{}")
        options.pop("_label", None)
        instance = ConnectorRegistry().get_connector(
            ConnectorConfig(
                provider_type="trading212",
                credentials={str(k): str(v) for k, v in payload.items()},
                options=options,
            )
        )
        await instance.authenticate()
        instruments = await cast("Any", instance).fetch_instruments()
        by_key = {
            str(value.get("ticker") or value.get("symbol") or "").upper(): value
            for value in instruments
        }
        outcomes: dict[str, str] = {}
        for item in items:
            row = await self.session.get(
                UnresolvedSecurity, item.affected_entity_id
            )
            if (
                row is not None
                and getattr(row, "tenant_id", item.tenant_id) != item.tenant_id
            ):
                row = None
            wanted = (
                str(row.external_security_id or row.raw_ticker or "").upper()
                if row is not None
                else ""
            )
            match = by_key.get(wanted)
            if row is None or match is None:
                outcomes[str(item.id)] = "retry"
                continue
            row.raw_isin = (
                str(match.get("isin") or match.get("ISIN") or "")
                or row.raw_isin
            )
            row.raw_ticker = (
                str(match.get("ticker") or match.get("symbol") or "")
                or row.raw_ticker
            )
            row.raw_name = (
                str(match.get("name") or match.get("shortName") or "")
                or row.raw_name
            )
            row.raw_metadata = json.dumps(match, sort_keys=True, default=str)
            row.resolution_method = "trading212_metadata"
            if row.raw_isin:
                security = await self.session.scalar(
                    select(Security).where(
                        Security.isin == row.raw_isin.upper()
                    )
                )
                if security is not None:
                    row.resolved_security_id = str(security.id)
                    row.resolution_method = "auto_isin"
                    await self._link_transactions(row, security)
            outcomes[str(item.id)] = "success"
        await self.session.flush()
        return outcomes

    async def _link_transactions(
        self, row: UnresolvedSecurity, security: Security
    ) -> None:
        """Attach retained Trading212 activities to the resolved security."""
        ticker = str(row.raw_ticker or row.external_security_id or "").upper()
        if not ticker:
            return
        result = await self.session.execute(
            select(Transaction).where(
                Transaction.tenant_id == getattr(row, "tenant_id", None),
                Transaction.provider_key == "trading212",
                Transaction.security_id.is_(None),
                Transaction.provider_metadata["ticker"].as_string() == ticker,
            )
        )
        scalar_rows = result.scalars()
        transactions = (
            list(scalar_rows.all()) if hasattr(scalar_rows, "all") else []
        )
        for transaction in transactions:
            transaction.security_id = security.id

    async def _credential(
        self, item: Any, row: UnresolvedSecurity
    ) -> Credential | None:
        connection_id = item.connection_id
        stmt = select(Credential).where(
            Credential.tenant_id == item.tenant_id,
            Credential.provider_key == row.provider_key,
            Credential.status == "active",
        )
        if connection_id is not None:
            stmt = stmt.where(Credential.id == connection_id)
        return (await self.session.execute(stmt)).scalars().first()


class Trading212CostBasisStrategy:
    """Backfill acquisition activities for Wealthfolio cost-basis issues.

    Wealthfolio reports the symptom at holding level, while Trading212 owns
    the authoritative filled price.  One bounded history fetch repairs all
    affected holdings for the account and persists through the normal sync
    boundary; no price is inferred from market data.
    """

    key = "wealthfolio_cost_basis"
    endpoint_family = "trading212_transaction_history"
    quota_cost = 1

    def __init__(
        self, session: AsyncSession, *, persist: Any, settings: Any
    ) -> None:
        self.session = session
        self.persist = persist
        self.settings = settings
        self._processed: set[str] = set()

    def supports(self, item: Any) -> bool:
        context = _context(item)
        return (
            str(item.provider_key).lower() == "wealthfolio"
            and bool(context.get("account_id"))
            and not context.get("manual_review")
        )

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            message = "Trading212 cost-basis repair requires a connector"
            raise RuntimeError(message)
        context = _context(item)
        connection_key = str(
            context.get("connection_id") or context["account_id"]
        )
        if connection_key in self._processed:
            return
        until = datetime.now(UTC)
        since = until - timedelta(days=3650)
        raw = await connector.fetch_transactions(
            since=since,
            account_id=str(
                context.get("provider_account_id") or context["account_id"]
            ),
            limit=None,
        )
        canonical = connector.transform_transactions(raw)
        await self.persist(item, str(context["account_id"]), canonical)
        self._processed.add(connection_key)

    async def verify(
        self, item: Any, _connector: Any = None
    ) -> VerificationResult:
        context = _context(item)
        security_id = context.get("security_id")
        account_id = context.get("account_id")
        if not security_id or not account_id:
            return VerificationResult(
                False, "cost basis has no canonical account scope"
            )
        count = await self.session.scalar(
            select(Transaction.id)
            .where(
                Transaction.tenant_id == item.tenant_id,
                Transaction.account_id == str(account_id),
                Transaction.security_id == str(security_id),
                Transaction.transaction_type.in_(("purchase", "transfer")),
                Transaction.quantity > 0,
                Transaction.unit_price.is_not(None),
                Transaction.unit_price > 0,
            )
            .limit(1)
        )
        return VerificationResult(
            count is not None,
            "Trading212 acquisition price is present"
            if count is not None
            else "Trading212 acquisition price remains missing",
        )


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    )
