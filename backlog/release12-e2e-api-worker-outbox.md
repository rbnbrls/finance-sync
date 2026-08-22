---
title: "Valideer API-worker-outbox end-to-end"
status: done
priority: 40
---

## Context

De synchronisatie- en outboxcomponenten zijn afzonderlijk getest, maar de
exactly-once waarneembare uitkomst van API tot worker en outbox moet formeel
tegen PostgreSQL en Redis worden bewezen.

## Acceptance criteria

- [x] Draai de volledige E2E-suite tegen PostgreSQL 16 en Redis 7.
- [x] Bewijs API-aanvraag, worker-executie, sync-runstatus en outbox-publicatie
  in één testflow.
- [x] Herlevering of worker-restart veroorzaakt geen dubbele domeinrows of
  dubbele observeerbare outbox-uitkomst.
- [x] Tenant-scoping en foutafhandeling zijn in de E2E-flow opgenomen.
- [x] JUnit- en relevante service-logs worden geüpload.
- [x] Onverwachte skips maken de E2E-job rood.

## Verification

- PostgreSQL 16 / Redis 7: `31 passed, 3259 deselected, 0 skipped`.
- Covered API sync, worker delivery, sync-run status, transactional outbox,
  redelivery after a simulated worker crash, tenant isolation and destination
  failure recovery.
- JUnit skip checker passed; CI uploads the JUnit, E2E log and service logs.
