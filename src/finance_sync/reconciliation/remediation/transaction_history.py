"""Bounded transaction-history remediation extension point."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from finance_sync.reconciliation.remediation.verification import (
    VerificationResult,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


class TransactionHistoryStrategy:
    """Fetch and persist a gap through existing connector/persistence APIs.

    Persistence is injected deliberately: this strategy cannot invent an
    account mapping or bypass the canonical sync persistence boundary.
    """

    key = "transaction_history_gap"
    endpoint_family = "transaction_history"
    quota_cost = 1

    def __init__(
        self,
        session: AsyncSession,
        *,
        persist: Callable[[Any, str, list[Any]], Awaitable[int]],
        connector_factory: Callable[[Any], Awaitable[Any]] | None = None,
        verify: Callable[[Any], Awaitable[bool]] | None = None,
        supported_providers: frozenset[str] = frozenset(
            {
                "trading212",
                "bunq",
                "ynab",
                "degiro_pension",
                "saxo_investor",
                "csv_import",
                "manual_expense",
                "plaid_like",
            }
        ),
    ) -> None:
        self.session = session
        self.persist = persist
        self.connector_factory = connector_factory
        self.verify_callback = verify
        self.supported_providers = supported_providers

    def supports(self, item: Any) -> bool:
        """Allow execution only for providers with an explicit contract."""
        return str(item.provider_key).lower() in self.supported_providers

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            if self.connector_factory is None:
                message = "transaction remediation requires a connector"
                raise RuntimeError(message)
            connector = await self.connector_factory(item)
        context = _context(item)
        account_id = str(context["account_id"])
        since = _parse_datetime(context.get("from"))
        until = _parse_datetime(context.get("to")) or datetime.now(UTC)
        if since is None or since >= until:
            message = "transaction remediation requires a valid window"
            raise ValueError(message)
        max_days = min((until - since).days + 1, 31)
        since = until - timedelta(days=max_days)
        raw = await connector.fetch_transactions(
            since=since,
            account_id=str(context.get("provider_account_id") or account_id),
            limit=min(int(context.get("limit", 500)), 500),
        )
        canonical = connector.transform_transactions(raw)
        await self.persist(item, account_id, canonical)

    async def verify(self, item: Any) -> VerificationResult:
        if self.verify_callback is not None:
            resolved = await self.verify_callback(item)
        else:
            resolved = await self._verify_minimum_transactions(item)
        return VerificationResult(
            resolved,
            "transaction gap no longer detected"
            if resolved
            else "transaction gap remains",
        )

    async def execute_batch(
        self, items: list[Any], connector: Any = None
    ) -> dict[str, str]:
        """Coalesce compatible account windows into one bounded fetch."""
        if not items:
            return {}
        if connector is None:
            if self.connector_factory is None:
                return {str(item.id): "retry" for item in items}
            try:
                connector = await self.connector_factory(items[0])
            except Exception:
                return {str(item.id): "retry" for item in items}
        contexts = [_context(item) for item in items]
        account_ids = {
            str(context.get("account_id", "")) for context in contexts
        }
        provider_account_ids = {
            str(context.get("provider_account_id", "")) for context in contexts
        }
        windows = [
            (
                _parse_datetime(context.get("from")),
                _parse_datetime(context.get("to")),
            )
            for context in contexts
        ]
        if (
            len(account_ids) != 1
            or len(provider_account_ids) != 1
            or any(
                start is None or end is None or start >= end
                for start, end in windows
            )
        ):
            return await self._execute_batch_individually(items, connector)
        starts = [start for start, _ in windows if start is not None]
        ends = [end for _, end in windows if end is not None]
        earliest = min(starts)
        latest = max(ends)
        if (latest - earliest).days + 1 > 31:
            return await self._execute_batch_individually(items, connector)
        raw = await connector.fetch_transactions(
            since=earliest,
            account_id=next(iter(provider_account_ids)),
            limit=500,
        )
        canonical = connector.transform_transactions(raw)
        await self.persist(items[0], next(iter(account_ids)), canonical)
        return {str(item.id): "success" for item in items}

    async def _execute_batch_individually(
        self, items: list[Any], connector: Any
    ) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for item in items:
            try:
                await self.execute(item, connector)
            except Exception:
                outcomes[str(item.id)] = "retry"
            else:
                outcomes[str(item.id)] = "success"
        return outcomes

    async def _verify_minimum_transactions(self, item: Any) -> bool:
        """Require an explicit local count threshold before resolving.

        A provider response alone is not proof that a gap disappeared.  The
        caller must put ``minimum_transactions`` in the issue context; absent
        that contract, verification fails closed.
        """
        from sqlalchemy import exists, func, select

        from finance_sync.models.transaction import Transaction

        context = _context(item)
        minimum = context.get("minimum_transactions")
        if minimum is None:
            return False
        try:
            threshold = max(1, min(int(minimum), 500))
        except (TypeError, ValueError):
            return False
        since = _parse_datetime(context.get("from"))
        until = _parse_datetime(context.get("to")) or datetime.now(UTC)
        account_id = context.get("account_id")
        if since is None or not account_id or since >= until:
            return False
        from finance_sync.models.account import Account

        statement = select(func.count(Transaction.id)).where(
            Transaction.tenant_id == item.tenant_id,
            Transaction.account_id == str(account_id),
            Transaction.provider_key == str(item.provider_key),
            Transaction.occurred_at >= since,
            Transaction.occurred_at < until,
            Transaction.tombstoned_at.is_(None),
            exists(
                select(Account.id).where(
                    Account.id == str(account_id),
                    Account.tenant_id == item.tenant_id,
                )
            ),
        )
        connection_id = context.get("connection_id") or getattr(
            item, "connection_id", None
        )
        if connection_id:
            statement = statement.where(
                Transaction.connection_id == connection_id
            )
        count = await self.session.scalar(statement)
        return int(count or 0) >= threshold


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return (
            parsed.astimezone(UTC)
            if parsed.tzinfo
            else parsed.replace(tzinfo=UTC)
        )
    return None


def _context(item: Any) -> dict[str, Any]:
    """Return bounded issue metadata with a stable static type."""
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    )
