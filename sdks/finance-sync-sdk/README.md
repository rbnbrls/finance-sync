# finance-sync-sdk

Plugin SDK for [finance-sync](https://github.com/rbnbrls/finance-sync) — build
third-party **connectors** (financial data providers) and **exporters**
(downstream destinations) as separate pip-installable packages.

## Quick start

```bash
pip install finance-sync-sdk
```

### Write a connector plugin

```python
from finance_sync_sdk import ConnectorPlugin, ConnectorConfig, RawAccount, RawTransaction

class MyBankPlugin(ConnectorPlugin):
    display_name = "MyBank"
    config_schema = ConnectorConfig

    @property
    def name(self) -> str:
        return "mybank"

    async def authenticate(self) -> None:
        api_key = self.config.credentials.get("api_key")
        if not api_key:
            raise PermanentError("api_key is required")

    async def fetch_accounts(self) -> list[RawAccount]:
        ...

    async def fetch_transactions(self, since, *, account_id=None, limit=None) -> list[RawTransaction]:
        ...
```

### Register via entry points

In your plugin's `pyproject.toml`:

```toml
[project.entry-points."finance_sync_sdk.plugins"]
mybank = "mybank_finance_sync:MyBankPlugin"
```

## Package contents

| Module | Contents |
|---|---|
| `finance_sync_sdk.plugin` | `ConnectorPlugin` and `ExporterPlugin` base classes |
| `finance_sync_sdk.models` | Pydantic data models (RawAccount, CanonicalAccountData, …) |
| `finance_sync_sdk.config` | Configuration helpers and schema base |
| `finance_sync_sdk.credentials` | Credential management helpers |
| `finance_sync_sdk.rate_limiter` | `RateLimitPolicy`, `RateLimiter` with backoff and jitter |
| `finance_sync_sdk.exceptions` | `ConnectorError`, `TransientError`, `PermanentError`, `RateLimitError` |
| `finance_sync_sdk.registry` | Entry-point-based `PluginRegistry` for discovery |

See the [plugin development guide](https://github.com/rbnbrls/finance-sync/blob/main/docs/plugin-development.md)
for full documentation.

See the [compatibility policy](COMPATIBILITY.md) for versioning guarantees,
deprecation policy, and dependency strategy.
