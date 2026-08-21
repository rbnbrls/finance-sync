---
title: "Beheer security-uitzonderingen met expiry en eigenaar"
status: todo
priority: 25
---

## Context

Trivy- en dependency-uitzonderingen hebben rationale en expiry nodig, maar het
periodieke eigenaarschap en de closeout van uitzonderingen moeten expliciet
worden bewaakt.

## Dependencies

Release 14 security-release-evidence.

## Acceptance criteria

- [ ] Iedere uitzondering bevat CVE/advisory, rationale, eigenaar, issue-link
  en expiry.
- [ ] Een verlopen of incompleet item faalt CI.
- [ ] Een rapport toont openstaande uitzonderingen en vervaldatums.
- [ ] Een opgeloste kwetsbaarheid verwijdert de uitzondering en wordt getest.
- [ ] Het rapport bevat geen secrets of financiële data.
