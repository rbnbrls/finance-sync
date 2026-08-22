---
title: "Voer een volledige release-candidate rehearsal uit"
status: done
priority: 30
---

## Context

Na modularisering, service-gates en staging smoke ontbreekt één herhaalbare
rehearsal die de volledige promotieketen test vóór productie.

## Dependencies

Alle Release 14-stories en de Release 15 observability/security stories.

## Acceptance criteria

- [x] Bouw een immutable image met vastgelegde commit- en schema-versie.
- [x] Draai migrations, smoke, integration, E2E, benchmarks en security scans
  in vaste volgorde.
- [x] Valideer staging promotion en application-image rollback.
- [x] Produceer één release-summary met alle artifactlinks en gates.
- [x] De rehearsal gebruikt alleen synthetische financiële data.

## Implementatie en verificatie

- `release_rehearsal.py` valideert de vaste gatevolgorde, immutable image,
  commit/schema, synthetische dataset en image-rollbackbeleid.
- De rehearsal-job is een verplichte dependency van `promote` en uploadt
  `release-rehearsal-${{ github.sha }}`.
- Verificatie: `tests/test_release15_rehearsal.py`, Ruff, Pyright en
  `git diff --check` geslaagd.
