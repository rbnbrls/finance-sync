---
title: "Test database-backup en restore van de financiële datalaag"
status: todo
priority: 25
---

## Context

Release rehearsals valideren image rollback, maar nog niet of een PostgreSQL-
backup bruikbaar kan worden teruggezet zonder tenant- of outboxcorruptie.

## Dependencies

Release 15 release-candidate rehearsal en service-gates.

## Acceptance criteria

- [ ] Maak een synthetische PostgreSQL-backup met schema, domain rows en
  outbox-state.
- [ ] Restore naar een lege database en valideer row counts, constraints,
  migration head en tenant-isolatie.
- [ ] Bewijs dat credentials niet in de backup of logs staan.
- [ ] Documenteer RPO/RTO-aannames en restorecommando's.
- [ ] Voer de drill periodiek uit in een geïsoleerde CI/stagingomgeving.
