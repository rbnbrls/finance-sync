---
title: "Maak release smoke-evidence herleidbaar"
status: done
priority: 20
---

## Context

Staging smoke, image build en service-gates leveren één herleidbaar
releasebewijs op.

## Acceptance criteria

- [x] Smoke-evidence bevat image-tag, commit, schema-versie en dataset.
- [x] Readiness, authentication, read API, sync, outbox en exporter worden gecontroleerd.
- [x] Logs, JUnit-output en smoke-summary worden artifacts.
- [x] Ontbrekende artifacts of een verkeerde image-tag laten de job falen.
- [x] De lokale en CI-aanroep is gedocumenteerd.

## Implementatie en verificatie

- Evidence bevat commit, immutable image-tag, schema-versie en veilige checks.
- De releasejob valideert artifacts en het `sha-*` image-tagformaat.
- Lokale aanroep gebruikt `scripts/release_smoke.py` met artifact- en JUnit-paden.
- Verificatie: smoke-evidence contracttests en Ruff geslaagd.
- Tests: `tests/test_release14_smoke_evidence.py` en staging-smoke-tests.
- CI/artifact: `release-staging-smoke-${{ github.sha }}`.
