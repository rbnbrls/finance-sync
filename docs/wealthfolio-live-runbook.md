# Wealthfolio live delivery runbook

This runbook activates the existing bunq/Trading 212 → finance-sync →
Wealthfolio path. It is safe to execute repeatedly and keeps credentials and
financial values out of repository artifacts.

## Readiness

1. Confirm the approved target and operator consent. Create a Wealthfolio
   backup before the first write.
2. In Coolify, configure `WEALTHFOLIO_SERVER_URL`,
   `WEALTHFOLIO_PASSWORD`, and `WORKER_JOB_EXPORT_ENABLED=true` as managed
   variables. Apply them to both `app` and the singleton `worker`.
3. Deploy both Compose services. The worker uses `Dockerfile.worker`, runs
   migrations on startup, exposes health on port 9090, and registers the
   `export_wealthfolio` five-minute sweep.
4. Configure bunq and Trading 212 through `/api/v1/connectors/configs` and
   run each connection's existing test endpoint. Never paste credential
   values into logs, tickets, fixtures, or this repository.
5. Run the read-only check from the deployed image:

   ```bash
   uv run python scripts/wealthfolio_readiness.py
   ```

   A successful result has authenticated Wealthfolio access and both provider
   configurations present. The output contains only booleans, a commit, date,
   environment, and an error type if the check fails.

## Controlled acceptance run

Use a staging Wealthfolio target first. Limit the run to one bunq and one
Trading 212 connection/account. Run the existing connector sync endpoints,
then invoke the existing manual fallback:

```bash
finance-sync wealthfolio push --account-ids <staging-account-ids> \
  --full-history --max-transactions <small-limit>
```

Record only: run ID, status, counts, source identities, currency/FX and error
category. Verify accounts, activities and holdings through the authenticated
Wealthfolio UI/API without storing response bodies containing values.

Run the same command a second time. Acceptance requires zero new duplicate
activities, stable account count, unchanged delivery cursor for already
delivered records, and stable idempotency keys. The CLI smoke command performs
the same two-pass assertion when an explicitly approved target is used:

```bash
finance-sync wealthfolio smoke --account-ids <staging-account-ids>
```

The checked-in synthetic provider fixtures are the no-secret fallback for CI
and local validation:

```bash
uv run pytest tests/test_staging_dataset.py \
  tests/exporter/test_wealthfolio_live_contract.py -q
```

All scheduled, API-triggered and CLI Wealthfolio pushes use the same
tenant/target Redis lease. A concurrent second push is reported as
`export_in_progress` and must not be started manually. The lease also covers
the legacy environment-based five-minute sweep.

## Failure, timeout and rollback

If a push fails or times out, do not manually replay individual records. Keep
finance-sync as the canonical source, inspect the redacted export/job result,
and retry the same command. Delivery cursors and stable idempotency keys make
the retry safe; a failed batch is not advanced as delivered. If the target
projection is corrupted, restore the Wealthfolio backup and rerun with
`--full-history --rebuild` for the explicitly selected accounts only.
The Data health page exposes the latest failed export and links to its
authenticated retry endpoint; a restart-interrupted run is recorded as
`cancelled` and should not be mistaken for an active export.

## Evidence policy

Store a redacted evidence note containing date, environment, commit, run IDs,
safe counts, statuses, and pass/fail assertions. Do not store passwords,
tokens, account numbers, IBANs, raw API responses, screenshots with balances,
or unredacted logs. The backlog can move to `done` only after the operator
has supplied this evidence for both providers and the two-run check.
