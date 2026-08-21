"""Account and account-scoped transaction read component."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from finance_sync.models.account import Account
from finance_sync.models.transaction import Transaction
from finance_sync.services.read.pagination import expression, sort_field

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.visibility import ReadScope


class AccountReadService:
    """Read accounts and account-scoped transactions for one session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        scope: ReadScope | None = None,
    ) -> None:
        self._session = session
        self._scope = scope

    def _account_condition(self) -> Any:
        return self._scope.account_filter() if self._scope else True

    def _derived_condition(self, model: Any) -> Any:
        if self._scope is None:
            return True
        return model.account_id.in_(self._scope.account_ids_subquery())

    @staticmethod
    def _account_summary(account: Account) -> Any:
        from finance_sync.services.read_api import AccountSummary

        return AccountSummary(
            id=str(account.id),
            name=account.name,
            account_type=str(account.account_type),
            account_subtype=account.account_subtype,
            currency_code=account.currency_code,
            current_balance=account.current_balance,
            available_balance=account.available_balance,
            provider_key=account.provider_key,
            is_active=account.is_active,
            owner_user_id=account.owner_user_id,
            created_at=account.created_at,
            updated_at=account.updated_at,
        )

    @staticmethod
    def _transaction_response(transaction: Transaction) -> Any:
        from finance_sync.services.read_api import TransactionResponse

        return TransactionResponse(
            id=str(transaction.id),
            account_id=str(transaction.account_id),
            security_id=(
                str(transaction.security_id)
                if transaction.security_id
                else None
            ),
            amount=transaction.amount,
            currency_code=transaction.currency_code,
            occurred_at=transaction.occurred_at,
            booked_at=transaction.booked_at,
            description=transaction.description,
            transaction_type=transaction.transaction_type,
            status=str(transaction.status),
            provider_key=transaction.provider_key,
            created_at=transaction.created_at,
            updated_at=transaction.updated_at,
        )

    async def list_accounts(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "name",
        sort_order: str = "asc",
        account_type: str | None = None,
        is_active: bool | None = None,
    ) -> Any:
        """List accounts using only account-domain queries."""
        from finance_sync.services.read_api import AccountDetailResponse

        conditions: list[Any] = [
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            self._account_condition(),
        ]
        if account_type is not None:
            conditions.append(Account.account_type == account_type)  # type: ignore[attr-defined]
        if is_active is not None:
            conditions.append(Account.is_active == is_active)  # type: ignore[attr-defined]
        sortable = {
            "name": Account.name,
            "account_type": Account.account_type,
            "current_balance": Account.current_balance,
            "created_at": Account.created_at,
            "updated_at": Account.updated_at,
        }
        total_result = await self._session.execute(
            select(func.count())
            .select_from(Account)
            .where(expression(*conditions))
        )
        result = await self._session.execute(
            select(Account)
            .where(expression(*conditions))
            .order_by(sort_field(sortable, sort_by, sort_order))
            .offset(offset)
            .limit(limit)
        )
        rows = result.scalars().all()
        return AccountDetailResponse(
            items=[self._account_summary(account) for account in rows],
            total=total_result.scalar() or 0,
            limit=limit,
            offset=offset,
        )

    async def get_account(self, tenant_id: str, account_id: str) -> Any:
        """Fetch one account within tenant and account scope."""
        result = await self._session.execute(
            select(Account).where(
                Account.id == account_id,  # type: ignore[attr-defined]
                Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
                self._account_condition(),
            )
        )
        account = result.scalar_one_or_none()
        return self._account_summary(account) if account else None

    async def list_account_transactions(
        self,
        tenant_id: str,
        account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "occurred_at",
        sort_order: str = "desc",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        transaction_type: str | None = None,
        security_id: str | None = None,
    ) -> Any:
        """List transactions for one visible account."""
        from finance_sync.services.read_api import TransactionListResponse

        conditions: list[Any] = [
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.account_id == account_id,  # type: ignore[attr-defined]
            self._derived_condition(Transaction),
        ]
        if date_from is not None:
            conditions.append(Transaction.occurred_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Transaction.occurred_at <= date_to)  # type: ignore[attr-defined]
        if transaction_type is not None:
            conditions.append(Transaction.transaction_type == transaction_type)  # type: ignore[attr-defined]
        if security_id is not None:
            conditions.append(Transaction.security_id == security_id)  # type: ignore[attr-defined]
        sortable = {
            "occurred_at": Transaction.occurred_at,
            "amount": Transaction.amount,
            "created_at": Transaction.created_at,
        }
        total_result = await self._session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(expression(*conditions))
        )
        result = await self._session.execute(
            select(Transaction)
            .where(expression(*conditions))
            .order_by(sort_field(sortable, sort_by, sort_order))
            .offset(offset)
            .limit(limit)
        )
        rows = result.scalars().all()
        return TransactionListResponse(
            items=[self._transaction_response(tx) for tx in rows],
            total=total_result.scalar() or 0,
            limit=limit,
            offset=offset,
        )
