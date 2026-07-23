"""Top-level v1 router that aggregates all sub-routers."""

from __future__ import annotations

from fastapi import APIRouter

from finance_sync.api.v1.accounts import router as accounts_router
from finance_sync.api.v1.ai_summary import router as ai_summary_router
from finance_sync.api.v1.auth import router as auth_router
from finance_sync.api.v1.cashflow import router as cashflow_router
from finance_sync.api.v1.connectors_config import (
    router as connectors_config_router,
)
from finance_sync.api.v1.enrichment import router as enrichment_router
from finance_sync.api.v1.feedback import router as feedback_router
from finance_sync.api.v1.ha_integration import router as ha_integration_router
from finance_sync.api.v1.net_worth import router as net_worth_router
from finance_sync.api.v1.portfolio import router as portfolio_router
from finance_sync.api.v1.root import router as root_router
from finance_sync.api.v1.securities import router as securities_router
from finance_sync.api.v1.sync_runs import router as sync_runs_router
from finance_sync.api.v1.webhooks import router as webhooks_router

router = APIRouter()
router.include_router(root_router)
router.include_router(auth_router)
router.include_router(ai_summary_router)
router.include_router(cashflow_router)
router.include_router(connectors_config_router)
router.include_router(enrichment_router)
router.include_router(feedback_router)
router.include_router(securities_router)
router.include_router(accounts_router)
router.include_router(ha_integration_router)
router.include_router(portfolio_router)
router.include_router(net_worth_router)
router.include_router(sync_runs_router)
router.include_router(webhooks_router)
