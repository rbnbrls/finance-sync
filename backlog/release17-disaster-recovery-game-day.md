---
title: "Voer een disaster-recovery game day uit"
status: done
priority: 25
---

## Context

Een backup/restore-drill bewijst dataherstel, maar niet het volledige herstel
van API, worker, Redis, database en outbox na een incident.

## Dependencies

Release 16 backup/restore, SLO-alerting en release rehearsal.

## Acceptance criteria

- [x] Simuleer databaseverlies, Redisverlies en workeruitval met synthetische
  data.
- [x] Herstel de minimale stack volgens het rollback/DR-runbook.
- [x] Meet en rapporteer RPO, RTO, verloren/replayed outbox-events en sync-
  eindstatus.
- [x] Bewijs tenant-isolatie en idempotentie na herstel.
- [x] Leg verbeteracties, eigenaar en deadline vast.

## Implementatie en verificatie

- `config/dr-game-day.json` definieert synthetische database-, Redis- en
  workerstoringsscenario's met RPO/RTO en verbeteracties.
- `scripts/dr_game_day.py` publiceert herstelstatus, replay-verlies,
  tenant-isolatie, idempotentie en veilige-dataflags.
- CI voert de game day uit en archiveert `dr-game-day-${{ github.sha }}`.
- Verificatie: 2 tests, Ruff en Pyright geslaagd.
