"""finance-sync-sdk — Plugin SDK for finance-sync.

Build third-party connectors and exporters as separate pip-installable
packages.  Plugins are discovered at runtime via the
``finance_sync_sdk.plugins`` and ``finance_sync_sdk.exporters`` entry-point
groups.
"""

from finance_sync_sdk.exceptions import (
    ConnectorError,
    ExporterError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync_sdk.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    ConnectorHealth,
    ExportData,
    ExportFormat,
    ExportRequest,
    ExportResult,
    RawAccount,
    RawTransaction,
)
from finance_sync_sdk.plugin import ConnectorPlugin, ExporterPlugin
from finance_sync_sdk.rate_limiter import RateLimiter, RateLimitPolicy
from finance_sync_sdk.registry import PluginRegistry

__all__ = [
    "CanonicalAccountData",
    "CanonicalTransactionData",
    "ConnectorConfig",
    "ConnectorError",
    "ConnectorHealth",
    "ConnectorPlugin",
    "ExportData",
    "ExportFormat",
    "ExportRequest",
    "ExportResult",
    "ExporterError",
    "ExporterPlugin",
    "PermanentError",
    "PluginRegistry",
    "RateLimitError",
    "RateLimitPolicy",
    "RateLimiter",
    "RawAccount",
    "RawTransaction",
    "TransientError",
]
