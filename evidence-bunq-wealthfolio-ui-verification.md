# Wealthfolio UI verification — bunq-path data (t_51c9109d)

Date: 2026-08-16 (~18:05–18:15Z) — Operator: Hermes kanban worker (default)
Task: Verify bunq data is visible in Wealthfolio UI and capture evidence
Instance: http://192.168.3.50:8080 (Wealthfolio, LXC 104)
Pipeline: bunq connector → finance-sync worker (`sync_bunq`) → exporter
(`export_wealthfolio`) → Wealthfolio REST API / UI

## Status: FAIL/DISCREPANCY — no bunq-sourced data present in the UI

Zero bunq accounts, zero bunq transactions and zero bunq balances are visible
in the Wealthfolio UI or API. The finance-sync pipeline has never imported any
bunq data (0 sync runs ever, 0 bunq credentials provisioned). The expected
data per the backlog story
(`backlog/wealthfolio-koppeling-live-bunq-trading212.md` — "bunq-data
zichtbaar in Wealthfolio") is entirely missing. Details and root cause below.

---

## 1. UI evidence (screenshots, captured 2026-08-16 ~18:10 local, live instance)

Browser: headless Chromium (Playwright), authenticated with the instance
password (read from LXC 104 at runtime, never printed/committed).

| Screenshot | Shows |
|------------|-------|
| `docs/wealthfolio-ui-evidence/wf_bunq_dashboard_accounts.png` | Dashboard, Accounts section **expanded**: exactly 2 accounts — `Smoke Test Brokerage` (EUR, €525.00) and `snap-test-f83d67` (EUR, €0.00). **No bunq account.** |
| `docs/wealthfolio-ui-evidence/wf_bunq_dashboard.png` | Dashboard overview: net worth Investments €525.00, "2 accounts" (collapsed), Holdings "No holdings yet" |
| `docs/wealthfolio-ui-evidence/wf_bunq_account_detail.png` | Account detail `Smoke Test Brokerage`: Cash Balance €525.00, Investments €0.00 — the only brokerage with any data |
| `docs/wealthfolio-ui-evidence/wf_bunq_transactions.png` | Activities view: **3 / 3 activities**, all three smoke transactions (Buy 10 @ €100, Sell 5 @ €100, Dividend €25) in `Smoke Test Brokerage`. **No bunq transactions.** |
| `docs/wealthfolio-ui-evidence/wf_bunq_connect.png` | Sync & Connections page: no brokerage/bank/crypto connection configured (only the generic Wealthfolio Connect onboarding) — nothing bunq linked |

## 2. Authoritative API verification (same session, ~18:15Z)

```
AUTH_STATUS: requiresPassword=true, login 200 OK (session cookie)

ACCOUNTS      GET /api/v1/accounts        → 200, count=2
  d70e1d85-44f8-4102-aaf9-e32f4a47a862  "Smoke Test Brokerage"  SECURITIES EUR  (FINANCE_SYNC-managed)
  186a6234-e131-47f2-8b6c-e53ed84e9d5e  "snap-test-f83d67"      SECURITIES EUR  (throwaway, 0 activities)

ACTIVITIES    POST /api/v1/activities/search {page:0, pageSize:100}
  meta.totalRowCount = 3, rows = 3 (unique_ids=3, duplicate_ids=0)
    2026-08-12 DIVIDEND qty=1.0  price=25.0  amount=25.0   EUR | Smoke test dividend | ID: smoke-txn-div-1
    2026-08-10 SELL     qty=5.0  price=100.0 amount=0.0    EUR | Smoke test sell   | ID: smoke-txn-sell-1
    2026-08-01 BUY      qty=10.0 price=100.0 amount=0.0    EUR | Smoke test buy    | ID: smoke-txn-buy-1
  page 1: 0 rows → total 3. bunq-derived rows: 0

HOLDINGS      GET /api/v1/holdings?accountId=…  → cash row EUR 525.00 only (smoke account);
              no bunq account → no bunq holdings queryable
```

No duplicate accounts or transactions exist on the instance (3 unique
activities, 2 accounts, all smoke/test artifacts) — duplication is **not**
an issue here; the issue is that bunq data is **entirely absent**.

## 3. finance-sync production data state (prod DB `avoxjx7g0c36ru1ez7hetauy`, queried live)

| Table | Rows | Detail |
|---|---|---|
| `credentials` | **0** | no bunq (or any other) connector credentials provisioned |
| `accounts` | 1 | only `22222222-…` (smoke_test, "Smoke Test Brokerage") |
| `sync_runs` | **0** | no connector sync has ever completed — bunq sync has never run |
| `wealthfolio_deliveries` | 1 | only the smoke account (cursor at `smoke-txn-*`, export_run e6ebf466) |

Coolify prod env (`obcopz3142hxzs1zlie78amh`): `WORKER_JOB_BUNQ_SYNC_ENABLED=true`,
`WEALTHFOLIO_SERVER_URL` + `WEALTHFOLIO_PASSWORD` set, **no `BUNQ_*` credential
vars** (checked after PR #252 merge — the environment-aware provisioning path
has nothing to provision).

## 4. Worker / scheduler state (prod worker container `rbeh9tetzvuyirutb66rxqea`)

- `sync_bunq` job IS registered (15-min interval) and fires on schedule.
- Each tick: `provider=bunq sync_job_starting` → credential SELECT (empty,
  cached) → **`sync_job_no_tenants`** → no-op.
- Latest observed tick: 2026-08-16 17:53:17Z (same pattern at 18:00:17Z).
- Worker container currently runs `cf89c71` (pre-#252); pending redeploy after
  PR #252 — relevant only once credentials exist.

## 5. Expected vs actual (discrepancy table — input for fix task t_ee4ab38e)

| Item | Expected (backlog story / export) | Actual (verified live) | Status |
|---|---|---|---|
| bunq account(s) in UI | bunq accounts visible (accounts list) | only Smoke Test Brokerage + snap-test-f83d67 | **MISSING** |
| bunq transactions in UI | bunq transactions visible (Activities) | only 3 smoke transactions | **MISSING** |
| bunq balances | balances shown where applicable | nothing bunq | **MISSING** |
| sync_runs for bunq | sync_job_bunq completes with items | **0 rows ever** | **MISSING** |
| duplicates | none | none (3 unique) | OK |
| credentials | bunq API key provisioned | none anywhere (DB, env, Coolify, sessions) | **MISSING** |

## 6. Root cause

Not a mapper/cursor/delivery bug: the exporter path is proven live and
idempotent for the Trading212/smoke path (PRs #246–#251). The blocker is
**upstream of the pipeline**: no bunq API key has ever been provisioned
(credentials table empty since deploy; no `BUNQ_*` env; never provided in any
session), so `sync_bunq` no-ops every 15 minutes and no bunq data ever enters
finance-sync or Wealthfolio.

Secondary finding (from parent t_2d5038ff attempt 1, still unreproduced
against real bunq): the bunq connector assumes a pre-existing installation —
a fresh key would need the installation flow (RSA exchange → `/installation`
→ `/device-server` → `/session-server`); sandbox testing hit 403 on
`session-server` and 400 "device region not supported" from this host. Needs
verification with a real key.

## 7. Required to unblock (for fix task t_ee4ab38e + operator)

1. Provision a bunq API key (bunq app → Profile → Security → API keys; must
   allow the worker egress IP) via the API
   (`POST /api/v1/connectors/configs` `{"provider_type":"bunq"}`), a
   `BUNQ_API_KEY` env var (post-#252 environment-aware path), or a direct
   `credentials` row.
2. Redeploy the worker to post-#252 code (currently cf89c71).
3. Let `sync_bunq` run → export → re-verify (re-verify task t_7e5fb14a).
4. Verify the installation-flow gap against the real key if first auth 403s.

## 8. Related

- Root task: t_2d5038ff · fix task: t_ee4ab38e · re-verify: t_7e5fb14a
- Trading212-path evidence: PR #251 (`evidence-wealthfolio-ui-verification.md`)
- Backlog story: `backlog/wealthfolio-koppeling-live-bunq-trading212.md`