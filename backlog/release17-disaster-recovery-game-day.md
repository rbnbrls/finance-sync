---
title: "Voer een disaster-recovery game day uit"
status: todo
priority: 25
---

## Context

Een backup/restore-drill bewijst dataherstel, maar niet het volledige herstel
van API, worker, Redis, database en outbox na een incident.

## Dependencies

Release 16 backup/restore, SLO-alerting en release rehearsal.

## Acceptance criteria

- [ ] Simuleer databaseverlies, Redisverlies en workeruitval met synthetische
  data.
- [ ] Herstel de minimale stack volgens het rollback/DR-runbook.
- [ ] Meet en rapporteer RPO, RTO, verloren/replayed outbox-events en sync-
  eindstatus.
- [ ] Bewijs tenant-isolatie en idempotentie na herstel.
- [ ] Leg verbeteracties, eigenaar en deadline vast.
