# Connector Plugin API Reference

This document describes the **stable public API** that third-party connector
plugins interact with.  It covers the SDK classes, models, and protocols
that are guaranteed to remain backward-compatible within the same major
version of `finance-sync-sdk`.

---

## 1. Plugin package layout

A third-party connector plugin must be a Python package that:

1. Subclasses `finance_sync_sdk.ConnectorPlugin` (or
   `finance_sync_sdk.ExporterPlugin`).
2. Declares a `[project.entry-points]` entry in its `pyproject.toml`.

```
mybank-finance-sync/
├── pyproject.toml
├── src/
│   └── mybank_finance_sync/
│       ├── __init__.py
│       ├── plugin.py          # ConnectorPlugin subclass
│       ├── config.py          # Optional: Pydantic config schema
│       └── py.typed           # PEP 561 marker
└── tests/
    └── test_plugin.py
```

---

## 2. Entry-point contracts

### Connector plugins

```toml
[project.entry-points."finance_sync_sdk.plugins"]
mybank = "mybank_finance_sync.plugin:MyBankPlugin"
```

The entry point **value** must be a dotted path to a `ConnectorPlugin`
subclass.  The entry point **name** becomes the `provider_type` that users
pass to `ConnectorConfig`.

### Exporter plugins

```toml
[project.entry-points."finance_sync_sdk.exporters"]
csv = "mybank_finance_sync.exporter:CSVExporter"
```

---

## 3. The ConnectorPlugin ABC

```python
class ConnectorPlugin(ABC):
    display_name: str = ""
    plugin_version: str = "0.1.0"
    config_schema: type[Any] | None = None
    rate_limit_policy: RateLimitPolicy | None = None

    def __init__(self, config: ConnectorConfig) -> None: ...

    # — Required —
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def authenticate(self) -> None: ...

    @abstractmethod
    async def fetch_accounts(self) -> list[RawAccount]: ...

    @abstractmethod
    async def fetch_transactions(
        self, since: datetime, *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]: ...

    # — Optional —
    async def health(self) -> ConnectorHealth: ...
    def transform_accounts(self, raw: list[RawAccount]) -> list[CanonicalAccountData]: ...
    def transform_transactions(self, raw: list[RawTransaction]) -> list[CanonicalTransactionData]: ...

    # — Helpers —
    async def _rate_limited_fetch_accounts(self) -> list[RawAccount]: ...
    async def _rate_limited_fetch_transactions(...) -> list[RawTransaction]: ...

    @classmethod
    def describe(cls) -> dict[str, Any]: ...
```

### Contract

- `authenticate()` is called once before any `fetch_*` call.
- `fetch_accounts()` may be called multiple times after a single `authenticate()`.
- `fetch_transactions()` must accept a `since` datetime in UTC.
- Exceptions must be from the `finance_sync_sdk.exceptions` hierarchy.

---

## 4. The ExporterPlugin ABC

```python
class ExporterPlugin(ABC):
    display_name: str = ""
    plugin_version: str = "0.1.0"
    config_schema: type[Any] | None = None
    supported_formats: list[str] | None = None

    def __init__(self, config: object | None = None) -> None: ...

    # — Required —
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def export(self, request: ExportRequest) -> ExportResult: ...

    # — Template method —
    async def run_export(self, request: ExportRequest) -> ExportResult: ...

    @classmethod
    def describe(cls) -> dict[str, Any]: ...
```

---

## 5. Data models

### 5.1 ConnectorConfig

```python
class ConnectorConfig(BaseModel):
    provider_type: str        # Must match the entry-point name
    credentials: dict[str, str]  # Provider secrets (decrypted)
    options: dict[str, Any]      # Non-secret configuration
```

### 5.2 RawAccount

```python
class RawAccount(BaseModel):
    external_account_id: str
    name: str
    account_type: str               # "checking", "savings", "brokerage", etc.
    account_subtype: str | None
    currency_code: str              # ISO-4217, default "EUR"
    current_balance: Decimal | None
    available_balance: Decimal | None
    iso_currency_code: str | None
    provider_metadata: dict[str, Any] | None
```

