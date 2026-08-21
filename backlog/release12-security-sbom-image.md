---
title: "Sluit security-, SBOM- en image-gates voor Release 12"
status: todo
priority: 30
---

## Context

Release 12 vereist reproduceerbaar bewijs voor dependency-, SBOM- en
containerveiligheid voordat de modularisering als production-ready geldt.

## Acceptance criteria

- [ ] `pip-audit` draait tegen de release-lockfile en faalt bij ongeaccepteerde
  kwetsbaarheden.
- [ ] CycloneDX-SBOM wordt gegenereerd en als CI-artifact opgeslagen.
- [ ] Trivy scant de gebouwde image met `.trivyignore` als tijdelijke,
  gemotiveerde en verlopen controleerbare uitzondering.
- [ ] Iedere ignore-entry bevat rationale en expiry; verlopen entries falen.
- [ ] Secrets, financiële waarden en credentials staan niet in logs of
  artifacts.
- [ ] Scanresultaten zijn aan commit en image-tag gekoppeld.
