---
title: "Automatiseer de disaster-recovery runbookstappen"
status: done
priority: 25
---

## Context

Release 17 bewijst herstel via een game day, maar de kritieke stappen zijn nog
niet geautomatiseerd en daardoor foutgevoelig tijdens een incident.

## Dependencies

Release 17 disaster-recovery game day en backup/restore-drill.

## Acceptance criteria

- [x] Maak idempotente scripts/workflows voor database restore, service-start,
  migration-check en outbox-validatie.
- [x] Laat een dry-run uitvoeren zonder productie-impact.
- [x] Controleer RPO/RTO, tenant-isolatie en sync-idempotentie na herstel.
- [x] Log uitsluitend operationele identifiers en geen financiële data.
- [x] Laat de automation periodiek in een geïsoleerde omgeving draaien.

## Implementatie en verificatie

- `automated_dr_runbook.py` voert restore, service-start, migration-check,
  outbox-validatie en idempotency-probe in vaste volgorde uit; dry-run markeert
  alle stappen als gepland en raakt geen productie.
- `config/automated-dr-runbook.json` bevat RPO 15 minuten, RTO 30 minuten en
  de geïsoleerde service-set. CI draait de dry-run en archiveert evidence.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
