# Live Wealthfolio export — idempotent import proof (t_b56d009f)

Date: 2026-08-16 — Operator: Hermes kanban worker (default)
Task: Run live Wealthfolio export twice and prove idempotent import
Instance: http://192.168.3.50:8080 (Wealthfolio, LXC 104)
Prod finance-sync: worker app rbeh9tetzvuyirutb66rxqea (running:healthy)

## Status: PASS — first run imports successfully, second run creates zero duplicates

---

## 1. Environment state verified (read-only)

### 1.1 Prod DB migrated to head
- `finance-sync-pg` (avoxjx7g0c36ru1ez7hetauy on LXC 100): `alembic current` → **0015 (head)**
- Migration chain 0009→0015 applied (stamp 0009 + upgrade head; performed during
  the earlier run that discovered the live reconcile bugs).  All tables present:
  `wealthfolio_deliveries`, `export_runs.exporter_type`, `wealthfolio_account_mappings`.

### 1.2 Test data seeded (prod tenant 085231ce-564e-4cc9-a111-624618e8dec5)
- Account: `Smoke Test Brokerage` (22222222-2222-4222-8222-222222222222, EUR)
- Security: VWCE / IE00BK5BQT80 (Vanguard FTSE All-World UCITS ETF, EUR)
- Transactions: buy 10 @ 100 (2026-08-01), sell 5 @ 100 (2026-08-10),
  dividend 25 (2026-08-12) — all booked
- Holding: VWCE 5.0 (observed 2026-08-13)

### 1.3 Auth + account mapping (live API, 15:25Z)
```
POST /api/v1/auth/login → 200 {"authenticated": true, "expiresIn": 3600}
GET  /api/v1/accounts   → 200 (1 account)
  id: d70e1d85-44f8-4102-aaf9-e32f4a47a862
  name: "Smoke Test Brokerage", accountType: SECURITIES, currency: EUR
  provider: FINANCE_SYNC
  providerAccountId: finance-sync:085231ce-…:22222222-…  ← mapping correct
```
`wealthfolio_account_mappings` row created/updated by the exporter
(wf_account_id = d70e1d85, provider_account_id matches).

## 2. The double-run proof (15:27Z, PR #248 fix code)

Ran the production CLI in the worker container with the PR #248 exporter
(is_cash reconcile fix; container file restored to the deployed image
afterwards — sha verified):

```
$ python -m finance_sync.cli wealthfolio smoke
Wealthfolio smoke result:
  Accounts visible: 1
  Activities visible: 3
  Holdings visible: 0        ← see finding #3 below (Wealthfolio-side position calc)
  Idempotent second pass: no ← flag set by the holdings finding, NOT by duplicates
```

### 2.1 export_runs (authoritative) — run twice back-to-back
```
Run e6ebf466 (push #1, 15:27:19Z): transactions_attempted=3, exported=3, failed=0  ← FIRST RUN IMPORTS SUCCESSFULLY
Run 4e10c0b7 (push #2, 15:27:20Z): transactions_attempted=0, exported=0, failed=0  ← SECOND RUN: ZERO DUPLICATES
```

### 2.2 Remote state after both pushes (15:27:20Z probe)
- activities/search total = **3** (exactly the 3 unique transactions, no dupes):
  - BUY     10 @ 100  "Smoke test buy | ID: smoke-txn-buy-1"
  - SELL     5 @ 100  "Smoke test sell | ID: smoke-txn-sell-1"
  - DIVIDEND 1 @ 25   "Smoke test dividend | ID: smoke-txn-div-1"
- Every activity carries the external transaction ID in the comment field
  (`… | ID: smoke-txn-…`), and the delivery cursor advanced to the last
  transaction (55555555-…, 2026-08-12) with export_run_id = e6ebf466 →
  the second push found 0 pending and imported 0.

## 3. Findings

