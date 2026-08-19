"""Top-level v1 router that aggregates all sub-routers."""

from __future__ import annotations

from fastapi import APIRouter

from finance_sync.api.v1.accounts import router as accounts_router
from finance_sync.api.v1.ai_summary import router as ai_summary_router
from finance_sync.api.v1.allocation import router as allocation_router
from finance_sync.api.v1.auth import router as auth_router
from finance_sync.api.v1.card_transactions import (
    router as card_transactions_router,
)
from finance_sync.api.v1.cashflow import router as cashflow_router
from finance_sync.api.v1.connectors_config import (
    router as connectors_config_router,
)
from finance_sync.api.v1.datamarts import router as datamarts_router
from finance_sync.api.v1.degiro_imports import router as degiro_imports_router
from finance_sync.api.v1.destinations import router as destinations_router
from finance_sync.api.v1.dividends import router as dividends_router
from finance_sync.api.v1.enrichment import router as enrichment_router
from finance_sync.api.v1.feedback import router as feedback_router
from finance_sync.api.v1.ha_integration import router as ha_integration_router
from finance_sync.api.v1.holding_relevance import (
    router as holding_relevance_router,
)
from finance_sync.api.v1.holdings import router as holdings_router
from finance_sync.api.v1.intel_credentials import (
    router as intel_credentials_router,
)
from finance_sync.api.v1.legacy_exporters import (
    router as legacy_exporters_router,
)
from finance_sync.api.v1.market_intelligence import (
    router as market_intelligence_router,
)
from finance_sync.api.v1.net_worth import router as net_worth_router
from finance_sync.api.v1.performance import router as performance_router
from finance_sync.api.v1.portfolio import router as portfolio_router
from finance_sync.api.v1.prices import router as prices_router
from finance_sync.api.v1.reconciliation import router as reconciliation_router
from finance_sync.api.v1.root import router as root_router
from finance_sync.api.v1.scheduled_payments import (
    router as scheduled_payments_router,
)
from finance_sync.api.v1.securities import router as securities_router
from finance_sync.api.v1.staging_providers import (
    router as staging_providers_router,
)
from finance_sync.api.v1.subscriptions import router as subscriptions_router
from finance_sync.api.v1.sync import router as sync_router
from finance_sync.api.v1.sync_runs import router as sync_runs_router
from finance_sync.api.v1.sync_schedules import router as sync_schedules_router
from finance_sync.api.v1.tax_lots import router as tax_lots_router
from finance_sync.api.v1.transactions import router as transactions_router
from finance_sync.api.v1.webhooks import router as webhooks_router

router = APIRouter()
router.include_router(root_router)
router.include_router(auth_router)
router.include_router(ai_summary_router)
router.include_router(cashflow_router)
router.include_router(connectors_config_router)
router.include_router(degiro_imports_router)
router.include_router(destinations_router)
router.include_router(datamarts_router)
router.include_router(enrichment_router)
router.include_router(legacy_exporters_router)
router.include_router(feedback_router)
router.include_router(securities_router)
router.include_router(accounts_router)
router.include_router(allocation_router)
router.include_router(ha_integration_router)
router.include_router(performance_router)
router.include_router(portfolio_router)
router.include_router(net_worth_router)
router.include_router(reconciliation_router)
router.include_router(subscriptions_router)
router.include_router(sync_runs_router)
router.include_router(sync_schedules_router)
router.include_router(tax_lots_router)
router.include_router(webhooks_router)
router.include_router(scheduled_payments_router)
router.include_router(card_transactions_router)
router.include_router(transactions_router)
router.include_router(holdings_router)
router.include_router(holding_relevance_router)
router.include_router(market_intelligence_router)
router.include_router(intel_credentials_router)
router.include_router(dividends_router)
router.include_router(prices_router)
router.include_router(sync_router)
router.include_router(staging_providers_router)
