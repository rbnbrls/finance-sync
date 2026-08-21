---
title: "Verlaag de Pyright-warningbaseline onder 69"
status: todo
priority: 20
---

## Context

De nieuwe read- en persistence-componenten zijn warning-vrij, maar de globale
baseline staat nog op 69 warnings. Release 12 moet type debt ratcheten zonder
nieuwe warnings toe te staan.

## Acceptance criteria

- [ ] Classificeer alle bestaande warnings per module en oorzaak.
- [ ] Los minimaal negen warnings op zodat de baseline maximaal 60 wordt.
- [ ] Nieuwe read-, persistence- en stage-code blijft warning-vrij.
- [ ] De bestaande warning-budgetcheck gebruikt de verlaagde baseline.
- [ ] Ruff, source-Pyright en test-Pyright zijn groen.
- [ ] Wijzigingen bevatten geen brede `Any`- of `type: ignore`-verslechtering.
