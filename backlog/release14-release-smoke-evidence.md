---
title: "Maak release smoke-evidence herleidbaar"
status: done
priority: 20
---

## Context

Staging smoke, image build en service-gates moeten samen één herleidbaar
releasebewijs opleveren.

## Dependencies

Release 13 service-gates en security-evidence.

## Acceptance criteria

- [x] Definieer één smoke-run met image-tag, commit, schema-versie en
  synthetische dataset.
- [x] Controleer readiness, authentication, read API, sync, outbox en exporter.
- [x] Sla logs, JUnit-output en smoke-summary op als artifacts.
- [x] Laat ontbrekende artifacts of een verkeerde image-tag de job falen.
- [x] Documenteer reproduceerbare lokale/CI-aanroep.

## Implementatie en verificatie

- Smoke evidence bevat commit, immutable image-tag, schema-versie,
  synthetische dataset en veilige checksummary.
- De releasejob uploadt log, JSON-summary en JUnit XML en valideert dat alle
  bestanden bestaan en de image-tag het `sha-*` immutable formaat gebruikt.
- Lokale aanroep:
  `SMOKE_BASE_URL=https://<staging-host> SMOKE_ARTIFACT=staging-smoke-evidence.json SMOKE_JUNIT=staging-smoke.xml python3 scripts/release_smoke.py`.
- Verificatie: smoke-evidence contracttests, Ruff en `git diff --check`
  geslaagd.
- Tests: `tests/test_release14_smoke_evidence.py` en
  `tests/test_release12_staging_smoke.py`.
- CI/artifact: `release-staging-smoke-${{ github.sha }}` bevat log, JSON-summary
  en JUnit XML.
