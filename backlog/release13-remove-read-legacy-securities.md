---
title: "Verwijder legacy securities- en price-SQL uit ReadService"
status: todo
priority: 40
---

## Context

Securities, security prices en top-level prices hebben componenten, maar de
oude SQL blijft in `read_api.py` staan.

## Dependencies

`release13-remove-read-legacy-portfolio.md` en de securities-componenttests.

## Acceptance criteria

- [ ] Verwijder de onbereikbare securities-, listing- en priceblokken.
- [ ] Search, filters, pagination, latest-price-batch en freshness blijven
  gelijk.
- [ ] Security identity- en top-level price-tests slagen.
- [ ] OpenAPI en querybudget-contracten blijven ongewijzigd.
