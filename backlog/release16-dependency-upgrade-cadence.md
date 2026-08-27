---
title: "Automatiseer dependency-updates zonder security-regressies"
status: done
priority: 15
---

## Context

Security-scans detecteren kwetsbaarheden, maar er is nog geen gecontroleerde
cadans voor dependency-updates en compatibiliteitsvalidatie.

## Dependencies

Release 15 security-exception lifecycle.

## Acceptance criteria

- [x] Definieer updatefrequentie, eigenaar en maximale leeftijd van kritieke
  dependencies.
- [x] Laat updates automatisch een unit-, integration-, E2E- en securitygate
  uitvoeren.
- [x] Houd lockfile en SBOM synchroon.
- [x] Laat een mislukte update geen releasecandidate promoten.
- [x] Documenteer uitzonderingen met expiry en rollbackprocedure.

## Implementatie en verificatie

- `.github/dependabot.yml` maakt wekelijkse UV-updates aan met
  `dependencies`/`security` labels; eigenaar is het platform/security-team en
  kritieke updates worden maximaal één week oud gehouden.
- `.github/workflows/dependency-cadence.yml` voert `uv lock --check`, unit-,
  integration-, E2E-, pip-audit-, CycloneDX-, retention- en Trivy-policychecks
  uit.
- De releaseworkflow houdt security-, runtime- en operational gates als
  promotion dependencies; een failure kan dus geen candidate promoten.
- Uitzonderingen blijven onder `.trivyignore` met expiry/rationale en volgen
  dezelfde image-rollbackprocedure uit `docs/RELEASING.md`.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
