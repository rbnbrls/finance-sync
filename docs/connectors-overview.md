# Connectors Overview

finance-sync ships with the following built-in connectors. Each connector
subclasses ``finance_sync.connectors.base.Connector`` and is registered
via the ``finance_sync.connectors`` entry point group in ``pyproject.toml``.

| Connector | Provider Type | Description | Authentication |
|---|---|---|---|
| bunq | ``bunq`` | Bunq banking API (v1) | API key |
| Trading212 | ``trading212`` | Trading212 equity API (v0) | API key |
| DEGIRO Pensioen | ``degiro_pension`` | Official pension-account export import | None (file-based) |
| SaxoInvestor Excel | ``saxo_investor`` | SaxoInvestor XLSX positions and transactions | None (file-based) |
| YNAB | ``ynab`` | You Need A Budget API (v1) | Personal access token |
| CSV Import | ``csv_import`` | Import transactions from CSV files | None (file-based) |
| Manual Expense | ``manual_expense`` | Manual expense tracking via JSON | None (file-based) |
| Plaid-like | ``plaid_like`` | Open banking template (Plaid/TrueLayer/Teller) | Client ID + access token |

## Environment behavior

- **Multiple connections per provider** — a tenant can hold any number of
  connections for the same provider (e.g. two bunq logins), each with its
  own credentials, label, pause state and account selection.  See
  [connections.md](connections.md) for the full user guide.

- **Staging (`APP_ENVIRONMENT=staging`)** — each bunq and Trading212 card lets
  the user choose between the checked-in synthetic July 2026 dataset and the
  provider's official test environment. bunq Sandbox requires a sandbox API
  key; Trading212 Paper Trading requires its demo API key and API secret. The
  server locks test-API configurations to the official provider hostname, so a
  custom endpoint cannot be injected. `STAGING_CONNECTOR_BASE_URL` identifies
  the internal API used only by the static option.
- **Production (`APP_ENVIRONMENT=prod`)** — bunq and Trading212 are
  user-managed. A user with `connectors:write` supplies the API credentials
  and options in the Connectors dashboard; secrets are stored with the
  existing envelope encryption flow and are never returned by the API.

## bunq

- **Module:** ``finance_sync.connectors.bunq``
- **Auth:** API key in ``credentials["api_key"]``
- **API:** bunq v1 REST API
- **Rate limit:** 60 req/min
- **Features:** Full installation flow by default (signed RSA
  installation/device/session bootstrap — required for every new API key),
  paginated accounts and payments, account type mapping
  (MonetaryAccountBank → checking, MonetaryAccountSavings → savings)
- **Persistent state:** The installation material (client RSA keypair +
  installation token) is stored per tenant in the ``connector_state`` table,
  so repeated syncs reuse the same device instead of registering a new one
  per 15-minute tick (bunq limits devices per API key). Clear the row to
  force a fresh registration.
- **Options:**
  - ``base_url``: Custom API base URL (sandbox/testing)
  - ``full_auth``: Full installation flow (default ``true``); set ``false``
    only for an already-registered installation or static fixtures
  - ``permitted_ips``: IPs for device registration (list, or comma-separated
    string)
- **Docs:** See module docstring and ``docs/connector-development.md``

## Trading212

- **Module:** ``finance_sync.connectors.trading212``
- **Auth:** API key and API secret (HTTP Basic); legacy single-key
  authentication remains supported
- **API:** Trading212 v0 REST API
- **Rate limit:** 10 req/min (free tier)
- **Capabilities:** `accounts`, `transactions`, `holdings`
- **Features:** Portfolio snapshots through the generic holdings pipeline,
  linked security references, order history, dividend/tax/cash transaction
  history, live/demo mode switching
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

## DEGIRO Pensioen

- **Module:** ``finance_sync.connectors.degiro_pension``
- **Auth:** None; the connector only reads files supplied by the user
- **Capabilities:** `accounts`, `transactions`, `holdings`
- **Formats:** CSV (UTF-8/BOM or Windows-1252), XLSX and XLS. PDF is not
  supported.
- **Options:**
  - ``export_paths``: one or more official exports. A standalone portfolio
    export is a valid holdings-and-cash snapshot; upload transaction and
    account-statement exports when transaction history is also required. A
    single ``export_path`` or an ``export_directory`` is also accepted.
  - ``account_key``: optional stable, non-personal value used to derive the
    external account ID. When omitted, a hash of the configured paths is used.
  - ``account_name``: display name (default ``DEGIRO Pensioen``)
  - ``snapshot_at``: optional portfolio observation time; otherwise the
    portfolio file's modification time is used
