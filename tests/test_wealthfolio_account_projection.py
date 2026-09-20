from types import SimpleNamespace

from finance_sync.exporter.wealthfolio.exporter import _is_cash_account


def test_bunq_accounts_are_always_projected_as_cash() -> None:
    account = SimpleNamespace(provider_key="bunq", account_type="investment")

    assert _is_cash_account(account) is True


def test_non_bunq_investment_accounts_remain_investments() -> None:
    account = SimpleNamespace(provider_key="degiro", account_type="investment")

    assert _is_cash_account(account) is False
