# Wealthfolio UI re-verification — bunq-path data, post-fix (t_7e5fb14a)

Date: 2026-08-16 21:58–22:10Z — Operator: Hermes kanban worker (default)
Task: Re-verify bunq accounts/transactions/balances in Wealthfolio UI after the
bunq connector fix and finalize evidence
Instance: http://192.168.3.50:8080 (Wealthfolio, LXC 104)
Precondition: t_ee4ab38e completed — bunq installation-flow fix merged
(PR #260/#261, commit f6bbea1), migration 0016 applied to prod DB, prod app
(`obcopz3142hxzs1zlie78amh`) and worker (`rbeh9tetzvuyirutb66rxqea`) redeployed
to f6bbea1 (both healthy).

## Status: NO BUNQ DATA — root cause unchanged, fix verified live

Re-opened the live UI and re-verified through the production client path and
fresh headless-browser screenshots. The UI still shows only the smoke-test
artifacts; **zero bunq accounts, transactions, or balances**. The code-side
blockers are all fixed and live (worker runs f6bbea1, sync_bunq fires every 15
min), but the pipeline still has **no bunq credentials to sync with** — the
remaining blocker is operator-side: a bunq API key has never been provisioned.

## 1. Method (production path, no secrets in this doc)

- API checks ran **inside the prod app container** (`obcopz3142hxzs1zlie78amh`)
  using the same `WealthfolioClient` the exporter uses, with the configured
  `WEALTHFOLIO_SERVER_URL` / `WEALTHFOLIO_PASSWORD` env (password never
  printed/committed).
- Screenshots: headless Chromium (Playwright), authenticated with the same
  configured password, captured 2026-08-16 ~22:05Z.

## 2. Authoritative API verification (21:58Z, authenticated)

```
auth_status        POST /api/v1/auth/login → 200, authenticated=true
ACCOUNTS           GET /api/v1/accounts → count=2
  d70e1d85-44f8-4102-aaf9-e32f4a47a862  "Smoke Test Brokerage"  SECURITIES EUR
  186a6234-e131-47f2-8b6c-e53ed84e9d5e  "snap-test-f83d67"      SECURITIES EUR
ACTIVITIES         POST /api/v1/activities/search (per account, pageSize 100)
  Smoke Test Brokerage: totalRowCount = 3 (BUY/Sell/Dividend smoke-txn-*)
  snap-test-f83d67:    totalRowCount = 0
  → 3 activities total, all smoke; bunq-derived: 0; duplicate_ids: 0
HOLDINGS           GET /api/v1/holdings/list?accountId=… → 1 row
  (Smoke Test Brokerage cash €525.00); snap-test account: 0 rows; bunq: 0
```

No bunq account exists to query; no bunq holdings exist. Duplication remains
**not an issue** (3 unique activities, 0 duplicates) — the finding is that bunq
data is **entirely absent**, same as pre-fix.

## 3. Fresh UI evidence (screenshots, 2026-08-16 ~22:05Z)

| Screenshot | Shows |
|---|---|
| `docs/wealthfolio-ui-evidence/wf_reverify_dashboard.png` | Dashboard (Investments): total **€525.00**, "2 accounts" (Investments category), Holdings **"No holdings yet"**. No bunq account. |
| `docs/wealthfolio-ui-evidence/wf_reverify_activities.png` | Activities: **3 / 3 activities** — Dividend €25.00, Sell 5 @ €100 (€500), Buy 10 @ €100 (€1,000), all in `Smoke Test Brokerage`. No bunq transactions. |
| `docs/wealthfolio-ui-evidence/wf_reverify_holdings.png` | Holdings view: no holdings materialized (known instance-side gap, t_991b5fb5) |

Pre-fix baseline screenshots remain in `docs/wealthfolio-ui-evidence/wf_bunq_*.png`
(PR #257, merged to main as 99fdafd).

## 4. finance-sync prod DB state (queried live, 21:5xZ, via psql in postgres container)

| Table | Rows | Detail |
|---|---|---|
| `credentials` | **0** | no bunq (or any other) connector credentials |
| `sync_runs` | **0** | no connector sync has ever completed |
| `connector_state` | **0** | no persisted bunq installation material (fix ready, unused) |
| `accounts` | 1 | only `22222222-…` smoke_test |
| `transactions` | 3 | only the three smoke transactions |
| `wealthfolio_deliveries` | 1 | smoke account (cursor at smoke-txn-*) |
| `balances` | 0 | |

## 5. Worker / scheduler state (post-redeploy, f6bbea1)

- Worker healthy, all jobs registered (export_wealthfolio 5 min, sync_bunq 15 min,
  sync_bunq_cards 1 h, sync_trading212 1 h, nightly_reconciliation 02:00).
- `sync_bunq` tick 21:44:48Z: `sync_job_starting` → credential SELECT (empty) →
  **`sync_job_no_tenants`** → no-op. The fixed installation-flow code never
  engages because there is no key to authenticate with.
- `export_wealthfolio` ticks (21:34/21:39/21:44/21:49Z): attempted=0, imported=0,
  status `failed` with the known pre-existing IE00BK5BQT80 position-reconcile
  finding (Wealthfolio instance never materializes positions — tracked under
  t_991b5fb5). Not a bunq regression; idempotent push path unchanged.

## 6. Discrepancy table (re-verified)

| Item | Expected | Actual (re-verified live) | Status |
|---|---|---|---|
| bunq account(s) in UI | bunq accounts visible | only Smoke Test Brokerage + snap-test-f83d67 | **MISSING** |
| bunq transactions in UI | bunq transactions visible | only 3 smoke transactions | **MISSING** |
| bunq balances | balances shown where applicable | nothing bunq (1 cash row, smoke) | **MISSING** |
| sync_runs for bunq | sync_bunq completes with items | 0 rows ever (sync_job_no_tenants) | **MISSING** |
| duplicates | none | none (3 unique activities, 2 accounts) | OK |
| credentials | bunq API key provisioned | none anywhere (DB, env, Coolify) | **MISSING** |

## 7. Root cause (unchanged, now the ONLY remaining blocker)

All connector-code blockers are fixed and deployed (full_auth installation
flow, per-tenant persisted install state, base_url pagination — PR #260/#261).
The pipeline is ready and proven end-to-end for the non-bunq path (PRs
#246–#251). What remains is **upstream of the pipeline**: no bunq API key has
ever been provisioned, so `sync_bunq` no-ops on every tick and no bunq data
ever enters finance-sync or Wealthfolio.

## 8. What the operator needs to do (to finish this task)

1. Create a bunq API key (bunq app → Profile → Security → API keys). Bunq keys
   are IP-bound: the key must list the finance-sync worker's egress IP.
2. Provision it via
   `POST /api/v1/connectors/configs` (admin bearer)
   `{"provider_type": "bunq", "credentials": {"api_key": "<key>"}}` — or set a
   `BUNQ_API_KEY` env var on the prod Coolify app and redeploy the worker.
3. Within ~20 min the chain runs itself: sync_bunq (15 min) → export_wealthfolio
   (5 min) → data visible in Wealthfolio UI. Then this task's final UI
   verification + screenshots can complete the acceptance.

## 9. Related

- Root task: t_2d5038ff · fix task: t_ee4ab38e (PR #260/#261) · this re-verify: t_7e5fb14a
- Pre-fix evidence: `evidence-bunq-wealthfolio-ui-verification.md` (t_51c9109d, PR #257)
- Position-reconcile finding: t_991b5fb5