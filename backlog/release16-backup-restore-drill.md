---
title: "Test database-backup en restore van de financiële datalaag"
status: done
priority: 25
---

## Context

Release rehearsals valideren image rollback, maar nog niet of een PostgreSQL-
backup bruikbaar kan worden teruggezet zonder tenant- of outboxcorruptie.

## Dependencies

Release 15 release-candidate rehearsal en service-gates.

## Acceptance criteria

- [x] Maak een synthetische PostgreSQL-backup met schema, domain rows en
  outbox-state.
- [x] Restore naar een lege database en valideer row counts, constraints,
  migration head en tenant-isolatie.
- [x] Bewijs dat credentials niet in de backup of logs staan.
- [x] Documenteer RPO/RTO-aannames en restorecommando's.
- [x] Voer de drill periodiek uit in een geïsoleerde CI/stagingomgeving.

## Implementatie en verificatie

- `scripts/backup_restore_drill.py` maakt een synthetische PostgreSQL-fixture,
  voert `pg_dump`/`pg_restore` uit en valideert domain-, outbox- en tenant-
  aantallen. Het rapport bevat uitsluitend veilige metadata.
- CI draait de drill tegen twee geïsoleerde PostgreSQL-databases en uploadt
  `backup-restore-drill-${{ github.sha }}`.
- RPO is 15 minuten en RTO 30 minuten; de restore gebruikt `pg_restore
  --clean --if-exists` naar een lege database. Productiecredentials worden
  nooit als fixture gebruikt.
- Verificatie: `tests/test_release16_backup_restore.py`, Ruff en Pyright
  geslaagd.
