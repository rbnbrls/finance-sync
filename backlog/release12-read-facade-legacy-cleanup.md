---
title: "Verwijder legacy-SQL uit de read-facade"
status: todo
priority: 40
---

## Context

De portfolio-, holdings-, securities- en analyticsservices bestaan inmiddels,
maar `services/read_api.py` bevat nog onbereikbare legacy-SQL na de nieuwe
delegatieblokken. Dit maakt onderhoud en review onnodig moeilijk.

## Acceptance criteria

- [ ] Verwijder onbereikbare legacyblokken uit `read_api.py` zonder publieke
  methodesignatures of response-schema's te wijzigen.
- [ ] `ReadService` bevat uitsluitend facade-/delegatielogica en gedeelde
  compatibiliteitshelpers die daadwerkelijk worden gebruikt.
- [ ] Portfolio, holdings, securities, prices, net-worth en cashflow blijven
  tenant- en account-scoped werken.
- [ ] OpenAPI-output vóór en na de wijziging is inhoudelijk gelijk.
- [ ] Read-component-, API- en characterizationtests zijn groen.
- [ ] `read_api.py` is maximaal 300 regels.
