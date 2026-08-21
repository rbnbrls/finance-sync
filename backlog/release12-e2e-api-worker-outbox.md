---
title: "Valideer API-worker-outbox end-to-end"
status: todo
priority: 40
---

## Context

De synchronisatie- en outboxcomponenten zijn afzonderlijk getest, maar de
exactly-once waarneembare uitkomst van API tot worker en outbox moet formeel
tegen PostgreSQL en Redis worden bewezen.

## Acceptance criteria

- [ ] Draai de volledige E2E-suite tegen PostgreSQL 16 en Redis 7.
- [ ] Bewijs API-aanvraag, worker-executie, sync-runstatus en outbox-publicatie
  in één testflow.
- [ ] Herlevering of worker-restart veroorzaakt geen dubbele domeinrows of
  dubbele observeerbare outbox-uitkomst.
- [ ] Tenant-scoping en foutafhandeling zijn in de E2E-flow opgenomen.
- [ ] JUnit- en relevante service-logs worden geüpload.
- [ ] Onverwachte skips maken de E2E-job rood.