### 5.3 RawTransaction

```python
class RawTransaction(BaseModel):
    external_transaction_id: str
    external_account_id: str
    amount: Decimal                 # Positive = inflow, negative = outflow
    currency_code: str
    occurred_at: datetime           # UTC
    booked_at: datetime | None
    description: str | None
    transaction_type: str | None    # "payment", "purchase", "fee", etc.
    status: str | None              # "pending", "booked", "cancelled"
    provider_fingerprint: str | None
    provider_metadata: dict[str, Any] | None
```

### 5.4 CanonicalAccountData / CanonicalTransactionData

See `finance_sync_sdk.models` for the full schemas.  These are produced by
the `transform_*` methods and consumed by the finance-sync ingestion
pipeline.

### 5.5 ExportRequest

```python
class ExportRequest(BaseModel):
    format: str = "csv"
    since: datetime | None = None
    account_ids: list[str] | None = None
    options: dict[str, Any] = {}
```

### 5.6 ExportResult

```python
class ExportResult(BaseModel):
    status: str                     # "completed", "failed", "partial"
    records_exported: int = 0
    records_failed: int = 0
    error_message: str | None = None
    export_data: ExportData | None = None
```

---

## 6. Error hierarchy

```
Exception
├── ConnectorError
│   ├── TransientError         # Safe to retry
│   │   └── RateLimitError     # HTTP 429, carries retry_after
│   └── PermanentError         # Must not be retried
└── ExporterError
    ├── ExporterTransientError # Safe to retry
    └── ExporterPermanentError # Must not be retried
```

---

## 7. Rate-limit policy

```python
@dataclass
class RateLimitPolicy:
    max_requests: int = 60
    window_seconds: float = 60.0
    backoff_base: float = 1.0
    backoff_cap: float = 120.0
    max_retries: int = 5
    jitter: float = 0.1
    metadata: dict[str, Any] = {}
```

When a connector sets `rate_limit_policy`, the base class wraps
`fetch_accounts()` and `fetch_transactions()` with automatic rate-limiting
and retry-on-transient-error logic.

---

## 8. Plugin registry

```python
class PluginRegistry:
    def reload(self) -> None: ...
    def register_connector(self, name, cls, replace=False) -> None: ...
    def register_exporter(self, name, cls, replace=False) -> None: ...
    def get_connector(self, config: ConnectorConfig) -> ConnectorPlugin: ...
    def get_exporter(self, name, config=None) -> ExporterPlugin: ...
    def list_connectors(self) -> dict[str, dict[str, Any]]: ...
    def list_exporters(self) -> dict[str, dict[str, Any]]: ...
    @property
    def available_connectors(self) -> list[str]: ...
    @property
    def available_exporters(self) -> list[str]: ...
```

---

## 9. Versioning and compatibility

The `finance-sync-sdk` package follows **Semantic Versioning 2.0**.

- **Major version** (e.g. `1.x.x`): Breaking changes to the ABC interfaces.
- **Minor version** (e.g. `0.1.x`): Backward-compatible additions (new
  models, new optional methods on the ABC).
- **Patch version** (e.g. `0.1.1`): Bug fixes and internal refactors.

Plugins should declare their SDK dependency with an upper-bound:

```toml
dependencies = ["finance-sync-sdk>=0.1,<1.0"]
```

---

## 10. Lifecycle guarantees

1. A plugin instance is **single-use** — the host calls `authenticate()`
   once, then `fetch_accounts()` / `fetch_transactions()` as needed.
2. `authenticate()` **must** be called before any `fetch_*()`.
3. `fetch_accounts()` is called at most once per sync cycle.  Results
   are cached by the host.
4. `fetch_transactions()` may be called multiple times with different
   `account_id` scopes.
5. The host handles all database persistence — plugins never write to
   the finance-sync database directly.
