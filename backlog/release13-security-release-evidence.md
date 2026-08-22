---
title: "Bundel security- en image-scanbewijs voor release candidates"
status: done
priority: 30
---

## Context

Dependency-, SBOM- en image-scans bestaan in CI, maar release evidence moet
per commit/image-tag herleidbaar en gecontroleerd zijn.

## Dependencies

`release12-security-sbom-image.md`.

## Acceptance criteria

- [x] Upload pip-audit-, CycloneDX- en Trivy-output per releasecandidate.
- [x] `.trivyignore`-entries worden op expiry en rationale gecontroleerd.
- [x] Ongeaccepteerde kwetsbaarheden falen de releasejob.
- [x] Artifacts bevatten geen secrets, credentials of financiële waarden.

## Implementatie en verificatie

- De releaseworkflow heeft een verplichte `security-evidence`-job voor
  pip-audit, CycloneDX en de Trivy-output van de image-build.
- De job valideert `.trivyignore`, controleert evidence op credentials en
  uploadt alle outputs als één releaseartifact; elke scanuitkomst gate de job.
- Contracttests toegevoegd in `tests/test_release13_security_evidence.py`.
