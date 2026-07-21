# Plugin Development Guide

This document describes how to build, register, and distribute a
third-party connector or exporter plugin for **finance-sync** using the
`finance-sync-sdk` package.

---

## 1. Overview

The `finance-sync-sdk` package provides the base classes and utilities you
need to create plugins that finance-sync can discover and load at runtime.
There are two kinds of plugin:

| Plugin type | Purpose | Base class | Entry-point group |
|---|---|---|---|
| **Connector** | Fetch transactions from a financial provider (bank, broker, CSV file, …) | `ConnectorPlugin` | `finance_sync_sdk.plugins` |
| **Exporter** | Push data to a downstream destination (budgeting app, spreadsheet, …) | `ExporterPlugin` | `finance_sync_sdk.exporters` |

Plugins are **separate pip-installable packages** that declare entry points.
No configuration file changes are needed — the registry auto-discovers
every installed plugin.

---

## 2. Quick start

### 2.1 Install the SDK

```bash
pip install finance-sync-sdk
```

### 2.2 Write a connector plugin

```python
# mybank_finance_sync/plugin.py
from __future__ import annotations
from datetime import datetime
from decimal import Decimal

from finance_sync_sdk import ConnectorPlugin
from finance_sync_sdk.models import (
    ConnectorConfig, RawAccount, RawTransaction,
)
from finance_sync_sdk.exceptions import PermanentError
from finance_sync_sdk.rate_limiter import RateLimitPolicy


class MyBankPlugin(ConnectorPlugin):
    """Connector for MyBank's public API."""

    display_name = "MyBank"
    plugin_version = "0.1.0"

    rate_limit_policy = RateLimitPolicy(
        max_requests=30,
        window_seconds=60,
        max_retries=3,
    )

    @property
    def name(self) -> str:
        return "mybank"

    async def authenticate(self) -> None:
        api_key = self.config.credentials.get("api_key")
        if not api_key:
            raise PermanentError("api_key is required")
        # Validate token against provider API
        self._authenticated = True

    async def fetch_accounts(self) -> list[RawAccount]:
        # HTTP GET /accounts → parse response
        return [
            RawAccount(
                external_account_id="acc_123",
                name="MyBank Checking",
                account_type="checking",
                currency_code="EUR",
            )
        ]

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        # HTTP GET /transactions?since=...&account_id=...
        return [
            RawTransaction(
                external_transaction_id="tx_456",
                external_account_id=account_id or "acc_123",
                amount=Decimal("-50.00"),
                currency_code="EUR",
                occurred_at=since,
                description="Payment to Example Ltd",
                transaction_type="payment",
            )
        ]
```

### 2.3 Package and register

Create a `pyproject.toml` for your plugin package:

```toml
[project]
name = "mybank-finance-sync"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "finance-sync-sdk>=0.1.0",
]

[project.entry-points."finance_sync_sdk.plugins"]
mybank = "mybank_finance_sync.plugin:MyBankPlugin"

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

### 2.4 Install and verify

```bash
pip install -e .
```

Then in any finance-sync environment:

```python
from finance_sync_sdk.registry import PluginRegistry
from finance_sync_sdk.models import ConnectorConfig

registry = PluginRegistry()
print(registry.available_connectors)  # ['mybank', ...]
```

---

## 3. ConnectorPlugin API

### 3.1 Required overrides

| Method | Signature | Returns | Description |
|---|---|---|---|
| `name` | `@property` → `str` | | Short unique key, e.g. `"mybank"`. Must match the entry-point name. |
| `authenticate` | `async () → None` | | Obtain/validate credentials. Raise `PermanentError` on bad auth, `TransientError` on network issues. |
| `fetch_accounts` | `async () → list[RawAccount]` | Provider accounts | Called after successful authentication. |
| `fetch_transactions` | `async (since, *, account_id, limit) → list[RawTransaction]` | Provider transactions | `since` is a UTC datetime. `account_id` may scope the request. |

### 3.2 Optional overrides

| Method | Default | Description |
|---|---|---|
| `transform_accounts(raw)` | Identity copy → `CanonicalAccountData` | Override for provider-specific normalisation. |
| `transform_transactions(raw)` | Identity copy → `CanonicalTransactionData` | Override for provider-specific normalisation. |
| `health()` | Calls `authenticate()` | Lightweight connectivity check. |
| `config_schema` class attribute | `None` | Pydantic model for validating `ConnectorConfig.options`. |

### 3.3 Class attributes

| Attribute | Type | Default | Description |
|---|---|---|---|
| `display_name` | `str` | `""` | Human-readable name shown in UIs. |
| `plugin_version` | `str` | `"0.1.0"` | SemVer of the plugin itself. |
| `rate_limit_policy` | `RateLimitPolicy \| None` | `None` | When set, `fetch_accounts`/`fetch_transactions` are automatically wrapped with rate-limiting and retry logic. |

### 3.4 Protected helpers

| Method | Purpose |
|---|---|
| `_rate_limited_fetch_accounts()` | Calls `fetch_accounts` with rate-limit + retry protection. |
| `_rate_limited_fetch_transactions(...)` | Calls `fetch_transactions` with rate-limit + retry protection. |

---

## 4. ExporterPlugin API

### 4.1 Required overrides

| Method | Signature | Returns | Description |
|---|---|---|---|
| `name` | `@property` → `str` | | Short unique key, e.g. `"csv_exporter"`. |
| `export` | `async (request: ExportRequest) → ExportResult` | Exported data | Produce exported data for the given request. |

### 4.2 Template method

| Method | Default | Description |
|---|---|---|
| `run_export(request)` | Delegates to `export()` | Template method. Override to add pre/post processing. Enforces `supported_formats` validation by default. |

### 4.3 Class attributes

| Attribute | Type | Default | Description |
|---|---|---|---|
| `display_name` | `str` | `""` | Human-readable name. |
| `supported_formats` | `list[str] \| None` | `None` | List of supported formats (see `ExportFormat`). |
| `config_schema` | `type \| None` | `None` | Pydantic model for config validation. |

---

## 5. Pydantic models

### 5.1 Connector models

| Model | Description |
|---|---|
| `ConnectorConfig` | `provider_type`, `credentials` (dict), `options` (dict) |
| `RawAccount` | Raw provider account data |
| `RawTransaction` | Raw provider transaction data |
| `CanonicalAccountData` | Normalised account for upsert into finance-sync |
| `CanonicalTransactionData` | Normalised transaction for upsert into finance-sync |
| `ConnectorHealth` | Result of a health check |

### 5.2 Exporter models

| Model | Description |
|---|---|
| `ExportRequest` | `format`, `since`, `account_ids`, `options` |
| `ExportResult` | `status`, `records_exported/failed`, `error_message` |
| `ExportData` | `format`, `content`, `filename`, `extension`, `mime_type` |
| `ExportFormat` | Constants: `CSV`, `JSON`, `XLSX`, `OFX`, `QIF` |

---

## 6. Error handling

| Exception | When to use |
|---|---|
| `ConnectorError` | Base exception for connector errors (not directly raised). |
| `TransientError` | Temporary failure safe to retry (network timeout, HTTP 503). |
| `PermanentError` | Non-recoverable failure (bad auth, invalid data). |
| `RateLimitError` | HTTP 429; carries a `retry_after` hint (seconds). |
| `ExporterError` | Base exception for exporter errors. |

`TransientError` and `RateLimitError` are automatically retried by the
rate-limiter when `rate_limit_policy` is set.

---

## 7. Configuration schemas

Plugins can declare a Pydantic config schema for their `options`:

```python
from pydantic import BaseModel, Field
from finance_sync_sdk.config import PluginConfigSchema

