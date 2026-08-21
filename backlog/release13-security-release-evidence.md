---
title: "Bundel security- en image-scanbewijs voor release candidates"
status: todo
priority: 30
---

## Context

Dependency-, SBOM- en image-scans bestaan in CI, maar release evidence moet
per commit/image-tag herleidbaar en gecontroleerd zijn.

## Dependencies

`release12-security-sbom-image.md`.

## Acceptance criteria

- [ ] Upload pip-audit-, CycloneDX- en Trivy-output per releasecandidate.
- [ ] `.trivyignore`-entries worden op expiry en rationale gecontroleerd.
- [ ] Ongeaccepteerde kwetsbaarheden falen de releasejob.
- [ ] Artifacts bevatten geen secrets, credentials of financiële waarden.
