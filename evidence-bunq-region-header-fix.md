# bunq connector fix: drop rejected `X-Bunq-Region` header (t_2d5038ff)

Date: 2026-08-17 (~00:40Z) — Operator: Hermes kanban worker (default)
Task: Verify bunq data is visible in Wealthfolio UI — fix mapping/delivery issues
Repo: rbnbrls/finance-sync — branch: `fix/t_2d5038ff/bunq-region-header`

## Finding: the sandbox 400 was a connector bug, not a host geo-restriction

Prior attempts (t_51c9109d, t_ee4ab38e, t_7e5fb14a) hit bunq HTTP 400
`"Your device's region setting are not supported by bunq"` during the
installation flow and concluded it was a host/IP geo-restriction that needed
investigation "with a real key". **That diagnosis was wrong.**

Live header-isolation testing (2026-08-17, real bunq endpoints) shows the 400
is caused by the connector's own `X-Bunq-Region: NL` header:

| Headers sent to `POST /installation` | Sandbox `public-api.sandbox.bunq.com` | Prod `api.bunq.com` |
|--------------------------------------|---------------------------------------|---------------------|
| full `_base_headers()` (incl. `X-Bunq-Region: NL`) | **400** region-not-supported | **400** region-not-supported |
| `X-Bunq-Region: NL` alone | **400** region-not-supported | _(not tested)_ |
| geo header only (`X-Bunq-Geolocation: 0 0 0 0 NL`) | **200** OK | **200** OK |
| `X-Bunq-Language: en_US` only | **200** OK | _(not tested)_ |
| no geo headers at all | **200** OK (reaches key validation) | **200** OK |

bunq's API rejects the `X-Bunq-Region` header outright on every endpoint
(sandbox and production). Removing just that header makes the full
installation flow succeed — `POST /installation` → `POST /device-server` →
signed `POST /session-server` → `GET /user/{id}/monetary-account` all return
**HTTP 200** against the real sandbox (verified with a freshly issued
`sandbox-user-person` key).

Host IP is residential KPN NL (`77.175.111.18`, geo NL) — no region issue.

## Impact

Without this fix, the very **first** `authenticate()` call with any real
operator key would 400 with "region setting are not supported", the sync
would never proceed, and no bunq data would ever reach the exporter. This was
the second connector-level blocker, hiding behind the first (missing key).

## Fix

`src/finance_sync/connectors/bunq.py` — `_base_headers()` no longer sends
`X-Bunq-Region`. Headers sent: `X-Bunq-Client-Request-Id`,
`X-Bunq-Geolocation: 0 0 0 0 NL`, `X-Bunq-Language: en_US`,
`Cache-Control`, `User-Agent`. Regression tests assert the header is absent
from every request (install flow + authenticated accounts fetch) while the
geolocation header is still present.

## Live re-verification state (unchanged from t_7e5fb14a)

- **Wealthfolio UI (192.168.3.50:8080):** still 2 accounts (`Smoke Test
  Brokerage`, `snap-test-f83d67`), 3 activities (all `smoke-txn-*`), zero
  bunq — because no bunq credentials are provisioned (credentials table 0
  rows, verified live this run).
- Worker runs f6bbea1 (install-flow fix #260/#261 incl. `_base_url`
  pagination) — confirmed in the workspace clone at f6bbea1; `sync_bunq`
  ticks every 15 min → `sync_job_no_tenants`.

## Status

Connector code-side blockers for a first live bunq sync are now all fixed and
covered by regression tests:
1. full_auth default (PR #260/#261) — fresh keys run the RSA install flow
2. persisted install state per tenant (PR #260/#261, migration 0016)
3. `_next_page_url` uses configured base_url (PR #260/#261)
4. **`X-Bunq-Region` header removed (this PR)** — unblocks every endpoint

Remaining external dependency (operator action): provision a bunq API key
(`POST /api/v1/connectors/configs` `{"provider_type":"bunq","credentials":{"api_key":"..."}}`,
IP-bound to the worker egress IP 77.175.111.18). After that: next
`sync_bunq` tick (15 min) syncs accounts+transactions → next
`export_wealthfolio` tick (5 min) pushes them to Wealthfolio → UI
verification + screenshots + acceptance evidence can be completed.