---
title: "Verwijder legacy-SQL uit de read-facade"
status: done
priority: 40
---

## Context

De portfolio-, holdings-, securities- en analyticsservices bestaan inmiddels,
maar `services/read_api.py` bevat nog onbereikbare legacy-SQL na de nieuwe
delegatieblokken. Dit maakt onderhoud en review onnodig moeilijk.

## Acceptance criteria

- [x] Verwijder onbereikbare legacyblokken uit `read_api.py` zonder publieke
  methodesignatures of response-schema's te wijzigen.
- [x] `ReadService` bevat uitsluitend facade-/delegatielogica en gedeelde
  compatibiliteitshelpers die daadwerkelijk worden gebruikt.
- [x] Portfolio, holdings, securities, prices, net-worth en cashflow blijven
  tenant- en account-scoped werken.
- [x] OpenAPI-output vóór en na de wijziging is inhoudelijk gelijk.
- [x] Read-component-, API- en characterizationtests zijn groen.
- [x] `read_api.py` is maximaal 300 regels.

## Verification

- `ReadService` is teruggebracht tot een composition facade van de account-,
  portfolio-, securities-, analytics- en operationele read-componenten.
- Response-schema's zijn verplaatst naar `services/read/schemas.py` en blijven
  vanuit `read_api.py` geëxporteerd voor backward compatibility.
- Onbereikbare legacy-SQL is verwijderd; `read_api.py` is 113 regels.
- Contracttest toegevoegd: `tests/test_read_facade_contract.py`.
- Verificatie: `201 passed`, Pyright `0 errors`, Ruff en `git diff --check`
  geslaagd.
