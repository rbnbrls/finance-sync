# DEGIRO pension imports

finance-sync imports only official files supplied by an administrator. It does
not accept DEGIRO login details and does not use private endpoints.

## Control-panel upload

Configure a `degiro_pension` connector, open **Connectors**, and select
**Import**. Choose any combination of the transaction, account-statement and
portfolio exports. The first request is a dry-run: it displays detected report
types, account label, period, row counts, missing reports, warnings, unresolved
instruments and possible duplicates. Confirming uses the exact staged bytes and
hashes from that preview.

The API equivalents are:

- `POST /api/v1/connectors/degiro-pension/imports/preview` — multipart fields
  `connection_id` and one or more `files`;
- `POST /api/v1/connectors/degiro-pension/imports/{run_id}/confirm` — confirms
  the immutable preview; `retain_encrypted` and `force_reimport` default false;
- `GET /api/v1/connectors/degiro-pension/imports` — tenant-scoped history and
  freshness;
- `POST .../{run_id}/retry` and `DELETE .../{run_id}/files` — explicit audited
  recovery actions.

Defaults are 20 MiB per file, 100,000 rows, 12 files and a 30-minute preview
TTL. Configure these with `DEGIRO_IMPORT_MAX_FILE_BYTES`,
`DEGIRO_IMPORT_MAX_ROWS`, `DEGIRO_IMPORT_MAX_FILES` and
`DEGIRO_IMPORT_PREVIEW_TTL_MINUTES`.

Uploads are streamed into a tenant-scoped mode-0700 staging directory. The
extension, file signature/archive structure, parsed report structure and cells
are validated. XLSX expansion ratios are bounded and formula-like cells are
rejected. Originals are removed after confirmation or failure. If an admin
explicitly selects retention, files are AES-256-GCM encrypted with
`MASTER_ENCRYPTION_KEY`; the audit API shows that retention is active.

## Self-hosted watchfolder

Set the connector option `watchfolder` to the path visible **inside the worker
container**, normally `/imports/degiro/incoming`. Optional
`archive_directory` and `quarantine_directory` values default to sibling
directories under the watchfolder. The Docker Compose example mounts
`DEGIRO_WATCH_HOST_PATH` at `/imports/degiro` in the worker.

Create the host directories with ownership matching the container user and
permissions `0700`. Do not make the directory web-accessible. Files are only
claimed after `DEGIRO_WATCH_STABLE_SECONDS` (default 10) and are atomically
renamed into a private processing directory. A complete batch succeeds or
fails together. Successful and duplicate batches move to archive; invalid
batches move once to quarantine and therefore cannot cause retry storms.

The worker scans every 60 seconds by default. Configure
`WORKER_JOB_DEGIRO_WATCH_ENABLED` and
`WORKER_JOB_DEGIRO_WATCH_INTERVAL_SECONDS` as needed.

## Privacy, backup and recovery

`ImportRun` stores hashes, report types, counts, periods, warnings and scrubbed
errors—not financial values or server paths. Application logs only include
tenant/run identifiers and counts. Do not include the transient staging volume
in backups. Decide separately whether the operator-managed archive belongs in
encrypted backups and define a retention period; exports contain sensitive
financial data.

For a failed batch, inspect its warning and missing-report metadata, correct the
source export and upload again. For watchfolders, use the explicit retry action
after correcting an operational issue, or permanently delete the quarantined
files. Rotating or losing `MASTER_ENCRYPTION_KEY` makes retained uploads
unreadable, so include the key in the deployment's secret-recovery procedure.
