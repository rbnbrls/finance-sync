---
title: "Beheer security-uitzonderingen met expiry en eigenaar"
status: done
priority: 25
---

## Context

Trivy- en dependency-uitzonderingen hebben rationale en expiry nodig, maar het
periodieke eigenaarschap en de closeout van uitzonderingen moeten expliciet
worden bewaakt.

## Dependencies

Release 14 security-release-evidence.

## Acceptance criteria

- [x] Iedere uitzondering bevat CVE/advisory, rationale, eigenaar, issue-link
  en expiry.
- [x] Een verlopen of incompleet item faalt CI.
- [x] Een rapport toont openstaande uitzonderingen en vervaldatums.
- [x] Een opgeloste kwetsbaarheid verwijdert de uitzondering en wordt getest.
- [x] Het rapport bevat geen secrets of financiële data.

## Implementatie en verificatie

- `security_exception_report.py` combineert de bestaande expiry/rationale-
  validatie met eigenaar, advisory en issue-link per open exception.
- CI genereert en uploadt `security-exceptions.json`; verlopen of incomplete
  entries stoppen de security-job.
- Verificatie: `tests/test_release15_security_exceptions.py`, bestaande
  Trivy-policytests, Ruff, Pyright en `git diff --check` geslaagd.
