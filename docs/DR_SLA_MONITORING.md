# Disaster-recovery SLA (RPO/RTO) monitoring

The Release 18 automated runbook proves the recovery *steps*; this
release-19 story proves the recovery *targets* are actually met, on a
schedule, with synthetic data and zero production impact.

## What is measured

Every check publishes a status report with:

- **restore duration** — how long the synthetic restore took (seconds)
- **last usable backup** — the age of the newest usable backup (seconds;
  raw timestamps are never published)
- **replay-lag** — how far the restored data lags the source WAL position
  (seconds, floored at zero so clock skew can never produce a negative lag)
- **recovery status** — `success` / `failed` / `unknown` / `noop`

## How it runs

- `config/dr-sla-monitoring.json` holds the SLA contract: RPO 15 minutes,
  RTO 30 minutes, check interval 60 minutes, alert dedup window 60
  minutes, owner `finance-platform-oncall` and the runbook link.
- `scripts/dr_sla_monitoring.py` runs the check.  In CI
  (`dr-sla-monitoring` job) it performs a **real isolated restore-check**:
  a tenant-prefixed synthetic schema is seeded on an ephemeral Postgres,
  dumped with `pg_dump`, restored with `pg_restore` onto a second isolated
  database and verified.  The restore duration is measured with a
  monotonic clock.  Without `--source-url/--target-url` it runs in
  synthetic-only mode (pure computation, no I/O).

Run it locally in synthetic mode:

```bash
uv run python scripts/dr_sla_monitoring.py --tenant tenant-acme
```

## Alerts

Grafana rules (provisioned in
`docker/grafana/provisioning/alerting/finance-sync.rules.yaml`):

| Rule | Condition | Meaning |
|------|-----------|---------|
| `finance-sync-dr-rpo-breach` | `dr_sla_last_usable_backup_age_seconds > 900` for 15m | newest usable backup older than the 15-minute RPO |
| `finance-sync-dr-rto-breach` | `dr_sla_replay_lag_seconds > 1800` for 15m | replay-lag exceeds the 30-minute RTO |

Every alert carries a runbook link and the owner
(`finance-platform-oncall`).  Alerts are deduplicated per tenant per
60-minute window so a burst of consecutive failing checks pages at most
once per interval.

## Safety guarantees

- **No tenant data** — tenant IDs are reduced to an opaque operational
  label; backup timestamps are published only as ages; financial values
  never appear.
- **No credentials** — every published payload (status *and* failure
  paths) is scrubbed against credential patterns, including nested error
  fields.
- **Tenant isolation** — a restore-check for tenant A only ever selects
  backups owned by A; a foreign backup id is rejected with zero bytes.
- **No false RPO-green** — an empty inventory yields status != success
  with `last_usable_backup = null`.
- **Partial runs detected** — a run that crashed mid-way (duration
  measured, replay-lag never written) is published as `unknown`, never
  success.
- **Injection safe** — `tenant_id` / `runbook_id` are validated against a
  strict pattern; they never reach SQL or shell unparameterised.

## Failure handling

Any failure links to the runbook
(`docs/AUTOMATED_DR_RUNBOOK.md`) and the owner
(`finance-platform-oncall`).  Escalation: on-call operator first, then
platform owner for infrastructure failures.
