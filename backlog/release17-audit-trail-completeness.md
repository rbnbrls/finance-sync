---
title: "Controleer volledigheid van het audit trail"
status: todo
priority: 20
---

## Context

Security- en releasegates bevatten auditinformatie, maar wijzigingen aan
credentials, syncconfiguratie, security resolution en exports moeten als één
controleerbaar spoor kunnen worden onderzocht.

## Dependencies

Release 16 dataretentie/privacy-audit en Release 15 security-exceptions.

## Acceptance criteria

- [ ] Inventariseer alle security- en configuratiewijzigingen die auditbaar
  moeten zijn.
- [ ] Test actor, timestamp, tenant, objecttype, actie en redacted diff.
- [ ] Bewijs dat secrets, tokens en financiële waarden niet in auditrecords
  terechtkomen.
- [ ] Controleer read-only toegang en retentie van auditrecords.
- [ ] Voeg een exporteerbaar incidentonderzoekvoorbeeld toe met synthetische
  data.
