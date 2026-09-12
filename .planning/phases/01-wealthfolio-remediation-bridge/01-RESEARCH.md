# Phase 1: Wealthfolio remediation bridge - Research

**Researched:** 2026-09-12
**Domain:** Python async Wealthfolio integration, data-quality remediation, and tenant-safe worker orchestration
**Confidence:** MEDIUM

## Summary

The repository already has most of the durable machinery this phase needs: a tenant-scoped remediation backlog with a unique deduplication constraint, lease-based claiming, bounded retry states, verification metadata, APScheduler persistence, per-tenant worker isolation, encrypted destination secrets, and existing Wealthfolio quote/export helpers. [VERIFIED: src/finance_sync/models/remediation.py:24-131] — quote: `__tablename__ = "data_quality_remediation_items"` and `UniqueConstraint("tenant_id", "deduplication_key", name="uq_dq_remediation_tenant_dedup")`; [VERIFIED: src/finance_sync/reconciliation/remediation/backlog.py:22-23] — quote: `ACTIVE_STATUSES = ("pending", "retry_wait", "deferred")` and `TERMINAL_STATUSES = ("resolved", "failed", "ignored", "manual_review")`; [VERIFIED: src/finance_sync/worker/scheduler.py:282-289] — quote: `"coalesce": True`, `"max_instances": 1`; [VERIFIED: src/finance_sync/models/export_target.py:44-110] — quote: `ExportTarget`, `encrypted_secret`, `secret_nonce`, `last_health_status`, `last_health_error`, and `last_checked_at`.

