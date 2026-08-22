---
title: "Verlaag de Pyright-warningbaseline onder 69"
status: done
priority: 20
---

## Context

De nieuwe read- en persistence-componenten zijn warning-vrij, maar de globale
baseline staat nog op 69 warnings. Release 12 moet type debt ratcheten zonder
nieuwe warnings toe te staan.

## Acceptance criteria

- [x] Classificeer alle bestaande warnings per module en oorzaak.
- [x] Los minimaal negen warnings op zodat de baseline maximaal 60 wordt.
- [x] Nieuwe read-, persistence- en stage-code blijft warning-vrij.
- [x] De bestaande warning-budgetcheck gebruikt de verlaagde baseline.
- [x] Ruff, source-Pyright en test-Pyright zijn groen.
- [x] Wijzigingen bevatten geen brede `Any`- of `type: ignore`-verslechtering.

## Implementatie en verificatie

- De source-baseline is van 69 naar 60 warnings gebracht. De resterende
  warnings zijn per module en oorzaak vastgelegd in
  `docs/PYRIGHT_WARNING_DEBT.md`.
- Negen concrete warnings zijn opgelost in Plaid-like payloadvalidatie,
  enrichment-DTO's, FX-responsvalidatie, Intel-typing, Actual Budget en
  exporter-configuratie.
- De bestaande `scripts/check_pyright_budget.py` ratchet nu tegen `60`.
- Contracttests bewaken de baseline en classificatiedocumentatie.

Verificatie:

```text
uv run pyright -p pyproject.toml src
0 errors, 60 warnings

uv run python scripts/check_pyright_budget.py \
--baseline config/pyright-warning-budget.json src
Pyright: 0 errors, 60 warnings (budget 60)

uv run pyright -p pyrightconfig.tests.json tests
0 errors

uv run pytest tests/test_release12_pyright_debt.py -q
2 passed
```
