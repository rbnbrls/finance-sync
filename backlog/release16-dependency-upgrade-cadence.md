---
title: "Automatiseer dependency-updates zonder security-regressies"
status: todo
priority: 15
---

## Context

Security-scans detecteren kwetsbaarheden, maar er is nog geen gecontroleerde
cadans voor dependency-updates en compatibiliteitsvalidatie.

## Dependencies

Release 15 security-exception lifecycle.

## Acceptance criteria

- [ ] Definieer updatefrequentie, eigenaar en maximale leeftijd van kritieke
  dependencies.
- [ ] Laat updates automatisch een unit-, integration-, E2E- en securitygate
  uitvoeren.
- [ ] Houd lockfile en SBOM synchroon.
- [ ] Laat een mislukte update geen releasecandidate promoten.
- [ ] Documenteer uitzonderingen met expiry en rollbackprocedure.
