---
title: "Maak PostgreSQL-, Redis- en E2E-gates verplicht in CI"
status: done
priority: 40
---

## Context

De workflow bevat servicejobs, maar Release 13 moet formeel aantonen dat
onverwachte skips en ontbrekende artifacts geen geslaagde release opleveren.

## Dependencies

`release12-database-service-gates.md`,
`release12-e2e-api-worker-outbox.md`.

## Acceptance criteria

- [x] PostgreSQL/Redis integration en API-worker-outbox E2E draaien op iedere
  releasecandidate.
- [x] Alleen expliciet gemotiveerde opt-in/provider-tests mogen skippen.
- [x] Onverwachte skips, ontbrekende JUnit-output of service-startfouten falen.
- [x] Migration roundtrip-resultaten worden als artifact opgeslagen.

## Verification

- `release-gates` is toegevoegd aan de tag/manual releaseworkflow en blokkeert
  staging deployment bij falende PostgreSQL-, Redis- of E2E-tests.
- De gates schrijven JUnit-rapporten en logs naar artifacts en falen op skips
  of ontbrekende rapporten via `scripts/check_junit_no_skips.py`.
- De migratiejob voert `upgrade → downgrade base → upgrade` uit en uploadt
  `migration.log` plus de PostgreSQL-servicelog.
- Contracttests: `tests/test_release_ci_gate_contract.py`.
