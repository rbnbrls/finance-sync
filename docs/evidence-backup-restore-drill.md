# Evidence: PostgreSQL backup/restore-drill

Datum: 2026-09-01

De lokale PostgreSQL 16-testcontainer heeft de synthetic drill uitgevoerd met
dezelfde custom-format dump/restore-stappen als de CI-job:

1. Synthetic tenantdata geschreven naar schema `release16_backup_drill`.
2. `pg_dump --format=custom --no-owner --no-privileges` uitgevoerd.
3. Geïsoleerde database `finance_sync_restore` aangemaakt.
4. Schema verwijderd en met `pg_restore --clean --if-exists --no-owner`
   teruggezet.
5. Gerestaureerde rijen gecontroleerd.

Resultaat:

```text
restored_rows=2|2|tenant-acme,tenant-beta
```

De drill bevatte uitsluitend synthetische data; er zijn geen credentials of
productiedata in de evidence opgenomen.
