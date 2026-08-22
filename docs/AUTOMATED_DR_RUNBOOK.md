# Automated disaster-recovery runbook

The runbook is defined in `config/automated-dr-runbook.json` and executed by
`scripts/automated_dr_runbook.py`. It is deliberately idempotent and runs in
this order: restore database, start API, start worker, check migration head,
validate outbox, and run an idempotency probe.

Run the safe local check with:

```bash
uv run python scripts/automated_dr_runbook.py --dry-run
```

The isolated execution target is PostgreSQL, Redis, API and worker in a
non-production environment. The report records only operational identifiers,
RPO/RTO, tenant isolation and status flags. Production is never touched by a
dry-run. CI executes the dry-run periodically through the scheduled CI
workflow; the full restore procedure remains the production rollback and
backup procedure documented in `docs/RELEASING.md`.
