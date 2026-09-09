# Documentation index

This directory contains current product documentation, operational runbooks
and a small set of architecture decisions. The code and generated OpenAPI
schema are authoritative when a document conflicts with an implementation.

## Start here

- [Architecture](ARCHITECTURE.md): runtime boundaries, data flow and worker.
- [API guide](API.md): resource map, envelopes and examples.
- [Database](DATABASE.md), [migrations](MIGRATIONS.md) and
  [upgrades](UPGRADE.md).
- [Connectors](connectors-overview.md), [connector development](connector-development.md)
  and [plugin development](plugin-development.md).
- [Destinations](destinations.md) and the individual exporter integrations.
- [Control-plane contract](CONTROL_PLANE_CONTRACT.md): tenant isolation,
  statuses, timestamps and recovery-action rules.
- [MCP server](mcp-server.md), the single canonical MCP guide.

## Integrations and features

Connector and destination-specific notes cover DEGIRO, Wealthfolio, Actual
Budget, Firefly III, Ghostfolio, InvestBrain and Securo. Feature guides cover
market intelligence, holding relevance, subscriptions, datamarts, sync
schedules, tax lots and Home Assistant.

## Operations

Operational runbooks cover releases, retention, audit trails, key rotation,
capacity, autoscaling, disaster recovery, provider contracts and monitoring.
They describe procedures, not roadmap commitments; verify environment-specific
values before executing them.

The [Wealthfolio live delivery runbook](wealthfolio-live-runbook.md) covers
Coolify worker deployment, connector configuration, controlled acceptance,
idempotent retry, and rollback.

## Historical material

Architecture decisions live in [`adr/`](adr/). Release plans and superseded
planning documents are not part of the current implementation contract. When a
feature is not implemented, its document must say so explicitly instead of
presenting a planned design as a live endpoint.
