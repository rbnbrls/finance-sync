---
title: "Voer staging smoke en rollback-evidence uit"
status: todo
priority: 20
---

## Context

De release moet aantonen dat de gemodulariseerde applicatie operationeel
start, synchroniseert en veilig kan worden teruggedraaid zonder een
productiedowngrade.

## Acceptance criteria

- [ ] Draai staging smoke met uitsluitend synthetische financiële data.
- [ ] Controleer readiness, health, sync, outbox en exporter smoke flows.
- [ ] Controleer image rollback met backward-compatible database migrations.
- [ ] Documenteer dat rollback via application-image rollback verloopt en
  niet via blind schema-downgrade.
- [ ] Leg commit, image-tag, omgeving, datum en artifact-links vast.
- [ ] Werk README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook bij.
