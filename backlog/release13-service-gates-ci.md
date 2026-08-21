---
title: "Maak PostgreSQL-, Redis- en E2E-gates verplicht in CI"
status: todo
priority: 40
---

## Context

De workflow bevat servicejobs, maar Release 13 moet formeel aantonen dat
onverwachte skips en ontbrekende artifacts geen geslaagde release opleveren.

## Dependencies

`release12-database-service-gates.md`,
`release12-e2e-api-worker-outbox.md`.

## Acceptance criteria

- [ ] PostgreSQL/Redis integration en API-worker-outbox E2E draaien op iedere
  releasecandidate.
- [ ] Alleen expliciet gemotiveerde opt-in/provider-tests mogen skippen.
- [ ] Onverwachte skips, ontbrekende JUnit-output of service-startfouten falen.
- [ ] Migration roundtrip-resultaten worden als artifact opgeslagen.