Wealthfolio’s current server source confirms that `/api/v1/health/status` is a read endpoint that returns cached health when fresh and runs checks when stale. Its health fix endpoint accepts a fix action; `sync_prices` and `retry_sync` take a list of opaque Wealthfolio asset IDs, run incremental quote sync, and clear the health cache. [CITED: https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs] The public Addon API likewise requires opaque asset IDs for market, asset, and quote APIs, and documents targeted market sync, quote history reads, and quote updates. [CITED: https://wealthfolio.app/docs/addons/api-reference/]

The existing phase plan is directionally correct but stale/incomplete for implementation planning. It assumes the `ExportTarget` credential path without identifying the worker-side decryption/target lookup seam, proposes a new cursor table without resolving whether existing target health columns are sufficient, and treats `issues`, `fixAction`, and `affectedItems` as known response fields even though the public route source does not document the complete JSON shape. [VERIFIED: .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:11-22] — quote: `Reuse the encrypted Wealthfolio password already stored in ExportTarget`; [VERIFIED: .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:57-61] — quote: `Add a tenant-scoped wealthfolio_health_cursors table keyed by target.`; [VERIFIED: .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:79-84] — quote: `Parse issues, fixAction, affectedItems, severity, code, and bounded details from Wealthfolio.` These must become explicit plan checkpoints and fixtures.

**Primary recommendation:** Build one target-scoped bridge service around the existing `WealthfolioClient`, `ExportTarget`, `BacklogRepository`, remediation strategies, and worker scheduler; do not create a second queue, second credential store, or second quote writer.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Authenticated Wealthfolio health polling | API / Backend | Database / Storage | The worker owns remote authentication and bounded HTTP calls; durable state records poll outcomes per tenant and target. [VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:134-177] — quote: `class WealthfolioClient` and `self._client = httpx.AsyncClient(**kwargs)`; [VERIFIED: src/finance_sync/models/export_target.py:60-110] — quote: `tenant_id`, `target_type`, `encrypted_secret`, `last_checked_at`. |
| Health payload normalization and issue classification | API / Backend | — | Parsing, allow-list mapping, sanitization, and fail-closed routing are business logic and must not depend on UI projections. [VERIFIED: src/finance_sync/reconciliation/remediation/backlog.py:61-74] — quote: `class DetectedIssue` with `issue_type`, `affected_entity_type`, `affected_entity_id`, `remediation_strategy`, and `context`. |
| Deduplicated remediation backlog | Database / Storage | API / Backend | PostgreSQL uniqueness and transactional upsert provide cross-poll/restart idempotency; the service supplies stable keys and tenant scope. [VERIFIED: src/finance_sync/models/remediation.py:31-64] — quote: `UniqueConstraint("tenant_id", "deduplication_key", name="uq_dq_remediation_tenant_dedup")`. |
| Canonical quote and historical-price repair | API / Backend | Database / Storage | EnrichmentGateway/PriceStore update canonical finance-sync prices; the Wealthfolio adapter projects selected results remotely. [VERIFIED: src/finance_sync/reconciliation/remediation/quote.py:34-50] — quote: `self.gateway = EnrichmentGateway(...)` and `await self.gateway.get_latest_quote(...)`; [VERIFIED: src/finance_sync/reconciliation/remediation/price_history.py:45-65] — quote: `await self.gateway.get_historical_prices(...)`. |
| Remote repair verification | API / Backend | — | A repair is not complete until a subsequent authenticated health response no longer reports the issue; canonical row existence alone is insufficient. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:157-181] — quote: `verification = await strategy.verify(item)` followed by `status = ("resolved" if verification.resolved else ...)`. |
| Manual trigger and Data Health projection | API / Backend | Browser / Client | Existing control-plane routes already enforce permissions and tenant context; UI should consume the existing response contract. [VERIFIED: src/finance_sync/api/v1/control_plane.py:64-99] — quote: `auth: AuthContext = Depends(require_permission("sync", "read"))` and `auth: AuthContext = Depends(require_permission("enrichment", "write"))`. |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | `>=3.12` | Runtime | Project-declared runtime floor. [VERIFIED: pyproject.toml:1-7] — quote: `requires-python = ">=3.12"`. |
| FastAPI | `0.136.3` locked | Control-plane API and dependency-based authorization | Already the application framework. [VERIFIED: pyproject.toml:12-24; uv.lock:838-840] — quote: `"fastapi>=0.115"` and `name = "fastapi"` / `version = "0.136.3"`. |
| SQLAlchemy async | `2.0.51` locked | Target state, backlog, and transactional persistence | Existing async ORM and migration model layer. [VERIFIED: pyproject.toml:17-18; uv.lock:2907-2909] — quote: `"sqlalchemy[asyncio]>=2.0"` and `version = "2.0.51"`. |
| Alembic | `1.19.1` locked | Schema migration for bridge state | Existing migration chain is deployed before app/worker startup. [VERIFIED: pyproject.toml:19; uv.lock:164-166] — quote: `"alembic>=1.14"` and `version = "1.19.1"`; [VERIFIED: coolify.yaml:165-167] — quote: `The Compose \`migrate\` service applies the schema before the app and worker start.` |
| httpx | `0.28.1` locked | Authenticated Wealthfolio HTTP client | Existing `WealthfolioClient` uses an async HTTP client with cookie-backed login. [VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:134-177; uv.lock:1256-1258] — quote: `self._client = httpx.AsyncClient(**kwargs)` and `version = "0.28.1"`. |
| APScheduler | `3.11.3` locked | Persistent worker scheduling | Existing scheduler uses a PostgreSQL job store, UTC timezone, coalescing, and one active instance. [VERIFIED: src/finance_sync/worker/scheduler.py:270-289; uv.lock:209-211] — quote: `SQLAlchemyJobStore`, `timezone="UTC"`, `"max_instances": 1`. |
| Pydantic / pydantic-settings | `2.13.4` / `2.15.0` locked | Bounded response/config schemas | Existing settings and API schema conventions. [VERIFIED: pyproject.toml:22-23; uv.lock:2325-2327; uv.lock:2415-2417] — quote: `"pydantic>=2.10"`, `"pydantic-settings>=2.7"`, and the locked versions. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| prometheus-client | `0.26.0` locked | Bridge poll/issue/repair metrics | Extend the existing remediation metric family; avoid high-cardinality target IDs. [VERIFIED: src/finance_sync/observability/metrics.py:120-173; uv.lock:2119-2121] — quote: `data_quality_remediation_throughput_total` and `data_quality_remediation_manual_review_size`. |
| structlog | `26.1.0` locked | Sanitized structured worker logs | Use event names and bounded identifiers already used by the worker. [VERIFIED: src/finance_sync/worker/jobs.py:55-56; uv.lock:2993-2995] — quote: `logger = structlog.get_logger("finance_sync.worker.jobs")`. |
| Redis | `redis[hiredis]>=5.2` | Remediation quota/cooldown coordination | Reuse only for rate-limit coordination; PostgreSQL remains the durable source of backlog truth. [VERIFIED: pyproject.toml:20-21; src/finance_sync/worker/jobs.py:953-958] — quote: `RemediationRateLimitCoordinator(...)` and the conditional `container.redis_client`. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Existing `BacklogRepository` | A new bridge-specific queue/table | Do not use the alternative: it duplicates durable lifecycle, leasing, deduplication, and metrics already present. [VERIFIED: src/finance_sync/reconciliation/remediation/backlog.py:94-173] — quote: `class BacklogRepository` and `on_conflict_do_update`. |
| Target-scoped encrypted `ExportTarget` secret | Global `WEALTHFOLIO_PASSWORD` only | Do not use the global-only path for this bridge: the target model is tenant-scoped and encrypted, while global settings are the legacy export configuration. [VERIFIED: src/finance_sync/models/export_target.py:44-110] — quote: `tenant_id`, `encrypted_secret`, `secret_nonce`; [VERIFIED: src/finance_sync/config/settings.py:404-415] — quote: `WEALTHFOLIO_SERVER_URL` and `WEALTHFOLIO_PASSWORD`. |
| Targeted Wealthfolio quote sync | Full exporter run per issue | Do not use full export as the repair primitive: upstream exposes targeted market sync by asset IDs, and the current client already has idempotent quote upsert. [CITED: https://wealthfolio.app/docs/addons/api-reference/]; [VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:878-911] — quote: `get_quote_history`, `delete_quote`, and `upsert_quote`. |

**Installation:** No new external package is needed for this phase; all recommended capabilities are already declared and locked in the Python project. [VERIFIED: pyproject.toml:12-38; uv.lock:164-166,209-211,838-840,1256-1258,2119-2121,2325-2327,2415-2417,2907-2909,2993-2995]

## Package Legitimacy Audit

No external packages are added by the research recommendation, so the package legitimacy gate is not applicable. The implementation should reuse the existing locked dependencies rather than introduce a new Wealthfolio SDK or retry library. [VERIFIED: pyproject.toml:12-38]

## Architecture Patterns

### System Architecture Diagram

```text
APScheduler poll tick / authorized API trigger
                    |
                    v
       active tenant ExportTarget rows
       (decrypt target secret in-memory only)
                    |
                    v
     WealthfolioClient.authenticate()
                    |
                    v
       GET /api/v1/health/status
          | success                 | auth/transient/schema failure
          v                         v
  bounded HealthSnapshot       sanitized target error state
          |                         |
          v                         v
  normalize + allow-list       retry/backoff/metrics; do not
  issue categories              reconcile missing issues
          |
          v
 BacklogRepository.register()
 (tenant + stable dedup key)
          |
          v
 existing remediation claim/lease executor
       | safe quote/history     | unsafe/unknown
       v                        v
 canonical EnrichmentGateway  manual_review with context
       |
       v
 Wealthfolio targeted sync / quote projection
       |
       v
 fresh GET /api/v1/health/status
       | issue absent => resolved
       | issue present/failure => retry_wait or manual_review
```

Wealthfolio’s server source explicitly defines the health route family and shows that health status can be cached or freshly computed. [CITED: https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs] The repository’s executor already follows the required execute-then-verify transition, so the bridge-specific strategy should make remote health verification part of `verify()` rather than setting `resolved` during enqueue or projection. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:157-181]

