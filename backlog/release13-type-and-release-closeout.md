---
title: "Ratcheting van typebaseline en Release 13 closeout"
status: todo
priority: 20
---

## Context

Na cleanup en CI-gates moet de repository formeel worden gesloten met een
lagere Pyright-baseline en complete staging-/rollbackdocumentatie.

## Dependencies

Alle cleanup- en CI-gate stories uit Release 13.

## Acceptance criteria

- [ ] Pyright-baseline is maximaal 60 warnings en nieuwe componenten zijn
  warning-vrij.
- [ ] Ruff, source/test-Pyright, volledige unit/integration/E2E-suite en
  OpenAPI diff zijn groen.
- [ ] Staging smoke met synthetische data is uitgevoerd en gedocumenteerd.
- [ ] README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook zijn actueel.
- [ ] Releasechecklist bevat commit, image-tag, artifactlinks, eigenaar en
  datum.
