# Connectors Overview

finance-sync ships with the following built-in connectors. Each connector
subclasses ``finance_sync.connectors.base.Connector`` and is registered
via the ``finance_sync.connectors`` entry point group in ``pyproject.toml``.

| Connector | Provider Type | Description | Authentication |
|---|---|---|---|
| bunq | ``bunq`` | Bunq banking API (v1) | API key |
| Trading212 | ``trading212`` | Trading212 equity API (v0) | API key |
| YNAB | ``ynab`` | You Need A Budget API (v1) | Personal access token |
| CSV Import | ``csv_import`` | Import transactions from CSV files | None (file-based) |
| Manual Expense | ``manual_expense`` | Manual expense tracking via JSON | None (file-based) |
| Plaid-like | ``plaid_like`` | Open banking template (Plaid/TrueLayer/Teller) | Client ID + access token |

## bunq

- **Module:** ``finance_sync.connectors.bunq``
- **Auth:** API key in ``credentials["api_key"]``
- **API:** bunq v1 REST API
- **Rate limit:** 60 req/min
- **Features:** Session-server auth, paginated accounts and payments,
  account type mapping (MonetaryAccountBank → checking,
  MonetaryAccountSavings → savings)
- **Docs:** See module docstring and ``docs/connector-development.md``

## Trading212

- **Module:** ``finance_sync.connectors.trading212``
- **Auth:** API key in ``credentials["api_key"]``
- **API:** Trading212 v0 REST API
- **Rate limit:** 10 req/min (free tier)
- **Features:** Portfolio holdings, order history, dividend/cash
  transaction history, live/demo mode switching
- **Docs:** See module docstring

## YNAB

- **Module:** ``finance_sync.connectors.ynab``
- **Auth:** Personal access token in ``credentials["access_token"]``
- **API:** YNAB v1 REST API (``api.youneedabudget.com/v1``)
- **Rate limit:** 200 req/hour
- **Features:** Budget discovery, account fetching (checking, savings,
  credit), transaction sync with date filtering, category-based
  transaction type mapping, transfer detection, and sign inversion
  (YNAB outflow positive → finance-sync outflow negative)
- **Options:**
  - ``budget_id``: Specific budget to sync (string or budget name)
  - ``base_url``: Custom API base URL (for testing)
- **Docs:** See module docstring

## CSV Import

- **Module:** ``finance_sync.connectors.csv_import``
- **Auth:** None (file-based)
- **Rate limit:** None (file-based)
- **Features:** Single file or directory of CSV files, configurable
  column mapping, date format, delimiter, header/no-header mode,
  multi-file aggregation
- **Options:**
  - ``csv_path``: Path to a single CSV file
  - ``csv_directory``: Directory of CSV files (sorted by name)
  - ``column_mapping``: Dict mapping ``date``, ``description``,
    ``amount``, and optionally ``type`` to CSV column names
  - ``date_format``: strptime format (default: ``%Y-%m-%d``)
  - ``delimiter``: CSV delimiter (default: ``,``)
  - ``has_header``: Whether CSV has a header row (default: True)
  - ``currency``: Currency code (default: ``EUR``)
  - ``account_name``: Display name for the account

## Manual Expense

- **Module:** ``finance_sync.connectors.manual_expense``
- **Auth:** None (file-based)
- **Rate limit:** None (file-based)
- **Features:** JSON file-based expense tracking, categorisation with
  tags, receipt references, recurring expense detection, template
  file creation via ``ManualExpenseConnector.create_template()``
- **Options:**
  - ``data_path``: Path to the JSON expenses file
  - ``default_currency``: Currency code (default: ``EUR``)
  - ``account_name``: Display name for the wallet account

## Plaid-like

- **Module:** ``finance_sync.connectors.plaid_like``
- **Auth:** ``client_id`` + ``access_token`` in credentials
- **Rate limit:** 100 req/min
- **Features:** Token-based auth flow, cursor-based transaction
  pagination, account type normalisation (depository → checking/savings),
  environment switching (sandbox/development/production), mock data in
  sandbox mode for development
- **Options:**
  - ``environment``: ``"sandbox"``, ``"development"``, or ``"production"``
  - ``country_codes``: List of country codes (default: ``["NL", "US"]``)

## Using connectors at runtime

```python
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.connectors.models import ConnectorConfig

registry = ConnectorRegistry()

# List available connectors
print("Available:", registry.available)

# Instantiate a connector
config = ConnectorConfig(
    provider_type="ynab",
    credentials={"access_token": "pat_abc123"},
    options={"budget_id": "my-budget"},
)
connector = registry.get_connector(config)
await connector.authenticate()
accounts = await connector.fetch_accounts()
```

## Writing contract tests

Every connector **must** pass the contract tests defined in
``tests/connectors/contract_test_template.py``.  See the existing
test files for reference:

- ``tests/connectors/ynab/test_ynab_connector.py``
- ``tests/connectors/csv_import/test_csv_import_connector.py``
- ``tests/connectors/manual_expense/test_manual_expense_connector.py``
- ``tests/connectors/plaid_like/test_plaid_like_connector.py``

Contract tests verify:

1. Authentication success, idempotency, and missing-credential handling
2. Health check returns a ``ConnectorHealth`` object
3. ``fetch_accounts()`` returns ``list[RawAccount]``
4. ``fetch_transactions()`` returns ``list[RawTransaction]`` (with
   ``since``, ``account_id``, and ``limit`` parameters)
5. Transform methods map raw data to canonical models
6. ``name`` property matches ``config.provider_type``
7. ``display_name`` and ``sdk_version`` class attributes are set
