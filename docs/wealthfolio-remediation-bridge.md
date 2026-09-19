# Wealthfolio remediation bridge

The bridge is disabled by default. It polls active Wealthfolio export targets,
imports bounded `/api/v1/health/status` findings into the existing remediation
backlog, and automatically handles only canonical quote and historical-price
repairs.

Roll out in this order:

1. Apply migrations and confirm the worker has `REMEDIATION_ENABLED=true`.
2. Set `WEALTHFOLIO_HEALTH_BRIDGE_ENABLED=true` with a bounded interval and
   observe poll failures, imported issue counts, and the remediation backlog.
3. Use the tenant-scoped `POST /api/v1/control-plane/wealthfolio/health-sync`
   endpoint with the `sync:write` permission for a smoke test.
4. Enable automatic remediation only after a successful remote verification
   cycle. The worker reuses the encrypted `ExportTarget` secret; no second
   plaintext credential is supported.

The bridge stores only a payload hash, issue count, timestamps, and bounded
error state in `wealthfolio_health_cursors`. It never stores the raw health
payload. Unknown categories, missing purchase prices, and valuation findings
become `manual_review`; no transactions, transfers, purchase prices, or
valuations are invented.
