---
title: "Automatiseer de disaster-recovery runbookstappen"
status: todo
priority: 25
---

## Context

Release 17 bewijst herstel via een game day, maar de kritieke stappen zijn nog
niet geautomatiseerd en daardoor foutgevoelig tijdens een incident.

## Dependencies

Release 17 disaster-recovery game day en backup/restore-drill.

## Acceptance criteria

- [ ] Maak idempotente scripts/workflows voor database restore, service-start,
  migration-check en outbox-validatie.
- [ ] Laat een dry-run uitvoeren zonder productie-impact.
- [ ] Controleer RPO/RTO, tenant-isolatie en sync-idempotentie na herstel.
- [ ] Log uitsluitend operationele identifiers en geen financiële data.
- [ ] Laat de automation periodiek in een geïsoleerde omgeving draaien.