### 3.1 #246/#247/#248 fixes verified live
- **#246 quoteCcy**: without it the import endpoint rejects activities
  (observed 14:42Z); with it the first import succeeded (3/3/0).
- **#247 cursor as str**: cursor now advances (previously asyncpg rejected
  the UUID-for-VARCHAR bind after a successful import, causing a duplicate
  re-import on the next run — the historic 6-activity state observed at
  14:43Z).  After the fix: exactly 3 activities, cursor at the last txn.
- **#248 is_cash skip (PR merged cf89c71)**: the scheduled job's reconcile
  finding "Wealthfolio bevat posities buiten de bronsnapshot" (remote cash
  row) is GONE.  Before: 2 findings (15:20:33Z run 630f4703); after:
  1 finding (position only).

### 3.2 Remaining reconcile finding: no VWCE position on the remote
`holdings/list` returns only the cash row (EUR 525.00) — the BUY/SELL
activities import successfully but do not materialize a security position
in the instance's holdings view, so the reconciler flags
"Positie-afwijking voor opgeloste security IE00BK5BQT80".
Controlled experiment (throwaway account, 15:35Z): importing the SAME
activity with ticker `VWCE` resolves fine (providerSymbol VWCE.DE via
Yahoo) and is accepted (200), yet `holdings/list` still returns 0
positions.  Conclusion: position materialization is a Wealthfolio-side
computation/sync concern (no market-data-backed position row created on
this instance), NOT a finance-sync import or idempotency failure.  The
import pipeline (activities + cursor + comment-ID dedup) is proven sound.

## 4. Deliverables
- PR #248 "fix(export): ignore remote cash rows in holdings reconciliation"
  merged to main (cf89c71, 2026-08-16T15:26:10Z) — all 10 CI checks green
  after the ruff-format fix (f3895f3) was pushed.
- PRs #246 (quoteCcy) and #247 (cursor str) merged earlier.
- Worker redeployed to cf89c71 via Coolify API (deployment
  k3q5q8qrvo7phost01fagdol, finished 15:38:47Z); container
  rbeh9tetzvuyirutb66rxqea-153758884449 verified running the #248 code
  (exporter.py sha df097946).
- Scheduled job verified live after redeploy (15:43:17Z tick, run
  e0f9cf47): `imported=0`, `failed=0`, and the reconcile finding count
  dropped from 2 (cash row + position) to 1 (position only) — the #248
  cash-row fix is confirmed in the production scheduled run.

## 6. Remaining live finding (out of scope for import idempotency)
The reconciler still reports `Positie-afwijking voor opgeloste security
IE00BK5BQT80` because the remote Wealthfolio instance never materializes
a security position in `holdings/list` — verified by two controlled
experiments on throwaway accounts (15:35Z-15:37Z):
1. Activity import with ticker `VWCE` → check resolves (VWCE.DE via
   Yahoo, "Vanguard FTSE All-World UCITS ETF USD Accumulation"), import
   accepted (200), but `holdings/list` returns 0 positions.
2. Holdings snapshot import with string quantities → check 200
   (`snapshotsImported: 1`, symbol found), `holdings/list` still returns 0.
This is a Wealthfolio-instance position-computation/sync gap (positions
view not populated from imported activities/snapshots), NOT a
finance-sync import or idempotency failure.  The import pipeline
(activities accepted, cursor advanced, comment-ID dedup, zero duplicates)
is proven sound.  Follow-up: investigate the instance's position
materialization (background job / price sync) before claiming
UI-visible holdings (relevant to t_2d5038ff / t_991b5fb5).

## 5. Acceptance check
- [x] First run imports successfully          (3 attempted, 3 exported, 0 failed)
- [x] Second run creates zero duplicates      (0 attempted, 0 exported; remote = 3 unique)
- [x] Account mapping is correct              (FINANCE_SYNC provider, providerAccountId matches)
- [x] Evidence captured and attached          (this file + PR #248)