### Recommended Project Structure

```text
src/finance_sync/
├── exporter/wealthfolio/client.py          # health/status and targeted sync client methods
├── models/wealthfolio_health.py             # target-scoped durable poll state
├── services/wealthfolio_health_bridge.py    # poll, normalize, enqueue, reconcile
├── reconciliation/remediation/wealthfolio.py # target-aware quote/history strategy + remote verify
├── worker/jobs.py                           # bounded poll job and existing remediation job integration
├── worker/scheduler.py                      # feature-gated persistent job registration
├── api/v1/control_plane.py                  # authorized bounded manual trigger
├── schemas/data_health.py                   # bridge summary projection
└── observability/metrics.py                # low-cardinality bridge metrics
```

The exact new filenames are recommendations, not existing discrete values. The current code boundaries supporting them are verified by the existing client, model, remediation, worker, control-plane, schema, and metrics files cited above.

### Pattern 1: Target-scoped polling with durable success boundary

**What:** Load only active Wealthfolio targets for one tenant, decrypt the target secret in memory, authenticate, fetch one bounded health response, persist `last_successful_poll`/hash/error metadata, and only then reconcile disappearance of previously active issues. [VERIFIED: src/finance_sync/api/v1/destinations.py:621-630,664-668] — quote: `encrypted_secret=ciphertext` and `row.encrypted_secret, row.secret_nonce = encrypt_credential(...)`; [VERIFIED: src/finance_sync/services/wealthfolio_preflight.py:103-147] — quote: `The caller owns client construction and secret handling.`

**When to use:** Every scheduled or manual poll. A failed authentication, timeout, rate limit, malformed body, or partial response must update bounded error state but must not mark absent issues as resolved. [VERIFIED: .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:94-99] — quote: `A missing issue is not resolved when the latest poll failed.`

**Example:**

```python
async def poll_target(target: ExportTarget) -> PollResult:
    client = await build_target_client(target)  # decrypt only in memory
    try:
        await client.authenticate()
        payload = await client.get_health_status()
        snapshot = normalize_health(payload, target_id=str(target.id))
        await persist_success(target, snapshot)
        await enqueue_and_reconcile(snapshot)
        return PollResult(status="success", issue_count=len(snapshot.issues))
    except TransientWealthfolioError as exc:
        await persist_failure(target, sanitize_error(exc))
        return PollResult(status="retry_wait")
```

The function names and `PollResult` are illustrative; the lifecycle values `retry_wait`, `manual_review`, and `resolved` are existing executor/backlog values. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:104-117,162-181] — quote: `status="manual_review"`, `status = ("resolved" if verification.resolved else ... "retry_wait")`.

### Pattern 2: Stable issue identity plus bounded context

**What:** Normalize each affected remote entity to one `DetectedIssue`; make deduplication depend on tenant, target/connection, issue type, entity type/id, and stable scope while excluding volatile payload details. Store only sanitized identifiers, category, action, bounded dates, and actionable error context. [VERIFIED: src/finance_sync/reconciliation/remediation/backlog.py:77-91] — quote: `run IDs and volatile context are excluded` and the `dq:v1` key components; [VERIFIED: src/finance_sync/models/remediation.py:24-29] — quote: `Provider credentials and raw provider responses must never be placed in context.`

