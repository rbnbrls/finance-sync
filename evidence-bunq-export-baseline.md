# Bunq live export — status + baseline (t_15be1e9e)

Date: 2026-08-17 ~23:45Z (01:45 CEST) — Operator: Hermes kanban worker (default)
Task: t_15be1e9e "Confirm live export completed and capture export baseline"
Instance: Wealthfolio http://192.168.3.50:8080 (LXC 104)
Prod finance-sync: app `obcopz3142hxzs1zlie78amh` + worker `rbeh9tetzvuyirutb66rxqea`
(LXC 100), both running image `18d12aa` (PR #263, healthy). Prod DB
`finance_sync` (avoxjx7g0c36ru1ez7hetauy), alembic head **0016**.

## 1. Export status for bunq: NOT COMPLETED — has never started

The bunq export cannot run because **no bunq API credentials have ever been
provisioned**. All checks below were performed live against prod.

| check | state (2026-08-17 ~23:45Z) |
|---|---|
| `credentials` table | **0 rows** — no bunq API key anywhere |
| `connector_state` table | **0 rows** — no install/session state ever persisted |
| `sync_runs` table | **0 rows ever** — no connector sync has ever completed |
| worker `sync_bunq` job | live, every 15 min (23:40:16Z tick observed) → `sync_job_no_tenants` |
| worker `sync_bunq_cards` / `sync_trading212` | registered, no credentials to run with |
| worker image | 18d12aa (PR #263 header fix), healthy, all 9 jobs registered |
| export_wealthfolio job | live, every 5 min; latest run `9f6355ee` 23:45:16Z — see §3 |

All four code-side connector blockers (install flow #260/#261, persistence
migration 0016, base_url pagination, X-Bunq-Region header #263) are fixed,
merged and deployed — the remaining blocker is purely the missing operator
key (bunq app → Profile → Security → API keys, IP-bound to worker egress
77.175.111.18, provision via `POST /api/v1/connectors/configs`
`{"provider_type":"bunq","credentials":{"api_key":"…"}}`).

## 2. Export baseline (expected values for UI verification)

| metric | value |
|---|---|
| bunq accounts exported | **0** (prod `accounts` has only `smoke_test` ×1) |
| bunq transactions exported | **0** (prod `transactions` has only `smoke_test` ×3) |
| bunq card transactions / balances | 0 / 0 |
| export timestamp (bunq) | N/A — no bunq export ever ran |
| sync cursor (bunq) | none (`sync_runs` empty, no cursor rows) |
| Wealthfolio delivery cursor | `wealthfolio_deliveries`: account `22222222-…`, last txn `55555555-5555-4555-8555-555555555555` (smoke dividend, 2026-08-12T10:00:00Z), export_run `e6ebf466` (2026-08-16 15:27Z smoke export) |

**Expected state the UI verification must match (verified live this run):**
- Accounts: **2** — `Smoke Test Brokerage` (d70e1d85, FINANCE_SYNC, SECURITIES,
  EUR) + `snap-test-f83d67` (186a6234, FINANCE_SYNC). **0 bunq.**
- Activities: **totalRowCount = 3** — DIVIDEND 2026-08-12, SELL 2026-08-10,
  BUY 2026-08-01 (all on d70e1d85, all `smoke-txn-*`, comments carry
  `ID: smoke-txn-…`). **0 bunq. Duplicates: 0.**
- If any bunq-sourced account/transaction appears, that is NEW data from a
  sync that ran after this baseline — re-verify and update this baseline
  before comparing.

## 3. Export job health (not bunq-related)

The scheduled `export_wealthfolio` job runs every 5 min and every run is
marked `status=failed` with **0 attempted / 0 exported / 0 failed** because of
the **known pre-existing reconcile finding**: `Positie-afwijking voor
opgeloste security IE00BK5BQT80` — the Wealthfolio instance never
materializes security positions from imported activities (tracked under
t_991b5fb5, out of scope here). Latest runs: `9f6355ee` (23:45:16Z),
`ff877cb2` (23:40:16Z), `86666ae6` (23:35:16Z). This is NOT a bunq regression.

## 4. Acceptance check (t_15be1e9e)

- [x] Export status known         → bunq export NOT completed, never started
                                 (blocker: no credentials provisioned)
- [x] Baseline counts documented  → 0 bunq accounts / 0 bunq transactions /
                                 0 card transactions / 0 balances; UI must show
                                 2 accounts + 3 activities, 0 duplicates
- [x] Cursor position documented  → wealthfolio delivery cursor at
                                 55555555-… (export_run e6ebf466); no bunq cursor
- [x] Baseline saved in PR notes  → this file on branch
                                 `docs/t_15be1e9e/bunq-export-baseline`, PR #264