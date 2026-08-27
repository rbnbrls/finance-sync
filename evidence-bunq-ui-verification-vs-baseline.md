# Wealthfolio UI verification vs bunq export baseline (t_4367e7f6)

Date: 2026-08-17 ~02:10Z — Operator: Hermes kanban worker (default)
Task: t_4367e7f6 "Verify bunq data in Wealthfolio UI"
Instance: http://192.168.3.50:8080 (Wealthfolio, LXC 104)
Baseline: evidence-bunq-export-baseline.md (t_15be1e9e, PR #264, merged)
Prod finance-sync: app `obcopz3142hxzs1zlie78amh` + worker `rbeh9tetzvuyirutb66rxqea`
(LXC 100), image `18d12aa` (PR #263), healthy. Prod DB alembic head 0016.

## Status: MATCHES BASELINE — yet zero bunq data (export never ran)

Verified live through (a) the production client path and (b) a fresh
headless-browser session. The UI state is exactly the baseline's expected
state: **2 accounts, 3 smoke activities, €525.00 cash, 0 duplicates** and
**0 bunq** accounts/transactions/balances. The bunq export has never started
(no bunq API key provisioned anywhere — operator blocker, unchanged since the
baseline), so zero bunq data is the *expected* outcome of this verification.

## 1. Method (production path, no secrets in this doc)

- API checks used the repo's own `WealthfolioClient`
  (`src/finance_sync/exporter/wealthfolio/client.py` — the exact class the
  exporter uses) with the instance password fetched at runtime from LXC 104
  (`/root/wealthfolio.creds`); the password is never printed or committed.
- UI checks: headless Chromium (Playwright, system `python3`), logged in over
  the real login gate, screenshots captured 2026-08-17 ~02:05–02:08Z.

## 2. Authoritative API verification (~02:0xZ, authenticated)

```
auth_status   POST /api/v1/auth/login → 200, requiresPassword=true, authenticated=true
ACCOUNTS      GET /api/v1/accounts → count=2
  d70e1d85-44f8-4102-aaf9-e32f4a47a862  "Smoke Test Brokerage"  EUR  provider=finance-sync
  186a6234-e131-47f2-8b6c-e53ed84e9d5e  "snap-test-f83d67"      EUR  provider=test
ACTIVITIES    POST /api/v1/activities/search {accountIdFilter, pageSize 1000}
  Smoke Test Brokerage: meta.totalRowCount = 3, data rows = 3
    DIVIDEND 2026-08-12  qty=1.0  price=25.0  amount=25.0  EUR | Smoke test dividend | ID: smoke-txn-div-1
    SELL     2026-08-10  qty=5.0  price=100.0 amount=0.0   EUR | Smoke test sell    | ID: smoke-txn-sell-1
    BUY      2026-08-01  qty=10.0 price=100.0 amount=0.0   EUR | Smoke test buy     | ID: smoke-txn-buy-1
  snap-test-f83d67: totalRowCount = 0
  → 3 activities total, all smoke-txn-*, all on d70e1d85; bunq-derived: 0
HOLDINGS      GET /api/v1/holdings?accountId=d70e1d85… → 1 cash row (quantity=525.0)
              snap-test: 0 rows; no bunq account exists to query
```

API response for `activities/search` in this instance version returns rows
under `data` (not `rows`) and `meta` carries only `totalRowCount` (no
`unique_ids`/`duplicate_ids` fields) — the duplicate check therefore uses an
explicit key (account + type + date + amount + ID comment) over all 3 fetched
rows: **0 duplicate groups**.

## 3. UI evidence (screenshots, 2026-08-17 ~02:05–02:08Z)

New screenshots (this task): `docs/wealthfolio-ui-evidence/wf_verifybunq_*.png`.

| Screenshot | Shows |
|---|---|
| `wf_verifybunq_dashboard.png` | Dashboard: Investments Net Worth **€525.00**, "2 accounts", Holdings "No holdings yet", Goals none. |
| `wf_verifybunq_dashboard_accounts_expanded.png` | Accounts section expanded: **Smoke Test Brokerage (EUR €525.00 +5.00%)** + **snap-test-f83d67 (EUR €0.00)**. No bunq account. |
| `wf_verifybunq_accounts.png` | Accounts/Investments view: same 2 accounts, €525.00 total. |
| `wf_verifybunq_activities.png` | Activities: **3 / 3** — Dividend €25.00 (Aug 12), Sell 5 @ €100 → €500 (Aug 10), Buy 10 @ €100 → €1,000 (Aug 1), all in Smoke Test Brokerage, fees/taxes €0. No bunq transactions. |
| `wf_verifybunq_account_detail_smoke.png` | Smoke Test Brokerage detail: **Cash Balance €525.00**, Investments €0.00, no holdings materialized (known instance gap, t_991b5fb5). |
| `wf_verifybunq_account_detail_snap.png` | snap-test-f83d67 detail: empty (no holdings/activities). |
| `wf_verifybunq_holdings.png` | Holdings view: "No holdings yet" (instance never materializes positions — known, t_991b5fb5). |
| `wf_verifybunq_connect.png` | Sync & Connections: nothing bunq linked (only generic Wealthfolio Connect onboarding). |

Screenshots were visually verified (vision model) — no bunq branding, account
name, or transaction appears in any view.

## 4. Comparison vs baseline (t_15be1e9e, PR #264)

| Metric | Baseline expected | Actual (verified live) | Status |
|---|---|---|---|
| bunq accounts exported | 0 | **0** (UI accounts = 2, both smoke/test) | ✅ matches |
| bunq transactions exported | 0 | **0** (UI activities = 3, all smoke-txn-*) | ✅ matches |
| bunq card transactions / balances | 0 / 0 | 0 / 0 | ✅ matches |
| accounts in UI | Smoke Test Brokerage + snap-test-f83d67 | **exactly those 2** | ✅ matches |
| activities in UI | totalRowCount = 3 (DIVIDEND 08-12 / SELL 08-10 / BUY 08-01) | **3**, same dates/types/amounts | ✅ matches |
| duplicates | 0 | **0** (explicit key check; 3 unique IDs) | ✅ matches |
| balances | €525.00 cash, no holdings materialized | **€525.00** cash, no holdings | ✅ matches |

No new bunq-sourced account or transaction appeared since the baseline
capture (baseline §2 caveat checked: 0 bunq found) — UI state is exactly the
documented pre-export state.

## 5. Root cause of "no bunq data" (unchanged, operator blocker)

All connector-code blockers are fixed and deployed (PR #260/#261 install
flow + persistence + base_url; PR #263 X-Bunq-Region header; worker/app on
18d12aa). The sole remaining blocker is **upstream of the pipeline**: no bunq
API key has ever been provisioned — prod `credentials` table 0 rows,
`connector_state` 0 rows, `sync_runs` 0 rows ever; `sync_bunq` ticks every 15
min and logs `sync_job_no_tenants`. This is NOT a mapping/cursor/delivery bug
(that path is proven live and idempotent by PRs #246–#251 / t_b56d009f).

### What the operator needs to do (to see real bunq data)
1. Create a bunq API key (bunq app → Profile → Security → API keys). Bunq
   keys are IP-bound: the key must allow worker egress **77.175.111.18**.
2. Provision it via `POST /api/v1/connectors/configs`
   `{"provider_type": "bunq", "credentials": {"api_key": "<key>"}}` — or set
   `BUNQ_API_KEY` env on the prod app + redeploy.
3. Within ~20 min: sync_bunq (15 min) → export_wealthfolio (5 min) → bunq
   data appears in Wealthfolio; re-run this verification then (child task
   t_c87a5485 records "no issues found" today — the pipeline itself needs no
   code fix for the absence).

## 6. Acceptance check (t_4367e7f6)

- [x] UI opened in browser and accounts/transactions/balances inspected
- [x] Compared against export baseline (expected values match exactly)
- [x] Duplicates checked — 0 (explicit key comparison over all activities)
- [x] Screenshots captured & saved in `docs/wealthfolio-ui-evidence/` (this PR)
- [x] bunq data present? → **No** — zero bunq data is the documented expected
      state (export has never started; operator key not provisioned), so the
      verification MATCHES the baseline; discrepancy is the known blocker,
      not a pipeline defect

## 7. Related

- Root task: t_2d5038ff · baseline: t_15be1e9e (PR #264) · fix-if-failed:
  t_c87a5485 · connector fixes: PR #260/#261/#263
- Prior evidence: `evidence-bunq-wealthfolio-ui-verification.md` (PR #257),
  `evidence-bunq-wealthfolio-ui-reverification.md` (PR #262)
- Position-reconcile finding: t_991b5fb5 (not bunq-related)