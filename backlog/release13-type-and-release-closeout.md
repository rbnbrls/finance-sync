---
title: "Ratcheting van typebaseline en Release 13 closeout"
status: done
priority: 20
---

## Context

Na cleanup en CI-gates moet de repository formeel worden gesloten met een
lagere Pyright-baseline en complete staging-/rollbackdocumentatie.

## Dependencies

Alle cleanup- en CI-gate stories uit Release 13.

## Acceptance criteria

- [x] Pyright-baseline is maximaal 60 warnings en nieuwe componenten zijn
  warning-vrij.
- [x] Ruff, source/test-Pyright, volledige unit/integration/E2E-suite en
  OpenAPI diff zijn groen.
- [x] Staging smoke met synthetische data is uitgevoerd en gedocumenteerd.
- [x] README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook zijn actueel.
- [x] Releasechecklist bevat commit, image-tag, artifactlinks, eigenaar en
  datum.

## Implementatie en verificatie

- De warningbaseline staat op maximaal 60 en CI voert source/test-Pyright,
  Ruff, test- en OpenAPI-gates uit.
- Staging smoke, synthetische data, rollbackbeleid en commit-gebonden
  evidence zijn gedocumenteerd in README, ARCHITECTURE, DATABASE, UPGRADE en
  RELEASING.
- Release 13 checklist toegevoegd met commit, immutable image-tag,
  artifactlinks, eigenaar en UTC-verificatiedatum.
- Contracttests toegevoegd in `tests/test_release13_closeout.py`.
