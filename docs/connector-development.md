# Connector Development Guide

This document describes how to build, register, and test a new finance-sync
connector.

---

## 1. Overview

A *connector* is a Python class that subclasses
`finance_sync.connectors.base.Connector` and implements its abstract
methods.  Each connector knows how to authenticate with one external
financial provider, fetch accounts and transactions, and normalise the raw
data into canonical models.

Connectors are discovered at runtime via Python **entry points** (setuptools
`[project.entry-points]`).  This means you can ship a connector either:

- Inside the `finance-sync` package itself (built-in connector)
- As a **separate pip‑installable package** that declares the same entry
  point group (third‑party plugin)

No configuration file changes are needed — the registry auto‑discovers
every installed connector.

---

## 2. Write a connector

### 2.1 Subclass `Connector`

```python
from __future__ import annotations

from datetime import datetime
from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.exceptions import PermanentError, TransientError
from finance_sync.connectors.rate_limiter import RateLimitPolicy


class MyBankConnector(Connector):
    """Connector for MyBank API."""

    display_name = "MyBank"
    sdk_version = "0.1.0"

    # Optional: rate-limit policy
    rate_limit_policy = RateLimitPolicy(
        max_requests=30,
        window_seconds=60,
        max_retries=3,
        backoff_base=1.0,
    )

    @property
    def name(self) -> str:
        return self.config.provider_type  # e.g. "mybank"

    async def authenticate(self) -> None:
        token = self.config.credentials.get("api_key")
        if not token:
            raise PermanentError("api_key is required")
        # Validate the token against the provider's API
        # Raise TransientError on network blips

    async def fetch_accounts(self) -> list[RawAccount]:
        # HTTP GET /accounts
        # Parse response into RawAccount list
        ...

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        # HTTP GET /transactions?since=...&account_id=...&limit=...
        # Parse response into RawTransaction list
        ...
```

### 2.2 Credentials

Credentials are provided at instantiation time via
`ConnectorConfig.credentials`.  They have already been **decrypted** from
the envelope‑encrypted store (AES‑256‑GCM).  The connector never handles
encrypted blobs.

| Config key | Meaning |
|---|---|
| `provider_type` | Must match the entry‑point name (e.g. `"mybank"`) |
| `credentials` | Dict of secrets (api_key, client_id, client_secret, …) |
| `options` | Non‑secret config (sandbox mode, custom endpoint, …) |

### 2.3 Rate limiting and retries

Set `rate_limit_policy` as a class attribute (see example above).  The
framework then wraps `fetch_accounts()` and `fetch_transactions()` with a
sliding‑window rate limiter and exponential‑backoff retry.

If you need to call them with protection yourself, use the
`_rate_limited_fetch_accounts()` / `_rate_limited_fetch_transactions()`
helper methods — these apply the same policy.

### 2.4 Error classification

| Exception | When to raise |
|---|---|
| `PermanentError` | Invalid credentials, resource not found, malformed response |
| `TransientError` | Network timeout, HTTP 503, temporary provider outage |
| `RateLimitError` | HTTP 429 (pass `retry_after` if the provider sends it) |

`TransientError` (and `RateLimitError`) are automatically retried by the
rate‑limiter.  `PermanentError` is never retried.

### 2.5 Transform methods

The default `transform_accounts()` and `transform_transactions()` copy
fields by name.  Override them when your provider returns data in a
non‑standard format or needs normalisation (e.g. splitting a combined
field, mapping provider‑specific type codes to canonical types).

---

## 3. Register the connector

### Built‑in connector (inside finance-sync)

Add an entry to `pyproject.toml`:

```toml
[project.entry-points."finance_sync.connectors"]
bunq = "finance_sync.connectors.bunq:BunqConnector"
trading212 = "finance_sync.connectors.trading212:Trading212Connector"
csv_import = "finance_sync.connectors.csv_import:CSVImportConnector"
manual_expense = "finance_sync.connectors.manual_expense:ManualExpenseConnector"
plaid_like = "finance_sync.connectors.plaid_like:PlaidLikeConnector"
ynab = "finance_sync.connectors.ynab:YnabConnector"
```

### Third‑party plugin (separate package)

In your plugin's `pyproject.toml`:

```toml
[project.entry-points."finance_sync.connectors"]
mybank = "mybank_finance_sync:MyBankConnector"

[project.dependencies]
finance-sync = ">=0.1.0"
```

Users install your package with `pip install mybank-finance-sync` and the
registry discovers it automatically.

---

## 4. Test the connector

### 4.1 Contract tests

Every connector **must** pass the contract tests.  Create a test file:

```python
# tests/test_mybank_connector.py
import pytest
from datetime import datetime, timezone, timedelta
from finance_sync.connectors.models import ConnectorConfig
from tests.connectors.contract_test_template import ConnectorContractTest


class TestMyBankConnector(ConnectorContractTest):
    @pytest.fixture
    def connector_config(self) -> ConnectorConfig:
        return ConnectorConfig(
            provider_type="mybank",
            credentials={"api_key": "test_123"},
            options={"sandbox": True},
        )

    @pytest.fixture
    def connector(self, connector_config: ConnectorConfig) -> Connector:
        from finance_sync.connectors.mybank import MyBankConnector
        return MyBankConnector(config=connector_config)

    @pytest.fixture
    def sample_raw_data(self):
        from finance_sync.connectors.models import (
            RawAccount, RawTransaction, Decimal,
        )
        account = RawAccount(
            external_account_id="acc_1",
            name="MyBank Checking",
            account_type="checking",
        )
        txn = RawTransaction(
            external_transaction_id="tx_1",
            external_account_id="acc_1",
            amount=Decimal("-10.00"),
            occurred_at=datetime.now(timezone.utc) - timedelta(days=1),
            transaction_type="payment",
        )
        return [account], [txn]
```

### 4.2 Unit tests

Write additional unit tests for your connector's specific logic —
pagination, cursor handling, provider‑specific field mapping, etc.

### 4.3 Integration tests (optional)

For CI, use a test sandbox / developer API token provided by the financial
institution.  Never commit real credentials.

---

## 5. Using the registry in application code

```python
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.connectors.models import ConnectorConfig

registry = ConnectorRegistry()

# List available connectors
print(registry.available)

# Instantiate a connector
config = ConnectorConfig(
    provider_type="mybank",
    credentials={"api_key": "sk_live_..."},
    options={"sandbox": False},
)
connector = registry.get_connector(config)

# Use it
await connector.authenticate()
accounts = await connector.fetch_accounts()
for account in accounts:
    canonical = connector.transform_accounts([account])
    # upsert canonical[0] into the database
```

---

## 6. Best practices

1. **Never log credentials.**  Use `repr()` sparingly; the framework
   redacts secrets automatically in structured logs.

2. **Handle pagination.**  Most providers paginate account and transaction
   lists.  The `_rate_limited_fetch_*` helpers protect each page fetch.

3. **Respect rate limits.**  Always set `rate_limit_policy`.  Providers
   that return `Retry-After` headers should raise `RateLimitError` with
   the appropriate `retry_after` value.

4. **Test idempotency.**  Calling `fetch_accounts()` or
   `fetch_transactions()` twice should be safe — the second call may
   return updated data but must never raise.

5. **Set a meaningful `display_name`.**  This is shown in admin UIs and
   API metadata.

6. **Keep transform methods simple.**  Complex mapping logic belongs in a
   dedicated normaliser, not in the connector itself.