**When to use:** Every poll, including repeated identical responses and responses with multiple affected assets. Do not use the whole JSON payload or a timestamp in the deduplication key.

### Pattern 3: Canonical-first, projection-second, remote verification-last

**What:** Fetch or fill canonical finance-sync prices through the existing enrichment gateway, project only safe quote data to Wealthfolio using the existing client contract, then perform a fresh remote health read before changing the backlog status. [VERIFIED: src/finance_sync/reconciliation/remediation/quote.py:44-50; src/finance_sync/reconciliation/remediation/price_history.py:55-66] — quote: `await self.gateway.get_latest_quote(...)` and `await self.gateway.get_historical_prices(...)`; [CITED: https://wealthfolio.app/docs/addons/api-reference/] — quote source documents `market.sync(assetIds, ...)` and `quotes.update(assetId, quote)`.

**When to use:** Quote-sync and bounded historical-price issues only. Missing purchase prices, negative valuations, transaction gaps, unknown categories, and unresolved identity mappings must fail closed to manual review. [VERIFIED: src/finance_sync/services/wealthfolio_preflight.py:151-179] — quote: `Missing cost basis is a warning` and `non-zero holding has neither market_value nor price` is quarantined.

### Anti-Patterns to Avoid

- **Resolve on local success:** Canonical price-store success does not prove Wealthfolio health is clear; the existing executor’s `verify()` boundary must include the remote check. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:157-181]
- **Treat a health response as a stable schema without fixtures:** The upstream route source exposes `HealthStatus` but not its full serialized fields; treat the exact response shape as an integration contract to capture from a supported target/version. [CITED: https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs]
- **Use ticker as remote identity:** Wealthfolio’s current docs say `assetId` is opaque and not a ticker. [CITED: https://wealthfolio.app/docs/addons/api-reference/]
- **Reconcile absence after a failed or partial poll:** Only a complete successful response can establish that a previously seen issue is absent. [VERIFIED: .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:94-99]
- **Leak remote payloads or secrets:** Existing model and preflight contracts explicitly prohibit raw responses and credentials in remediation context/logs. [VERIFIED: src/finance_sync/models/remediation.py:24-29; src/finance_sync/services/wealthfolio_preflight.py:103-107]

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Durable deduplication and leases | A bridge-local queue or in-memory set | `BacklogRepository.register()` and `claim()` | Existing PostgreSQL uniqueness, upsert, and lease semantics survive restarts and concurrent workers. [VERIFIED: src/finance_sync/reconciliation/remediation/backlog.py:101-173,223-258] |
| Canonical quote acquisition | A Wealthfolio-specific provider fetcher | `EnrichmentGateway` + `PriceStore` through existing strategies | Existing quote and historical strategies already encode identifier, interval, date-window, and tenant-held-security checks. [VERIFIED: src/finance_sync/reconciliation/remediation/quote.py:29-99; src/finance_sync/reconciliation/remediation/price_history.py:29-147] |
| Remote quote identity/upsert | A second quote payload writer | `WealthfolioClient.get_quote_history()`, `delete_quote()`, and `upsert_quote()` | Existing client removes connector-owned same-day rows before PUT, providing idempotent projection behavior. [VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:878-911] |
| Credential storage | Plaintext bridge secret table or env-only target state | Encrypted `ExportTarget.encrypted_secret` / `secret_nonce` | Target configuration is tenant-scoped and existing destination code encrypts secrets before persistence. [VERIFIED: src/finance_sync/models/export_target.py:60-110; src/finance_sync/api/v1/destinations.py:616-630] |
| Retry/circuit behavior | Ad hoc sleeps in each strategy | Existing remediation retry policy, rate-limit coordinator, and worker retry helper | Centralized retry state is already observable and bounded. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:90-95,204-255; src/finance_sync/worker/jobs.py:61-92] |

**Key insight:** The hard problem is not issuing another HTTP GET; it is preserving a safe, tenant-scoped state machine across remote cache freshness, provider failures, canonical writes, and verification. Existing backlog/executor primitives should remain the single lifecycle authority.

## Common Pitfalls

### Pitfall 1: Health payload contract drift

**What goes wrong:** A parser assumes field names or nesting from an early Wealthfolio build and silently drops affected assets or misclassifies issues.

**Why it happens:** The official server route types the response as `HealthStatus`, but the public route source does not show the full serialized schema. [CITED: https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs]

**How to avoid:** Capture sanitized fixtures from the supported deployment version; parse tolerant aliases only where observed; reject malformed or unknown critical shapes into a visible `manual_review`/bridge error rather than treating them as healthy. [ASSUMED]

**Warning signs:** A successful HTTP 200 with zero normalized issues when the Wealthfolio UI still shows findings; unknown category count increasing; no affected entity IDs in backlog context.

