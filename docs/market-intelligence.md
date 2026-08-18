# Market Intelligence — self-hosted source layer

This document describes the provider-independent *bronlaag* ("source
layer") for portfolio intelligence: news, corporate events, earnings
material, analyst estimates and earnings-call metadata.  It implements
the story in `backlog/plus-market-intelligence-bronnen.md`.

## Principles

1. **Hermes is never the source of financial facts.**  All observations
   come from providers; Hermes may summarise them later, but the
   canonical link always points back to the original source.
2. **Licence compliance.**  Full copyrighted article text or transcripts
   are only persisted when the source licence explicitly permits it
   (`public_domain` / `open_license`).  Everything else stores metadata,
   a short snippet (character-capped, multi-byte safe) and a canonical
   link.
3. **Explicit unavailability.**  A provider that is down, rate-limited
   or not configured is reported as `unavailable` — never silently
   empty.
4. **Tenant isolation + idempotency.**  Every observation belongs to one
   tenant; re-ingesting a syndicated item is a no-op per tenant.
   Provider outages never delete previously valid data.

## Architecture

```
provider adapters (intel/adapters/*)  →  IntelItem (intel/models.py)
      ↓
IntelIngestionService (intel/service.py)
   - licensing policy (intel/licensing.py)
   - dedupe on (tenant, provider, source_id) + content_hash
   - security-identity resolution via SecurityResolver
   - review queue for ambiguous matches
      ↓
market_intelligence_items / _provider_states / _review_queue (DB)
      ↓
REST (api/v1/market_intelligence.py)  +  MCP tools (mcp/server.py)
```

The scheduler (`intel/scheduler.py`) refreshes each provider on its own
freshness cadence via the worker job `intel_refresh`
(`WORKER_JOB_INTEL_INTERVAL_MINUTES`, default 60).  A provider outage is
isolated per provider (bounded timeout) and can never block bunq,
Trading212 or Wealthfolio syncs — those run on their own scheduler jobs.

## Providers

### SEC EDGAR (`sec`)

| Field | Value |
|---|---|
| Source | US Securities and Exchange Commission, EDGAR system |
| Data | 8-K current reports → `corporate_events` + `earnings` (Item 2.02) |
| Licence | Public domain (17 CFR 200.735-3 and SEC policy) |
| API key | None — requires only a descriptive User-Agent |
| Rate limit | 10 req/s (SEC fair access), honours `Retry-After` on 403/429 |
| Freshness | max-age 24 h, min interval 1 h |
| Config | `INTEL_SEC_ENABLED=false` disables the source entirely |
| Storage | Metadata, headline, structured facts + canonical EDGAR URL; full filing text is never persisted (kept small and privacy friendly) |
| Coverage | US-listed companies with EDGAR CIK; events limited to the 8-K item list in `adapters/sec.py` |
| Disable / delete | Set `INTEL_SEC_ENABLED=false` and restart the worker; delete stored rows with `DELETE FROM market_intelligence_items WHERE provider='sec'` (per tenant) |

### OpenBB Platform (`openbb`)

| Field | Value |
|---|---|
| Source | OpenBB Platform REST API (same endpoint family as the enrichment gateway) |
| Data | `news` (headlines + snippets), optional `earnings` estimates |
| Licence | OpenBB terms of service apply; finance-sync stores only headlines, short snippets and structured facts — never full articles |
| API key | Optional: `OPENBB_API_KEY`.  Without it the provider is degraded and every capability reports `unavailable` |
| Rate limit | `OPENBB_RATE_LIMIT_RPS` (default 10), honours `Retry-After` on 429 |
| Freshness | max-age 6 h, min interval 15 min |
| Config | `OPENBB_API_KEY` (env).  No key = provider registered but disabled |
| Storage | Headline, snippet ≤ 500 chars, structured facts, canonical URL |
| Coverage | Depends on the configured OpenBB backend and key entitlements |
| Disable / delete | Remove `OPENBB_API_KEY` (provider reports unavailable); or delete rows `WHERE provider='openbb'` |

### Future providers (user-subscription sources)

Providers that require a user-owned subscription or API key are only
registered after **explicit configuration** by the user (a settings
flag + secret).  None are shipped yet.  When one is added, this section
must document its provenance, licence terms, configuration, known
coverage and how to disable it / delete its data.

## Licensing policy

`intel/licensing.py` maps a source's reuse class to storage rules:

| Class | Full text | Snippet | Metadata + facts |
|---|---|---|---|
| `public_domain` | ✅ | ✅ | ✅ |
| `open_license` | ✅ | ✅ | ✅ |
| `free_access` | ❌ | ✅ ≤ 500 chars | ✅ |
| `subscriber_only` | ❌ | ✅ ≤ 500 chars | ✅ |
| `proprietary` | ❌ | ❌ | ✅ |

Unknown or deviant license strings (empty, `"copyright (c) 2026"`,
`"CC-BY-NC-4.0"` instead of `"CC BY-NC 4.0"`) are classified as
`proprietary` — the safe default that never persists snippets or full
text.  The snippet cap is enforced in **characters** (not bytes) so
multi-byte content (emoji/CJK) cannot exceed it.

The ingestion service (`apply_licensing_policy`) enforces this again on
every item, and a `body`/`summary` requested for a forbidden class
raises `IntelLicensingError` (a programming error in the adapter).

## Identity resolution

Items carry candidate identifiers (`ticker`/`isin`/`figi`).  The
ingestion service resolves them through the existing
`SecurityResolver` (local DB → OpenBB gateway).  Rules:

- A single unambiguous match → `resolved`, attached to the security.
- Multiple identifiers resolving to **different** securities, or a
  low-confidence match → `ambiguous`; the item is stored **without** a
  holding link and one review-queue entry is created
  (`market_intelligence_review_queue`, unique per
  `(tenant_id, item_id)`, idempotent on re-ingest).

## Ingestion semantics

- **Deduplication**: unique on `(tenant_id, provider, source_id)` and
  `(tenant_id, content_hash)`.  Re-fetching the same syndicated item is
  a no-op.  Deduplication is **per tenant** — two tenants ingesting the
  same press release each get their own observation record.
- **Incremental**: only new items are persisted.
- **Provider outage**: never deletes or invalidates stored data; the
  provider-state row records `unavailable` + a sanitised error, and the
  freshness fields go stale.
- **Partial page failure**: pages are ingested as they arrive, so a 503
  on page 2 keeps page-1 items committed; the run is recorded as
  degraded with the error.
- **Rate limits**: 429s carry `Retry-After`; no request is issued before
  the window expires, and the scheduler does not plan a new run inside
  it.

## Read contracts

### REST

| Endpoint | Description |
|---|---|
| `GET /api/v1/market-intelligence/items` | List observations (filters: provider, kind, review_required; pagination) |
| `GET /api/v1/market-intelligence/items/{id}` | Single observation; cross-tenant id → 404 |
| `GET /api/v1/market-intelligence/providers` | Per-provider run/freshness/availability state (sanitised errors) |
| `GET /api/v1/market-intelligence/review-queue` | Ambiguous-resolution entries awaiting review |

All endpoints are tenant-scoped via the existing JWT/API-key auth
(`market-intelligence:read` permission).  They never return provider
credentials; restricted items never include `body`.

### MCP

- `list_market_intelligence` — list stored observations (tenant-scoped).
- `list_intel_provider_states` — per-provider state, sanitised errors.

## Credential safety

Provider credentials live only in settings / the envelope-encrypted
credential store.  Errors persisted to `last_error` are run through
`redact_text` (which scrubs API keys, tokens, JWTs, long base64/hex
runs and explicit credential values).  Logs, metrics, API responses and
Hermes prompts never contain secrets — `provider_metadata` drops
secret-shaped keys (`api_key`, `token`, `authorization`, …) before
persistence.

## Security-identity of stored content

Item `title`/`body` are stored and served as **data** (JSON-encoded,
never evaluated).  MCP tool names/schemas contain no content-derived
fields.  A prompt built from an observation treats the content as cited
data — it never includes credential values from the envelope.

## Operations

- **Disable a source**: set its env flag (`INTEL_SEC_ENABLED=false`,
  remove `OPENBB_API_KEY`) and restart the worker.  The provider then
  reports `unavailable`; no new rows are written.
- **Delete a source's data**: `DELETE FROM market_intelligence_items
  WHERE provider='...'` (per tenant, e.g. `AND tenant_id=...`), plus
  `market_intelligence_provider_states` and
  `market_intelligence_review_queue` for that provider.
- **Migration**: `0021` (items + provider states), `0022` (review
  queue).  Run `alembic upgrade head`.
