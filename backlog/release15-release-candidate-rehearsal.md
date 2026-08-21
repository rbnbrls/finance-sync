---
title: "Voer een volledige release-candidate rehearsal uit"
status: todo
priority: 30
---

## Context

Na modularisering, service-gates en staging smoke ontbreekt één herhaalbare
rehearsal die de volledige promotieketen test vóór productie.

## Dependencies

Alle Release 14-stories en de Release 15 observability/security stories.

## Acceptance criteria

- [ ] Bouw een immutable image met vastgelegde commit- en schema-versie.
- [ ] Draai migrations, smoke, integration, E2E, benchmarks en security scans
  in vaste volgorde.
- [ ] Valideer staging promotion en application-image rollback.
- [ ] Produceer één release-summary met alle artifactlinks en gates.
- [ ] De rehearsal gebruikt alleen synthetische financiële data.