### Pitfall 2: Wrong credential boundary

**What goes wrong:** The worker uses global `WEALTHFOLIO_PASSWORD` and applies one destination to every tenant, or cannot decrypt target credentials in the worker.

**Why it happens:** The current legacy export job reads settings-level Wealthfolio URL/password, while the destination API stores per-tenant encrypted target secrets. [VERIFIED: src/finance_sync/config/settings.py:404-415; src/finance_sync/worker/jobs.py:1465-1484] — quote: `WealthfolioConfig.from_settings(settings)` and `await wf_client.authenticate()`; [VERIFIED: src/finance_sync/models/export_target.py:60-110]

**How to avoid:** Query `ExportTarget` by `tenant_id`, `target_type`, and active status; decrypt via the existing envelope helper; build one client per target; never log the decrypted payload.

**Warning signs:** Cross-tenant issue counts, one target’s health shown for another tenant, or bridge operation works only when global env vars are present.

### Pitfall 3: Resolving after local repair only

**What goes wrong:** A canonical quote exists but Wealthfolio still reports the same issue because remote projection failed, the wrong asset ID was used, or Wealthfolio health is cached.

**Why it happens:** Existing quote strategy verification checks the local `SecurityPrice` store only. [VERIFIED: src/finance_sync/reconciliation/remediation/quote.py:68-99] — quote: `select(func.count()).select_from(SecurityPrice)` and `resolved = int(count or 0) >= 1`.

**How to avoid:** Add a target-aware strategy wrapper or verification adapter that performs a fresh remote health/status read after projection; keep unresolved work in `retry_wait` or `manual_review`.

**Warning signs:** Items become resolved while Wealthfolio UI still displays the finding; repeated next poll recreates the issue.

### Pitfall 4: Fabricating financial facts

**What goes wrong:** The bridge fills missing purchase prices, transaction history, transfers, or negative valuation inputs with zero/default/inferred values.

**Why it happens:** A downstream snapshot API may accept incomplete values even though the result is financially misleading. [VERIFIED: src/finance_sync/services/wealthfolio_preflight.py:151-179]

**How to avoid:** Route missing cost basis, negative/incomplete valuation, unsafe transaction, and unknown-category issues to `manual_review` with the remote entity and actionable evidence only.

**Warning signs:** Any repair writes transaction or valuation rows without a canonical source record; quote repair code starts mutating transaction or tax-lot models.

### Pitfall 5: Stale scheduler rows after disabling the flag

**What goes wrong:** A persistent APScheduler job continues firing after the feature is disabled or fires against a stale function registration.

**Why it happens:** The scheduler persists jobs in PostgreSQL and already contains explicit stale-job cleanup for the export flag. [VERIFIED: src/finance_sync/worker/scheduler.py:296-321]

**How to avoid:** Add bridge job removal when disabled, assert no bridge job appears by default, and test restart/flag-toggle behavior.

### Pitfall 6: High-cardinality observability

**What goes wrong:** Metrics become expensive or unusable because target IDs, asset IDs, or error bodies are labels.

**Why it happens:** Remediation metrics already label only provider/strategy/outcome/category and intentionally avoid row identifiers. [VERIFIED: src/finance_sync/observability/metrics.py:130-165]

**How to avoid:** Keep labels to bounded enums such as provider, outcome, error category, and endpoint family; put sanitized target IDs only in structured logs where needed.

## Code Examples

Verified patterns from official sources and the repository:

### Wealthfolio targeted quote repair contract

```python
# Wealthfolio docs: asset IDs are opaque; market.sync takes asset IDs.
await client.sync_market_data(
    asset_ids=[remote_asset_id],
    refetch_all=False,
    refetch_recent_days=7,
)

# Existing client contract: connector-owned quote projection is idempotent
# for the same asset/day/source.
await client.upsert_quote(remote_asset_id, quote_payload)
```

