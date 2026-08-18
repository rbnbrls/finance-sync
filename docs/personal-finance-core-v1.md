# Personal Finance Core v1

## Status

**Proposed contract.** It is the target stable data contract for finance-sync
connectors, projections, events, and downstream datamarts. It does not change
the existing database or public API until the migration plan below is executed.

## 1. Scope and boundaries

Personal Finance Core (PFC) is the provider-independent financial-truth layer
for a person or household. It owns personal accounts, cash, investment
activity, positions, instrument identity, and source provenance.

PFC is intentionally independent of provider DTOs and of OpenBB. A connector
maps its provider records into PFC commands; no provider-shaped field is part
of this contract. OpenBB is an optional market-data enrichment adapter. It may
resolve an `Instrument` or `Listing`, and may create price, FX, or fundamental
observations, but it cannot create or alter a person's balances, transactions,
activities, positions, or source provenance.

All PFC APIs and events use this envelope:

```json
{
  "schema_version": "pfc/1.0",
  "id": "uuid",
  "tenant_id": "uuid",
  "source": { "source_record_id": "uuid" },
  "observed_at": "2026-08-18T00:00:00Z"
}
```

Money and quantities are decimal strings, never floats. Timestamps are UTC
RFC 3339 instants. Currency codes are ISO 4217 and market identifiers use ISIN,
FIGI, and ISO 10383 MIC where available.

## 2. Contract rules

1. **Facts are append-only or revisioned.** A provider correction creates a
   later revision with the same stable source identity; it never silently
   overwrites a historical observation.
2. **The source of a fact is mandatory.** Every PFC fact has a `SourceRecord`.
   Source payloads stay outside normal consumer responses.
3. **Identity is local and stable.** PFC UUIDs are internal; `source_key` is
   the idempotency key within a connection and resource type.
4. **Economic instrument and tradable listing differ.** An Instrument can have
   multiple Listings; a holding links to an Instrument and, when known, its
   Listing.
5. **Cash movement and investment activity differ.** A broker trade may create
   an `InvestmentActivity` and one or more `CashTransaction` facts. Consumers
   may use either view without inferring a trade from a text description.
6. **A snapshot is an observation, not current mutable state.** Current
   balances and positions are projections selecting the latest valid snapshot.
7. **Provider extensions are namespaced.** Non-portable values live only in
   `source.provider_attributes`, keyed by provider and schema version; they are
   not promoted into PFC fields without a contract change.

## 3. Canonical entities

### 3.1 Account

An account is a custody or cash relationship owned by one tenant.

```json
{
  "kind": "account",
  "id": "uuid",
  "tenant_id": "uuid",
  "account_type": "current|savings|cash|brokerage|pension|credit|loan|other",
  "name": "DEGIRO Pensioen",
  "base_currency": "EUR",
  "status": "active|closed",
  "opened_at": "2024-01-01T00:00:00Z",
  "closed_at": null,
  "source": { "source_record_id": "uuid" }
}
```

An account must not store the current balance as authoritative state.

### 3.2 BalanceSnapshot

A point-in-time balance for an Account.

```json
{
  "kind": "balance_snapshot",
  "id": "uuid",
  "account_id": "uuid",
  "balance_kind": "booked|available|current|credit_limit",
  "amount": "9587.44",
  "currency": "EUR",
  "observed_at": "2026-08-18T00:00:00Z",
  "source": { "source_record_id": "uuid" }
}
```

Unique identity: `(account_id, balance_kind, observed_at, source_record_id)`.

### 3.3 CashTransaction

An independently visible movement of cash on an Account.

```json
{
  "kind": "cash_transaction",
  "id": "uuid",
  "account_id": "uuid",
  "activity_id": "uuid-or-null",
  "direction": "inflow|outflow",
  "amount": "1425.00",
  "currency": "EUR",
  "occurred_at": "2026-08-18T10:00:00Z",
  "booked_at": "2026-08-18T10:00:00Z",
  "status": "pending|booked|reversed|cancelled",
  "category": "transfer|payment|fee|interest|tax|deposit|withdrawal|other",
  "counterparty": { "name": "optional", "reference": "optional" },
  "source": { "source_record_id": "uuid", "source_key": "provider-id" }
}
```

