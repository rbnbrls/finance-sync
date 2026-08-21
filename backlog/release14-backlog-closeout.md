---
title: "Sluit gerealiseerde release-stories gecontroleerd af"
status: todo
priority: 10
---

## Context

De backlog bevat veel release-items met historische status en afhankelijkheden.
Na uitvoering van Release 13 moet de backlog betrouwbaar aangeven wat werkelijk
is geleverd.

## Dependencies

Alle Release 13- en Release 14-gates.

## Acceptance criteria

- [ ] Controleer iedere story tegen de feitelijke code en CI-artifacts.
- [ ] Zet alleen stories op `done` wanneer alle acceptatiecriteria aantoonbaar
  zijn afgerond.
- [ ] Markeer vervangen of samengevoegde stories expliciet als `cancelled` of
  documenteer de vervanging.
- [ ] Voeg links naar commits, tests en artifacts toe aan afgeronde stories.
- [ ] Werk `backlog/README.md` niet inhoudelijk afwijkend bij; behoud de
  bestaande pipelineconventies.
