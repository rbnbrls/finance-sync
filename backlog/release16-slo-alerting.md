---
title: "Definieer SLO's en alerts voor sync en outbox"
status: done
priority: 20
---

## Context

Operationele observability toont gate-status, maar productiesignalen moeten
ook aangeven wanneer syncduur, failures of outbox-lag een SLO overschrijden.

## Dependencies

Release 15 operational observability en release rehearsal.

## Acceptance criteria

- [x] Definieer SLO's voor sync-success rate, syncduur, outbox-lag en worker-
  failure rate.
- [x] Voeg metrieklabels toe zonder tenant-ID's, credentials of financiële
  waarden.
- [x] Maak alerts met severity, runbook-link en suppressie voor onderhoud.
- [x] Test alerts met synthetische failures.
- [x] Documenteer dashboards, ownership en escalation path.

## Implementatie en verificatie

- `config/slo-alerts.json` definieert de vier SLO's, veilige labels, severity,
  runbook-links en de `maintenance-window` suppressie.
- `check_slo_alerts.py` valideert het contract en evalueert synthetische
  failure-metrics; gevoelige labels falen policy-validatie.
- CI valideert de policy; `docs/observability.md` documenteert dashboard,
  ownership en escalation path.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
