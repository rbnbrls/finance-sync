---
title: "Verwijder legacy securities- en price-SQL uit ReadService"
status: done
priority: 40
---

## Context

Securities, security prices en top-level prices hebben componenten, maar de
oude SQL blijft in `read_api.py` staan.

## Dependencies

`release13-remove-read-legacy-portfolio.md` en de securities-componenttests.

## Acceptance criteria

- [x] Verwijder de onbereikbare securities-, listing- en priceblokken.
- [x] Search, filters, pagination, latest-price-batch en freshness blijven
  gelijk.
- [x] Security identity- en top-level price-tests slagen.
- [x] OpenAPI en querybudget-contracten blijven ongewijzigd.

## Implementatie en verificatie

- De securities-, listing- en price-query's blijven uitsluitend in
  `SecuritiesReadService`; de compatibility-facade bevat geen query-SQL.
- Securities-response-DTO's worden rechtstreeks uit `read.schemas` gebruikt.
- Characterizationtests bewaken facade-compositie, component-eigenaarschap en
  de DTO-module.

Verificatie: securities-, price-, read-facade- en OpenAPI-tests geslaagd; Ruff,
Pyright en `git diff --check` geslaagd.
