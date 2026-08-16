"""Synthetic provider API used by the staging environment only."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from finance_sync.config.settings import ROOT_DIR, Settings
from finance_sync.dependencies import get_settings

router = APIRouter(prefix="/staging-providers", include_in_schema=False)
_FIXTURES = ROOT_DIR / "deploy" / "staging" / "fixtures" / "2026-07"


def _require_staging(settings: Settings = Depends(get_settings)) -> None:
    if not settings.is_staging:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@lru_cache(maxsize=16)
def _fixture(relative_path: str) -> Any:
    path = (_FIXTURES / relative_path).resolve()
    if _FIXTURES.resolve() not in path.parents:
        msg = "Invalid staging fixture path"
        raise ValueError(msg)
    return json.loads(path.read_text(encoding="utf-8"))


@router.post(
    "/bunq/v1/session-server", dependencies=[Depends(_require_staging)]
)
async def bunq_session() -> Any:
    return _fixture("bunq/session-server.json")


@router.get(
    "/bunq/v1/user/{user_id}/monetary-account",
    dependencies=[Depends(_require_staging)],
)
async def bunq_accounts(user_id: int) -> Any:
    if user_id != 9900001:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _fixture("bunq/monetary-accounts.json")


@router.get(
    "/bunq/v1/monetary-account/{account_id}/payment",
    dependencies=[Depends(_require_staging)],
)
async def bunq_payments(account_id: int) -> Any:
    if account_id != 9100001:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _fixture("bunq/payments-account-9100001.json")


@router.get(
    "/trading212/api/v0/equity/account/info",
    dependencies=[Depends(_require_staging)],
)
async def trading212_account_info() -> Any:
    return _fixture("trading212/account-info.json")


@router.get(
    "/trading212/api/v0/equity/account/cash",
    dependencies=[Depends(_require_staging)],
)
async def trading212_account_cash() -> Any:
    return _fixture("trading212/account-cash.json")


@router.get(
    "/trading212/api/v0/equity/history/orders",
    dependencies=[Depends(_require_staging)],
)
async def trading212_orders() -> Any:
    return _fixture("trading212/order-history.json")


@router.get(
    "/trading212/api/v0/equity/history/transactions",
    dependencies=[Depends(_require_staging)],
)
async def trading212_transactions() -> Any:
    return _fixture("trading212/transaction-history.json")


@router.get(
    "/trading212/api/v0/equity/portfolio",
    dependencies=[Depends(_require_staging)],
)
async def trading212_portfolio() -> Any:
    return _fixture("trading212/portfolio.json")