`amount` is always non-negative; `direction` supplies the sign. This avoids
different sign conventions across providers.

### 3.4 InvestmentActivity

An investment-domain event, separate from its cash settlement.

```json
{
  "kind": "investment_activity",
  "id": "uuid",
  "account_id": "uuid",
  "instrument_id": "uuid",
  "listing_id": "uuid-or-null",
  "activity_type": "buy|sell|dividend|withholding_tax|fee|interest|split|transfer_in|transfer_out|corporate_action|other",
  "quantity": "100",
  "unit_price": { "amount": "14.25", "currency": "EUR" },
  "gross_amount": { "amount": "1425.00", "currency": "EUR" },
  "fee_amount": { "amount": "0.00", "currency": "EUR" },
  "occurred_at": "2026-08-18T10:00:00Z",
  "settled_at": null,
  "status": "pending|booked|cancelled",
  "source": { "source_record_id": "uuid", "source_key": "provider-order-id" }
}
```

Fields irrelevant to an activity type are `null`, not invented as zero.

### 3.5 HoldingSnapshot

A point-in-time quantity and valuation of an Instrument held in an Account.

```json
{
  "kind": "holding_snapshot",
  "id": "uuid",
  "account_id": "uuid",
  "instrument_id": "uuid",
  "listing_id": "uuid-or-null",
  "quantity": "100",
  "cost_basis": { "amount": "1200.00", "currency": "EUR" },
  "market_value": { "amount": "1425.00", "currency": "EUR" },
  "price": { "amount": "14.25", "currency": "EUR" },
  "observed_at": "2026-08-18T00:00:00Z",
  "source": { "source_record_id": "uuid" }
}
```

Unique identity: `(account_id, instrument_id, listing_id, observed_at,
source_record_id)`.

### 3.6 CashPositionSnapshot

Cash held in an investment account, distinct from the account's general
balance snapshot so portfolios can be valued by currency.

```json
{
  "kind": "cash_position_snapshot",
  "id": "uuid",
  "account_id": "uuid",
  "currency": "EUR",
  "amount": "9587.44",
  "observed_at": "2026-08-18T00:00:00Z",
  "source": { "source_record_id": "uuid" }
}
```

### 3.7 Instrument and Listing

`Instrument` represents the economic security. `Listing` represents the
specific venue/currency symbol used to trade it.

```json
{
  "kind": "instrument",
  "id": "uuid",
  "instrument_type": "equity|etf|fund|bond|option|future|crypto|currency|cash_fund|other",
  "name": "Alfen NV",
  "identifiers": { "isin": "NL0012817175", "figi": null, "cusip": null },
  "source": { "source_record_id": "uuid" }
}
```

```json
{
  "kind": "listing",
  "id": "uuid",
  "instrument_id": "uuid",
  "ticker": "ALFEN",
  "mic": "XAMS",
  "currency": "EUR",
  "is_primary": true,
  "source": { "source_record_id": "uuid" }
}
```

Identifiers are unique when non-null. An unresolved provider security becomes
an `UnresolvedInstrument` work item and must not be merged by ticker alone.

### 3.8 SourceRecord

`SourceRecord` supplies lineage for every fact and supports safe replay.

```json
{
  "kind": "source_record",
  "id": "uuid",
  "tenant_id": "uuid",
  "connection_id": "uuid",
  "provider": "bunq|trading212|degiro_pension|plugin-key",
  "resource_type": "accounts|transactions|activities|holdings|portfolio_file",
  "source_key": "provider-stable-id-or-content-hash",
  "source_schema_version": "provider-format-version",
  "fetched_at": "2026-08-18T00:05:00Z",
  "observed_at": "2026-08-18T00:00:00Z",
  "payload_fingerprint": "sha256",
  "payload_retention": "none|encrypted",
  "provider_attributes": { "provider-scoped": "non-contract-data" }
}
```

`(tenant_id, connection_id, resource_type, source_key)` is unique. Retained
payload bytes, when enabled, must be AES-256-GCM encrypted and linked by this
record; they are not exposed through a PFC datamart.

