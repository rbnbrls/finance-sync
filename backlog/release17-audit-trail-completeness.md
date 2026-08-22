---
title: "Controleer volledigheid van het audit trail"
status: done
priority: 20
---

## Context

Security- en releasegates bevatten auditinformatie, maar wijzigingen aan
credentials, syncconfiguratie, security resolution en exports moeten als één
controleerbaar spoor kunnen worden onderzocht.

## Dependencies

Release 16 dataretentie/privacy-audit en Release 15 security-exceptions.

## Acceptance criteria

- [x] Inventariseer alle security- en configuratiewijzigingen die auditbaar
  moeten zijn.
- [x] Test actor, timestamp, tenant, objecttype, actie en redacted diff.
- [x] Bewijs dat secrets, tokens en financiële waarden niet in auditrecords
  terechtkomen.
- [x] Controleer read-only toegang en retentie van auditrecords.
- [x] Voeg een exporteerbaar incidentonderzoekvoorbeeld toe met synthetische
  data.

## Implementatie en verificatie

- `config/audit-trail-policy.json` inventariseert credential-, sync-config-,
  security-resolution- en exportwijzigingen met verplichte auditvelden,
  read-only rollen en retentie.
- `config/incident-audit-example.json` is een exporteerbaar synthetisch
  incidentonderzoek; `audit_trail_completeness.py` valideert volledigheid en
  redacted diffs.
- CI valideert het beleid en het voorbeeld; bestaande tenant-scoped audit-API
  blijft read-only.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