class MyBankOptions(PluginConfigSchema):
    api_key: str = Field(..., description="MyBank API key")
    sandbox: bool = Field(default=False, description="Use sandbox API")
    endpoint: str = Field(default="https://api.mybank.com/v1")

class MyBankPlugin(ConnectorPlugin):
    config_schema = MyBankOptions
    ...
```

When `config_schema` is set, the SDK validates `ConnectorConfig.options`
automatically in the constructor.

---

## 8. Credential providers

For environments where credentials come from a vault or env vars:

```python
from finance_sync_sdk.credentials import EnvCredentialProvider

provider = EnvCredentialProvider(prefix="MYBANK_")
api_key = await provider.get("API_KEY")  # reads MYBANK_API_KEY
```

Built-in providers:
- `EnvCredentialProvider` — reads from environment variables
- `DictCredentialProvider` — in-memory dict (useful for testing)

---

## 9. Testing your plugin

### 9.1 Unit tests

```python
import pytest
from datetime import datetime, timezone
from finance_sync_sdk.models import ConnectorConfig
from mybank_finance_sync.plugin import MyBankPlugin


@pytest.fixture
def plugin():
    config = ConnectorConfig(
        provider_type="mybank",
        credentials={"api_key": "test_123"},
        options={"sandbox": True},
    )
    return MyBankPlugin(config=config)


@pytest.mark.asyncio
async def test_authenticate(plugin):
    await plugin.authenticate()
    assert plugin._authenticated


@pytest.mark.asyncio
async def test_fetch_accounts(plugin):
    await plugin.authenticate()
    accounts = await plugin.fetch_accounts()
    assert len(accounts) > 0
    assert accounts[0].external_account_id
```

### 9.2 Use PluginRegistry

```python
from finance_sync_sdk.registry import PluginRegistry

registry = PluginRegistry()
registry.register_connector("mybank", MyBankPlugin, replace=True)
config = ConnectorConfig(provider_type="mybank", credentials={"api_key": "test"})
plugin = registry.get_connector(config)
```

---

## 10. Distribution

### 10.1 Build and publish to PyPI

```bash
pip install build twine
python -m build
twine check dist/*
twine upload dist/*
```

### 10.2 GitHub release workflow

See `.github/workflows/publish-sdk.yml` in the finance-sync repository for
the workflow that publishes `finance-sync-sdk` itself.  Adapt it for your
plugin:

```yaml
name: Publish to PyPI
on:
  push:
    tags: ["v*"]
jobs:
  pypi:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build twine
      - run: python -m build
      - run: twine upload dist/* -u __token__ -p ${{ secrets.PYPI_TOKEN }}
```

---

## 11. Best practices

1. **Never log credentials** — the SDK redacts secrets in structured logs.
2. **Handle pagination** — loop through cursor/page tokens in `fetch_transactions`.
3. **Set `rate_limit_policy`** — providers with API limits need this.
4. **Test idempotency** — calling `fetch_accounts()` or `fetch_transactions()`
   twice should be safe.
5. **Set a meaningful `display_name`** — shown in admin UIs and API metadata.
6. **Use `config_schema`** — makes your plugin self-documenting and validates
   user input early.
7. **Ship with a `py.typed` marker file** — enables type checking for your
   plugin's users.
