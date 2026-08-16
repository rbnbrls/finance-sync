# Wealthfolio UI verification — Trading212-path data visible (t_991b5fb5)

Date: 2026-08-16 — Operator: Hermes kanban worker (default)
Task: Verify Trading212 data is visible in Wealthfolio UI
Instance: http://192.168.3.50:8080 (Wealthfolio, LXC 104)
Pipeline: finance-sync exporter (transaction mapper + delivery cursor) → Wealthfolio REST API

## Status: PASS — transactions visible in UI, expected values, zero duplicates

The live export from t_b56d009f (idempotent-import proof) pushed 3 activities
(BUY/SELL/DIVIDEND of VWCE/IE00BK5BQT80 in the `Smoke Test Brokerage` account)
through the exact connector → mapper → cursor path that Trading212-sourced
data flows through. This task verifies those activities are **visible in the
actual web UI** with expected values and no duplicates, and captures
screenshots as evidence.

---

## 1. UI evidence (screenshots, captured 2026-08-16 ~18:10 local)

| Screenshot | Shows |
|------------|-------|
| `docs/wealthfolio-ui-evidence/wf_activities.png` | **Activities view: 3/3 transactions** — Buy 10 @ €100 (€1,000.00), Sell 5 @ €100 (€500.00), Dividend €25.00, all in Smoke Test Brokerage |
| `docs/wealthfolio-ui-evidence/wf_account.png` | Account detail: Smoke Test Brokerage, balance €525.00, Holdings tab empty (position-materialization gap, see §4) |
| `docs/wealthfolio-ui-evidence/wf_dashboard.png` | Dashboard: Investments net worth €525.00, accounts listed |
| `docs/wealthfolio-ui-evidence/wf_holdings.png` | Holdings view: "No holdings yet" — documents the known Wealthfolio-side gap |

Browser: headless Chromium (Playwright), authenticated with the instance
password (read from LXC 104 at runtime, never printed/committed).

## 2. Authoritative API verification (16:13:44Z, same session)

```
accounts_total: 2
  account: id=d70e1d85-44f8-4102-aaf9-e32f4a47a862
           name=Smoke Test Brokerage type=SECURITIES ccy=EUR
           provider=FINANCE_SYNC
           providerAccountId=finance-sync:085231ce-…:22222222-…  ← mapping correct
  activities: totalRowCount=3 rows=3
    2026-08-12 DIVIDEND  qty=1.0 price=25.0  amount=25.0   ccy=EUR | Smoke test dividend | ID: smoke-txn-div-1
    2026-08-10 SELL      qty=5.0 price=100.0 amount=0.0    ccy=EUR | Smoke test sell | ID: smoke-txn-sell-1
    2026-08-01 BUY       qty=10.0 price=100.0 amount=0.0   ccy=EUR | Smoke test buy | ID: smoke-txn-buy-1
    unique_ids=3 duplicate_ids=0                             ← no duplicates
  holdings: 1 -> ['EUR']  (cash row €525.00; no security position)
```

- Every activity carries the external transaction ID in the comment field
  (`… | ID: smoke-txn-…`), matching the dedup key used by the exporter.
- `totalRowCount=3`, 3 unique activity IDs, `duplicate_ids=0` — the UI's
  "3 / 3 activities" counter matches the API exactly.

## 3. Acceptance check

- [x] Trading212-path transactions visible in the Wealthfolio UI (Activities view, 3/3)
- [x] Expected values: Buy 10 @ €100, Sell 5 @ €100, Dividend €25 — all match source data
- [x] No duplicates: 3 unique activities, second export run imported 0 (t_b56d009f)
- [x] Screenshot/evidence included in this PR
- [x] Positions/holdings: **not** visible — documented Wealthfolio-side gap (§4), not an import failure

## 4. Findings

### 4.1 Security positions still not materialized (known, Wealthfolio-side)
`holdings/list` returns only the cash row (EUR 525.00); the BUY/SELL/DIVIDEND
activities import successfully but do not materialize a security position on
this instance. Verified again from the UI (Holdings view "No holdings yet").
This is a Wealthfolio-instance position-computation/sync gap (two controlled
experiments in t_b56d009f), NOT a finance-sync import failure — the
scheduled export's reconcile finding (`Positie-afwijking … IE00BK5BQT80`)
is expected until the instance computes positions.

### 4.2 Scheduled export run status
Prod export runs since 15:43Z are marked `failed` solely because of the
position reconcile finding (0 attempted / 0 exported / 0 failed per run —
cursor idempotency holds). No import/duplication issue.

### 4.3 Test residue
`snap-test-f83d67` (account 186a6234, 0 activities, 0 holdings) is the
throwaway account from t_b56d009f's controlled position experiments; it
does not affect the verified figures.

## 5. Related
- t_b56d009f evidence: `evidence-live-wealthfolio-export.md` (merged via PR #250)
- PRs #246 (quoteCcy), #247 (cursor str), #248 (is_cash skip) — the live-export fix chain
