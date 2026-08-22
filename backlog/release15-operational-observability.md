---
title: "Maak release-gates en smoke-resultaten operationeel zichtbaar"
status: done
priority: 20
---

## Context

CI-artifacts bewijzen een release, maar operators hebben een compact overzicht
nodig van gate-status, sync-health, outbox-lag en laatste smoke-run.

## Dependencies

Release 14 release-smoke-evidence en backlog-closeout.

## Acceptance criteria

- [x] Publiceer per releasecandidate een machineleesbare gate-summary.
- [x] Neem unit, integration, E2E, migration, security, benchmark en staging
  op.
- [x] Toon sync-health en outbox-lag zonder financiële waarden of secrets.
- [x] Maak ontbrekende/te oude artifacts zichtbaar als failure.
- [x] Documenteer waar operators logs, JUnit en scanrapporten vinden.

## Implementatie en verificatie

- `operational_gate_summary.py` valideert de zeven verplichte gates, artifact-
  aanwezigheid en artifact-leeftijd en publiceert alleen veilige statusvelden.
- De releaseworkflow downloadt alle evidence, maakt
  `release-operational-summary-${{ github.sha }}` en laat `promote` daarvan
  afhangen.
- `docs/RELEASING.md` beschrijft de locaties van logs, JUnit-, scan- en gate-
  rapporten voor operators.
- Verificatie: `tests/test_release15_operational_observability.py`, Ruff,
  Pyright en `git diff --check` geslaagd.
