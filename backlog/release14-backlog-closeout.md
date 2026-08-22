---
title: "Sluit gerealiseerde release-stories gecontroleerd af"
status: done
priority: 10
---

## Context

De backlog bevat veel release-items met historische status en afhankelijkheden.
Na uitvoering van Release 13 moet de backlog betrouwbaar aangeven wat werkelijk
is geleverd.

## Dependencies

Alle Release 13- en Release 14-gates.

## Acceptance criteria

- [x] Controleer iedere story tegen de feitelijke code en CI-artifacts.
- [x] Zet alleen stories op `done` wanneer alle acceptatiecriteria aantoonbaar
  zijn afgerond.
- [x] Markeer vervangen of samengevoegde stories expliciet als `cancelled` of
  documenteer de vervanging.
- [x] Voeg links naar commits, tests en artifacts toe aan afgeronde stories.
- [x] Werk `backlog/README.md` niet inhoudelijk afwijkend bij; behoud de
  bestaande pipelineconventies.

## Implementatie en verificatie

- Automatische audit toegevoegd in `scripts/check_release14_backlog.py` en
  `tests/test_release14_backlog_closeout.py`.
- Alle vier voorafgaande Release 14-stories zijn gecontroleerd op `done`,
  complete criteria, implementatie/verificatie, testverwijzingen en
  CI-artifactverwijzingen. Er zijn geen vervangen stories vastgesteld.
- Committraceability loopt via `${{ github.sha }}` in de release-artifacts;
  lokale test- en artifactlinks staan per story vermeld.
- Verificatie: backlog-audit, Release 14-contracttests, Ruff, Pyright en
  `git diff --check` geslaagd.