- **Upload/watchfolder guide:** See ``docs/degiro-imports.md``
- **Wealthfolio end-to-end guide:** See ``docs/degiro-wealthfolio.md``

### Exporting from DEGIRO

1. Log in to the official DEGIRO platform or app and select the pension
   account.
2. Open **Inbox > Transacties**, set the required start and end dates, use the
   export button, and download CSV or Excel.
3. Open **Inbox > Rekeningoverzicht**, set the same period, use the export
   button, and download CSV or Excel.
4. Open **Portefeuille**, select the export button on the right (the downward
   arrow in the app), select the snapshot date, and download CSV or Excel.
5. Upload ``Portfolio.csv`` on its own for a holdings-and-cash snapshot, or
   configure all three files in ``export_paths`` for a complete history. Keep
   the portfolio export together with its original modification timestamp, or
   set ``snapshot_at`` explicitly.

These locations and formats follow DEGIRO's current
[reporting instructions](https://www.degiro.nl/helpdesk/belasting/welke-rapportagemogelijkheden-zijn-er-en-waar-kan-ik-de-rapportages-vinden).

Report types are detected from their headers and structure, not their file
names. Securities and cash are processed separately. ``current_balance`` is
the sum of the portfolio's EUR market values and cash row(s), while
``available_balance`` is cash only. An empty portfolio therefore imports with
a zero balance and no holdings.

DEGIRO does not publish a supported customer API. finance-sync therefore never
asks for a DEGIRO username, password or 2FA secret and does not log in, automate
a browser, manage cookies, or call private endpoints. Imports are snapshots:
users must periodically create and supply new exports. PDF, pending orders and
live prices are not supported. Malformed rows fail the complete atomic sync and
are exposed through the connector's validation report.

## SaxoInvestor Excel

- **Module:** ``finance_sync.connectors.saxo_investor``
- **Auth:** None; the connector reads a user-supplied XLSX export
- **Capabilities:** ``accounts``, ``holdings``, ``transactions``
- **Input:** the SaxoInvestor **Posities** export, the **Transactions** export,
  or both files together. The importer detects the file type from its Dutch
  headers and accepts the non-standard style XML produced by Saxo's download flow.
- **Identity:** ISIN is the primary security identifier; Saxo's symbol and
  venue are retained as provider metadata.
- **Snapshot:** ``snapshot_at`` overrides the timestamp. Otherwise a date in
  a filename such as ``Posities_23-aug-2026.xlsx`` is used, falling back to
  the file modification time.
- **Options:** ``account_key`` (stable default ``default``), ``account_name``
  (default ``SaxoInvestor``), and optional ``snapshot_at``. In the dashboard
  files are uploaded for each import and removed after processing; self-hosted
  integrations may still provide ``export_path`` or ``export_paths``.

In the dashboard the user selects one or both files with one upload action.
The import creates or updates the single brokerage account and writes the
available holdings and transaction rows atomically. A positions-only upload
does not invent transaction history; a transactions-only upload leaves the
current account balance unchanged. Saxo's ``Huidige waarde (EUR)`` is treated as the market-value currency;
the instrument currency from ``Valuta`` is retained separately for price and
cost-basis fields. The supplied sample contains nine positions with a total
reported market value of EUR 37,007.97.

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

### Dashboard importflow

De gebruikersflow start op één pagina, **Importeren**. De connectorcatalogus
publiceert per connector de secret-safe velden `ingestion_methods` (`api`,
`file` of beide) en optionele `import_wizard`-metadata. De UI gebruikt die
metadata om eerst de tegenpartij en daarna de ingestiemethode te laten kiezen.

Bestandsimports lopen voor nieuwe clients via:

- `POST /api/v1/connectors/file-uploads/dispatch`;
- `POST /api/v1/connectors/file-uploads/dispatch/{run_id}/confirm` voor
  confirmable previews;
- `GET /api/v1/connectors/file-uploads/runs` voor de tenant-scoped historie.

De bestaande DEGIRO-, Saxo- en generieke endpoints blijven tijdelijk bestaan
als backwards-compatible adapters. De connector zelf blijft verantwoordelijk
voor provider-specifieke validatie; de dashboardflow beheert alleen de
gemeenschappelijke wizardstappen.

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