The `sync_market_data` method is a proposed adapter name and therefore `[ASSUMED]`; the upstream documented operation is `market.sync(assetIds, refetchAll, refetchRecentDays?)`, while the repository already provides `upsert_quote`. [CITED: https://wealthfolio.app/docs/addons/api-reference/]; [VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:896-911] — quote: `async def upsert_quote(self, asset_id: str, quote: dict[str, Any]) -> None`.

### Existing remediation transition boundary

```python
verification = await strategy.verify(item)
item.verification_count += 1
status = (
    "resolved"
    if verification.resolved
    else "retry_wait"
)
await backlog.transition(
    tenant_id,
    item_id,
    status=status,
    claim_token=item.claim_token,
)
```

This preserves the current executor’s verified status vocabulary and ordering. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:157-181] — quote: `verification = await strategy.verify(item)` and `status = ("resolved" if verification.resolved else ... "retry_wait")`.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Broad Wealthfolio quote refresh | Targeted market sync by opaque asset IDs | Current Wealthfolio Addon API documentation | Repair only the affected assets and keep work bounded. [CITED: https://wealthfolio.app/docs/addons/api-reference/] |
| UI-only Health Center repair | Authenticated server route with machine-readable fix action | Current Wealthfolio server source | A bridge can call status/fix endpoints, but must still verify health afterward. [CITED: https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs] |
| Local-only remediation completion | Execute, project, then verify | Existing finance-sync remediation executor | Prevents false `resolved` states. [VERIFIED: src/finance_sync/reconciliation/remediation/executor.py:157-181] |

**Deprecated/outdated:**

- **Global-only Wealthfolio credentials for bridge work:** Keep the global settings path only for legacy exporter compatibility; the new bridge should use tenant-scoped `ExportTarget` secrets. [VERIFIED: src/finance_sync/models/export_target.py:44-110; src/finance_sync/config/settings.py:404-415]
- **Direct Wealthfolio SQLite mutation:** The project’s current client and upstream docs expose HTTP/API operations; the phase should not bypass the authenticated API or share Wealthfolio’s database file. [CITED: https://github.com/wealthfolio/wealthfolio; VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:134-177]

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The supported Wealthfolio deployment returns issue fields that can be normalized from `issues`, `fixAction`, `affectedItems`, `severity`, and `code`. | Summary / Architecture Patterns | Parser and fixture design could be wrong; planner must add a contract capture/checkpoint against the actual target or exact upstream `HealthStatus` definition. [ASSUMED] |
| A2 | A target-scoped cursor/state table is needed in addition to existing `ExportTarget.last_*` health columns. | Summary / Architecture Patterns | Schema may be overbuilt or duplicate existing state; planner should compare required cursor/hash/error fields to the target model before migration. [ASSUMED] |
| A3 | Wealthfolio’s installed deployment supports the documented current fix/market contracts at the project’s target version. | Standard Stack / Code Examples | Older target versions may require compatibility handling or disable automatic repair. [ASSUMED] |
| A4 | The best remote verification is a fresh health/status call after targeted repair rather than a dedicated per-issue verification endpoint. | Architecture Patterns | Health caching or issue hashing may require a forced check route or a second bounded poll. [ASSUMED] |
| A5 | Bridge target configuration will be represented by `ExportTarget` rather than legacy global settings. | Alternatives / Pitfalls | If deployment intentionally uses only the legacy global target, the worker design and tenant isolation contract change materially. [ASSUMED] |

## Open Questions

1. **What is the exact serialized `HealthStatus` payload for the supported Wealthfolio version?**
   - What we know: The route returns `HealthStatus`; official docs enumerate categories and fix action IDs, and the server fix source shows asset-ID payload behavior. [CITED: https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs; https://wealthfolio.app/docs/guide/health-center/]
   - What’s unclear: Exact field names, issue IDs/data hashes, category codes, affected-item nesting, severity values, and whether `/health/status` is sufficiently fresh after `/health/fix`.
   - Recommendation: Make a sanitized real-response fixture and upstream-version compatibility test a Wave 0 checkpoint before locking the normalizer.

2. **Should bridge state extend `ExportTarget` or use a separate target-keyed table?**
   - What we know: `ExportTarget` already stores `last_health_status`, `last_health_error`, and `last_checked_at`, while the phase plan requests payload hash, issue count, and successful-poll semantics. [VERIFIED: src/finance_sync/models/export_target.py:100-110; .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:57-61]
   - What’s unclear: Whether multiple bridge runs, cursors, or per-target issue-generation state require separate rows.
   - Recommendation: Decide after listing the minimum state needed for safe absence reconciliation; prefer extending the target if one row is sufficient, otherwise add a foreign-keyed table.

3. **What exact remote repair primitive should be used for historical gaps?**
   - What we know: Upstream documents targeted `market.sync` and quote history/update APIs. [CITED: https://wealthfolio.app/docs/addons/api-reference/]
   - What’s unclear: Whether direct quote PUTs, `market.sync` with recent-day refetch, or both are needed for the supported Wealthfolio deployment, and whether server-side sync can report completion synchronously.
   - Recommendation: Prefer targeted market sync for provider-owned quote issues; use direct quote projection only when canonical history is authoritative and the current client contract is proven against fixtures.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|-------------|-----------|---------|----------|
| Python | Application/tests | ✓ | `3.14.6` | Project declares `>=3.12`; use project-supported interpreter in CI if local type/test behavior differs. [VERIFIED: environment probe; pyproject.toml:6] |
| uv | Dependency/test commands | ✓ | `0.11.27` | Use `pytest`/`alembic` directly only if the locked environment is already active. [VERIFIED: environment probe] |
| Docker | PostgreSQL/Redis integration and E2E fixtures | ✓ | `29.7.2` | Start `docker compose -f docker-compose.test.yml up -d --wait`. [VERIFIED: environment probe; Makefile:54-86] |
| PostgreSQL service | Integration, migrations, durable scheduler tests | ✗ running locally | — | Docker test compose; local `psql` CLI is absent. [VERIFIED: environment probe] |
| Redis service | Quota/rate-limit coordination tests | ✗ running locally | — | Docker test compose; unit tests can use existing fakes. [VERIFIED: environment probe] |
| Wealthfolio target | Contract/E2E remote verification | Not observed in local process list | — | Mocked client fixtures for unit/integration; deployment checkpoint for real target. [VERIFIED: environment probe] |

**Missing dependencies with no fallback:** None for unit-level implementation; a real Wealthfolio instance is required for final contract and deployment verification. [ASSUMED]

**Missing dependencies with fallback:** PostgreSQL/Redis use the repository’s Docker test compose; Wealthfolio uses sanitized mocked HTTP fixtures until a target is available. [VERIFIED: Makefile:54-86]

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest `9.1.1` + pytest-asyncio `1.4.0` locked [VERIFIED: uv.lock:2465-2483] |
| Config file | `pyproject.toml` [VERIFIED: pyproject.toml:179-188] |
| Quick run command | `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_multi_client.py tests/test_remediation_backlog.py -q` [VERIFIED: Makefile:36-37; pyproject.toml:179-188] |
| Full suite command | `make test-ci` [VERIFIED: Makefile:48-50] |

### Phase Requirements → Test Map

| Requirement / behavior | Test Type | Automated Command | File Exists? |
|------------------------|-----------|-------------------|-------------|
| Auth success, 401/403, timeout, 408/429/5xx, malformed JSON, sanitized errors | unit | `uv run pytest tests/test_wealthfolio_multi_client.py -q` | ✅ existing file; add cases |
| Health normalizer preserves unknown fields safely and routes unknown categories to manual review | unit | `uv run pytest tests/test_wealthfolio_health_bridge.py -q` | ❌ Wave 0 |
| Repeated poll is deduplicated per tenant/target/entity | unit + integration | `uv run pytest tests/test_remediation_backlog.py -q` and bridge tests | ✅ backlog; ❌ bridge |
| Failed poll does not reconcile missing issues | unit | `uv run pytest tests/test_wealthfolio_health_bridge.py -q` | ❌ Wave 0 |
| Safe quote repair updates canonical price and targeted Wealthfolio quote | integration | `uv run pytest tests/test_wealthfolio_health_bridge.py -q` | ❌ Wave 0 |
| Historical price window is bounded and verified | unit | `uv run pytest tests/test_remediation_backlog.py -q` plus bridge strategy tests | ✅ partial existing coverage |
| Missing purchase price, negative valuation, transaction/unknown issues become manual review without mutation | unit | `uv run pytest tests/test_wealthfolio_preflight.py tests/test_remediation_backlog.py -q` | ✅ existing files; add bridge cases |
| Remote verification failure yields `retry_wait`, not `resolved` | unit | `uv run pytest tests/test_remediation_backlog.py -q` | ✅ executor coverage exists; add remote verifier cases |
| Disabled flag registers no bridge job and removes stale persisted job | unit | `uv run pytest tests/test_intel_scheduler_registry.py tests/test_worker.py -q` | ✅ existing scheduler/worker files; add bridge cases |
| Manual trigger is permission-protected, bounded, idempotent, and tenant-safe | API | `uv run pytest tests/test_control_plane_api.py tests/test_control_plane_phase1_tenant.py -q` | ✅ existing files; add endpoint cases |
| Migration upgrades empty/existing DB | integration | `make integration-up && uv run alembic upgrade head` | ✅ migration CI pattern; add migration if needed |

### Sampling Rate

- **Per task commit:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest <changed tests> -q`
- **Per wave merge:** `make test` plus relevant integration tests.
- **Phase gate:** `make ci-fast` and migration/integration/E2E checks green before `$gsd-verify-work`. [VERIFIED: Makefile:14-52]

### Wave 0 Gaps

- [ ] `tests/test_wealthfolio_health_bridge.py` — parser fixtures, target scope, deduplication, failure boundary, remote verification, and unsafe routing.
- [ ] Exact sanitized HealthStatus fixture from the supported Wealthfolio version — required before treating response fields as verified.
- [ ] Bridge scheduler registration/disabled-flag tests — verify persisted-job removal.
- [ ] Target-backed worker fixture — decrypts an `ExportTarget` secret without using global settings.
- [ ] Integration fixture for PostgreSQL/Redis if the new cursor/state table is retained.

## Security Domain

The phase handles credentials and financial data, so security enforcement applies. [CITED: https://owasp.org/www-project-application-security-verification-standard/]

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| Authentication | yes | Authenticate every target with its own encrypted secret; classify 401/403 separately; never fall back to another tenant’s credential. [CITED: https://github.com/OWASP/ASVS/blob/master/5.0/en/0x15-V6-Authentication.md; VERIFIED: src/finance_sync/api/v1/destinations.py:916-940] |
| Session Management | yes | Keep Wealthfolio session cookies inside one client instance, close the client, and never log cookies/passwords. [CITED: https://github.com/OWASP/ASVS/blob/master/5.0/en/0x16-V7-Session-Management.md; VERIFIED: src/finance_sync/exporter/wealthfolio/client.py:203-243] |
| Access Control / Authorization | yes | Manual trigger must use backend `require_permission(...)` and tenant-scoped service queries. [CITED: https://devguide.owasp.org/en/03-requirements/05-asvs/; VERIFIED: src/finance_sync/api/v1/control_plane.py:80-93] |
| Input Validation / Business Logic | yes | Allow-list issue categories/actions, bound list sizes/date windows/context bytes, and fail closed for unknown or unsafe financial mutations. [CITED: https://github.com/OWASP/ASVS/blob/master/5.0/docs_en/OWASP_Application_Security_Verification_Standard_5.0.0_en.json; VERIFIED: src/finance_sync/reconciliation/remediation/price_history.py:29-43] |
| Cryptography / Data Protection | yes | Reuse envelope encryption for `ExportTarget` secrets; do not persist raw payloads or credentials in JSON context. [CITED: https://github.com/OWASP/ASVS/blob/master/5.0/en/0x15-V6-Authentication.md; VERIFIED: src/finance_sync/models/remediation.py:24-29; src/finance_sync/api/v1/destinations.py:616-630] |
| Error Handling and Logging | yes | Sanitize errors, bound error text, avoid raw remote payloads, and keep metrics low-cardinality. [CITED: https://devguide.owasp.org/en/03-requirements/05-asvs/; VERIFIED: src/finance_sync/observability/metrics.py:130-165; src/finance_sync/worker/jobs.py:81-92] |

### Known Threat Patterns for Python/FastAPI/SQLAlchemy bridge

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Cross-tenant target lookup or backlog update | Elevation of privilege / Information disclosure | Include tenant ID in every target, state, backlog, and API query; rely on existing tenant-scoped services and foreign keys. [VERIFIED: src/finance_sync/reconciliation/remediation/backlog.py:111-115,211-221] |
| Credential leakage in logs/metrics/context | Information disclosure | Decrypt only in memory; sanitize exception text; never store raw response or credentials. [VERIFIED: src/finance_sync/models/remediation.py:24-29; src/finance_sync/services/wealthfolio_preflight.py:103-107] |
| Replay or duplicate repair | Tampering / Denial of service | Stable dedup keys, database uniqueness, leases, idempotent quote upsert, and bounded manual-trigger limits. [VERIFIED: src/finance_sync/models/remediation.py:31-64; src/finance_sync/exporter/wealthfolio/client.py:896-911] |
| SSRF through target URL | Server-side request forgery | Reuse the existing destination URL validation/safe URL path and restrict bridge to configured active Wealthfolio targets; do not accept arbitrary URL in the trigger body. [VERIFIED: src/finance_sync/api/v1/destinations.py:916-940; ASSUMED for the new endpoint contract] |
| False resolution after stale/failed remote check | Business logic tampering | Successful poll boundary plus fresh remote verification; failed/partial polls never reconcile disappearance. [VERIFIED: .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md:94-99] |

## Sources

### Primary (HIGH confidence)

- `src/finance_sync/models/remediation.py`, `backlog.py`, and `executor.py` — existing deduplication, leasing, status lifecycle, and verification contracts. [VERIFIED: in-repo source read this session]
- `src/finance_sync/exporter/wealthfolio/client.py` — existing authenticated client and quote projection contract. [VERIFIED: in-repo source read this session]
- `src/finance_sync/models/export_target.py` and `src/finance_sync/api/v1/destinations.py` — tenant-scoped encrypted destination state. [VERIFIED: in-repo source read this session]
- `https://raw.githubusercontent.com/wealthfolio/wealthfolio/main/apps/server/src/api/health.rs` — current upstream route and fix implementation. [CITED]

### Secondary (MEDIUM confidence)

- `https://wealthfolio.app/docs/guide/health-center/` — current Health Center categories, severities, auto-fix IDs, and verification caveats. [CITED]
- `https://wealthfolio.app/docs/addons/api-reference/` — current market/asset/quote API semantics and opaque asset IDs. [CITED]
- `https://github.com/OWASP/ASVS/tree/master/5.0` — current ASVS 5.0 security categories and verification guidance. [CITED]

### Tertiary (LOW confidence)

- None used for locked recommendations. Any unverified implementation names or exact Wealthfolio payload fields are explicitly marked `[ASSUMED]`.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — existing project declarations and lockfile were read; no new packages recommended.
- Architecture: MEDIUM — repository seams are verified, but the exact upstream health JSON contract is not fully published in the route source.
- Pitfalls: MEDIUM — grounded in existing code, official Wealthfolio behavior, and security guidance; deployment-version compatibility remains an open checkpoint.

**Research date:** 2026-09-12
**Valid until:** 2026-10-12 for the repository stack; 2026-09-19 for Wealthfolio API behavior because it is an actively changing upstream.
