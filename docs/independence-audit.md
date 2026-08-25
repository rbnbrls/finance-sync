# Runtime independence audit

## Verdict

The repository contains its own API, worker, scheduler, exporter triggers,
MCP server and health monitor. None of these components requires Hermes or a
Hermes cron job to run.

## In-repository entry points

| Component | Entry point | Scheduling/trigger |
|---|---|---|
| API | `uvicorn finance_sync.main:app` | HTTP requests |
| Worker | `python -m finance_sync.worker` | APScheduler and tenant schedules |
| CLI | `finance-sync` | Explicit operator command |
| MCP | `python -m finance_sync.mcp` | MCP client connection |
| Health monitor | `finance-sync-monitor` | systemd timer in `deploy/systemd/` |
| Wealthfolio monitor | `finance-sync-wealthfolio-monitor` | Explicit operator/systemd trigger |

The worker owns scheduled syncs, enrichment, outbox/webhook processing,
reconciliation, DEGIRO watchfolders and the configured export sweep. REST and
CLI operations remain available independently of the worker, subject to the
operation's persistence and destination requirements.

## Review rules

- Do not add a dependency on a user's home-directory agent runtime for tokens,
  state or scheduling.
- Read credentials from the documented environment/settings layer only.
- Keep monitoring state in `STATE_FILE` or the configured systemd data path.
- Verify the worker and API health endpoints separately after deployment.

This document records repository architecture. It does not assert that a
particular deployment has configured every optional connector, exporter or
systemd timer.
