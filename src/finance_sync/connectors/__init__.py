"""Connector SDK — provider-agnostic plugin system.

Build connectors by subclassing ``Connector`` and registering them via the
``finance_sync.connectors`` entry point group in ``pyproject.toml``.

See ``docs/connector-development.md`` for the full guide.
"""

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    ConnectorHealth,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.rate_limiter import RateLimiter, RateLimitPolicy
from finance_sync.connectors.registry import ConnectorRegistry

__all__ = [
    "CanonicalAccountData",
    "CanonicalTransactionData",
    "Connector",
    "ConnectorConfig",
    "ConnectorError",
    "ConnectorHealth",
    "ConnectorRegistry",
    "PermanentError",
    "RateLimitError",
    "RateLimitPolicy",
    "RateLimiter",
    "RawAccount",
    "RawTransaction",
    "TransientError",
]