## 4. Events and datamarts

Events are facts, not provider payloads. Their envelope is:

```json
{
  "event_version": "pfc-event/1.0",
  "event_id": "uuid",
  "tenant_id": "uuid",
  "subject_type": "account|cash_transaction|investment_activity|holding_snapshot",
  "subject_id": "uuid",
  "event_type": "pfc.cash_transaction.recorded",
  "occurred_at": "2026-08-18T10:00:00Z",
  "correlation_id": "sync-run-uuid",
  "data_version": "pfc/1.0"
}
```

Datamarts are read-only projections of PFC facts. They contain a declared
schema version, account scope, and permitted data classes. Examples are
`cash-ledger-v1`, `portfolio-v1`, `net-worth-v1`, and `tax-lots-v1`. A
consumer receives only its authorized datamarts and account scope; it never
receives credentials, raw payloads, or another consumer's cursor.

## 5. Versioning and compatibility

- Contract labels use `pfc/<major>.<minor>`.
- A minor version may add optional fields, values, and event types only.
- A major version may remove or change semantics and requires a parallel
  datamart/event stream plus an explicit consumer migration window.
- Every source mapping declares the PFC version it produces; every datamart
  declares the versions it accepts.
- Unknown optional fields must be retained by storage adapters where possible
  and ignored by consumers. Unknown required major versions must fail closed.

## 6. Migration plan

### Phase 0 — Lock the contract

Approve this document as an ADR-backed `pfc/1.0` contract. Add JSON Schema or
Pydantic models in a new `finance_sync.pfc` package, plus compatibility tests
for serialized fixtures. No database behavior changes.

### Phase 1 — Establish lineage and temporal invariants

Add tenant-scoped `source_records`, tenant-scoped `sync_runs`, and
tenant-scoped outbox events. Link existing accounts, transactions, balances,
and holdings to source records. Add encrypted raw-payload storage only when
the configured retention policy requires it.

### Phase 2 — Introduce parallel PFC facts

Create PFC tables and write adapters from the existing canonical models:

| Existing model | PFC target |
|---|---|
| `Account` | `Account` |
| `Balance` | `BalanceSnapshot` |
| `Transaction` | `CashTransaction` plus optional `InvestmentActivity` |
| `Holding` | `HoldingSnapshot` |
| `Security` / `SecurityListing` | `Instrument` / `Listing` |

Run dual writes behind a feature flag and reconcile daily. Existing REST and
export contracts remain unchanged.

### Phase 3 — Normalize source mappings

Make each connector produce PFC commands via a dedicated mapper. Bunq maps to
accounts, balance snapshots, and cash transactions; Trading 212 maps to
accounts, cash transactions, investment activities, holdings, and cash
positions; DEGIRO Portfolio uploads map to holdings and cash positions only.
Provider metadata remains attached solely through `SourceRecord`.

### Phase 4 — Publish governed datamarts

Create `DataMart`, `DataMartGrant`, and consumer-delivery cursor models.
Expose versioned pull APIs and event feeds. Migrate Actual Budget to
`cash-ledger-v1` and Wealthfolio to `portfolio-v1`; verify that grants restrict
both account scope and data class.

The first policy slice is implemented: datamart, consumer, and grant storage;
administrator APIs; and a consumer-bound effective-policy endpoint. See
[Governed datamarts](datamarts.md). Delivery adapters must adopt this policy
before they are considered governed.

### Phase 5 — Cut over and retire legacy semantics

Switch read services and exporters to PFC projections. Deprecate ambiguous
investment uses of generic `Transaction` only after a full retention window,
successful reconciliation, and downstream consumer acknowledgement.

## 7. Definition of done

- Every new connector maps to PFC commands with no provider DTO beyond the
  connector boundary.
- A PFC fact can be traced to one source record and sync run without exposing
  its raw payload.
- Replaying a source record is idempotent and produces the same fact identity
  or a documented revision.
- A consumer can access only explicitly granted, versioned datamarts.
- OpenBB failures cannot block ingestion of personal financial facts.
- Contract, migrations, connector mappings, event envelopes, and datamart
  grants have unit, integration, and end-to-end compatibility tests.
