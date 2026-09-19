"""Deterministic financial data for local development and staging.

This data is deliberately inserted as normalized records.  It does not
pretend to be a provider sync and contains no credentials or real personal
financial data.  The stable external IDs make the operation idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text

from finance_sync.models import Account, Balance, Holding, Security, Transaction
from finance_sync.models.enums import (
    AccountType,
    BalanceKind,
    BalanceSource,
    HoldingSource,
    SecurityType,
    TransactionStatus,
    TransactionType,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SEED_MARKER = "finance-sync.synthetic.v1.bunq"
_START = datetime(2026, 7, 1, 12, tzinfo=UTC)


async def seed_non_production_dataset(
    session: AsyncSession, tenant_id: Any, owner_user_id: Any | None = None
) -> bool:
    """Insert the synthetic dataset once, returning whether rows were added."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        # App and worker run this seed in separate sessions during a
        # concurrent staging startup.  The marker check alone is not enough:
        # both transactions can pass it before either commits.
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('finance-sync:non-production-seed'))"
            )
        )
    marker = await session.scalar(
        select(Account.id).where(
            Account.tenant_id == tenant_id,
            Account.provider_key == "bunq",
            Account.external_account_id == SEED_MARKER,
        )
    )
    if marker is not None:
        return False

    accounts = {
        "bunq": Account(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            provider_key="bunq",
            external_account_id=SEED_MARKER,
            name="Staging Betaalrekening (bunq)",
            account_type=AccountType.CHECKING,
            currency_code="EUR",
            iso_currency_code="EUR",
            current_balance=Decimal("2500.00"),
            available_balance=Decimal("2500.00"),
            provider_metadata={"synthetic": True, "source": "seed"},
        ),
        "trading212": Account(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            provider_key="trading212",
            external_account_id="finance-sync.synthetic.v1.trading212",
            name="Staging Portfolio (Trading 212)",
            account_type=AccountType.BROKERAGE,
            currency_code="EUR",
            iso_currency_code="EUR",
            current_balance=Decimal("1250.00"),
            available_balance=Decimal("1250.00"),
            provider_metadata={"synthetic": True, "source": "seed"},
        ),
        "degiro": Account(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            provider_key="degiro_pension",
            external_account_id="finance-sync.synthetic.v1.degiro",
            name="Staging Pensioenrekening (DEGIRO)",
            account_type=AccountType.BROKERAGE,
            currency_code="EUR",
            iso_currency_code="EUR",
            current_balance=Decimal("350.00"),
            available_balance=Decimal("350.00"),
            provider_metadata={"synthetic": True, "source": "seed"},
        ),
    }
    session.add_all(accounts.values())
    await session.flush()

    securities_data = [
        (
            "IE00BK5BQT80",
            "VWCE.DE",
            "Vanguard FTSE All-World UCITS ETF",
            SecurityType.ETF,
            "EUR",
        ),
        (
            "NL0010273215",
            "ASML.NL",
            "ASML Holding NV",
            SecurityType.STOCK,
            "EUR",
        ),
        ("US0378331005", "AAPL", "Apple Inc.", SecurityType.STOCK, "USD"),
        (
            "IE00B4L5Y983",
            "IWDA.NL",
            "iShares Core MSCI World UCITS ETF",
            SecurityType.ETF,
            "EUR",
        ),
    ]
    isins = [item[0] for item in securities_data]
    tickers = [item[1] for item in securities_data]
    existing = list(
        await session.scalars(
            select(Security).where(
                Security.isin.in_(isins) | Security.ticker.in_(tickers)
            )
        )
    )
    existing_by_isin = {
        security.isin: security for security in existing if security.isin
    }
    existing_by_ticker = {
        security.ticker: security
        for security in existing
        if security.ticker and security.isin
    }
    securities: dict[str, Security] = {}
    for isin, ticker, name, kind, currency in securities_data:
        existing_security = existing_by_isin.get(
            isin
        ) or existing_by_ticker.get(ticker)
        if existing_security is not None:
            # A previous interrupted seed could have inserted the security
            # before the transaction rolled back.  Complete that row instead
            # of reusing an incomplete record (notably a missing ticker),
            # otherwise Wealthfolio cannot reconcile the holding identity.
            existing_security.ticker = ticker
            existing_security.name = name
            existing_security.security_type = kind
            existing_security.currency_code = currency
            securities[ticker] = existing_security
            continue
        security = Security(
            isin=isin,
            ticker=ticker,
            name=name,
            security_type=kind,
            currency_code=currency,
        )
        securities[ticker] = security
        session.add(security)
    await session.flush()

    def transaction(
        provider: str,
        account: Account,
        external_id: str,
        amount: str,
        when: datetime,
        kind: TransactionType,
        description: str,
        *,
        security: Security | None = None,
        quantity: str | None = None,
        unit_price: str | None = None,
        currency: str = "EUR",
        fx_rate: str | None = None,
    ) -> Transaction:
        return Transaction(
            tenant_id=tenant_id,
            provider_key=provider,
            connection_id=None,
            external_transaction_id=external_id,
            account_id=account.id,
            security_id=security.id if security else None,
            amount=Decimal(amount),
            currency_code=currency,
            amount_in_base=(
                Decimal(amount)
                if currency == "EUR"
                else (
                    Decimal(amount) / Decimal(fx_rate)
                    if fx_rate is not None
                    else None
                )
            ),
            base_currency_code="EUR"
            if fx_rate is not None or currency == "EUR"
            else None,
            fx_rate=Decimal(fx_rate) if fx_rate is not None else None,
            quantity=Decimal(quantity) if quantity else None,
            unit_price=Decimal(unit_price) if unit_price else None,
            occurred_at=when,
            booked_at=when,
            transaction_type=kind,
            description=description,
            status=TransactionStatus.BOOKED,
            provider_fingerprint=f"synthetic:{provider}:{external_id}",
        )

    bunq_amounts = [
        ("3250.00", "Salaris juli", TransactionType.DEPOSIT),
        ("-1250.00", "Huur juli", TransactionType.PAYMENT),
        ("-54.32", "Boodschappen", TransactionType.PAYMENT),
        ("-4.20", "Koffie", TransactionType.PAYMENT),
        ("-32.50", "Openbaar vervoer", TransactionType.PAYMENT),
        ("-68.40", "Energie", TransactionType.PAYMENT),
        ("-18.75", "Lunch", TransactionType.PAYMENT),
        ("-24.95", "Apotheek", TransactionType.PAYMENT),
        ("-12.00", "Streaming", TransactionType.PAYMENT),
        ("-75.00", "Pensioeninleg", TransactionType.TRANSFER),
    ]
    session.add_all(
        transaction(
            "bunq",
            accounts["bunq"],
            f"bunq-{day:02d}",
            amount,
            _START + timedelta(days=day - 1),
            kind,
            description,
        )
        for day, (amount, description, kind) in enumerate(bunq_amounts, 1)
    )
    for index, (ticker, quantity, price, amount) in enumerate(
        [
            ("VWCE.DE", "2", "130", "-260"),
            ("AAPL", "1", "195", "-195"),
            ("ASML.NL", "0.5", "680", "-340"),
            ("IWDA.NL", "3", "91", "-273"),
        ],
        1,
    ):
        session.add(
            transaction(
                "trading212",
                accounts["trading212"],
                f"t212-order-{index}",
                amount,
                _START + timedelta(days=index * 7 - 4),
                TransactionType.PURCHASE,
                f"Synthetic aankoop {ticker}",
                security=securities[ticker],
                quantity=quantity,
                unit_price=price,
            )
        )
    session.add(
        transaction(
            "trading212",
            accounts["trading212"],
            "t212-dividend-1",
            "0.25",
            _START + timedelta(days=16),
            TransactionType.DIVIDEND,
            "Synthetic AAPL dividend",
            security=securities["AAPL"],
        )
    )
    for index, (ticker, isin_amount, price, amount, currency) in enumerate(
        [
            ("VWCE.DE", "2", "130", "-260", "EUR"),
            ("ASML.NL", "0.5", "680", "-340", "EUR"),
            ("AAPL", "1", "210", "-182.61", "EUR"),
            ("IWDA.NL", "3", "91", "-273", "EUR"),
        ],
        1,
    ):
        session.add(
            transaction(
                "degiro_pension",
                accounts["degiro"],
                f"degiro-trade-{index}",
                amount,
                _START + timedelta(days=index * 7 - 1),
                TransactionType.PURCHASE,
                f"Synthetic DEGIRO aankoop {ticker}",
                security=securities[ticker],
                quantity=isin_amount,
                unit_price=price,
                currency=currency,
            )
        )
    session.add(
        transaction(
            "degiro_pension",
            accounts["degiro"],
            "degiro-dividend-1",
            "0.25",
            _START + timedelta(days=16),
            TransactionType.DIVIDEND,
            "Synthetic dividend",
            security=securities["AAPL"],
            currency="USD",
            fx_rate="1.10",
        )
    )

    def holding(
        account_key: str,
        ticker: str,
        quantity: str,
        cost: str,
        value: str,
        price: str,
    ) -> Holding:
        return Holding(
            tenant_id=tenant_id,
            account_id=accounts[account_key].id,
            security_id=securities[ticker].id,
            observed_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            quantity=Decimal(quantity),
            cost_basis=Decimal(cost),
            cost_basis_currency="EUR",
            market_value=Decimal(value),
            currency_code="EUR",
            price=Decimal(price),
            price_currency="EUR",
            source=HoldingSource.PROVIDER_SYNC,
        )

    session.add_all(
        [
            holding("trading212", "VWCE.DE", "2", "260", "264", "132"),
            holding("trading212", "AAPL", "1", "195", "198", "198"),
            holding("trading212", "ASML.NL", "0.5", "340", "350", "700"),
            holding("trading212", "IWDA.NL", "3", "273", "276", "92"),
            holding("degiro", "VWCE.DE", "2", "260", "264", "132"),
            holding("degiro", "AAPL", "1", "186.96", "215", "215"),
            holding("degiro", "ASML.NL", "0.5", "340", "350", "700"),
            holding("degiro", "IWDA.NL", "3", "273", "276", "92"),
        ]
    )
    session.add_all(
        [
            Balance(
                tenant_id=tenant_id,
                account_id=account.id,
                observed_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
                balance_kind=BalanceKind.CURRENT,
                amount=account.current_balance,
                currency_code="EUR",
                source=BalanceSource.PROVIDER_SYNC,
            )
            for account in accounts.values()
        ]
    )
    await session.commit()
    return True
