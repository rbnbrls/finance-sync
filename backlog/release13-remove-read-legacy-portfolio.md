---
title: "Verwijder legacy portfolio- en holdings-SQL uit ReadService"
status: todo
priority: 40
---

## Context

Portfolio en holdings delegeren al naar `PortfolioReadService`, maar de oude
implementaties staan nog als onbereikbare code in `read_api.py`.

## Dependencies

De bestaande portfolio-characterization tests en
`release12-read-facade-legacy-cleanup.md`.

## Acceptance criteria

- [ ] Verwijder alleen de onbereikbare portfolio-/holdingsblokken.
- [ ] `get_portfolio()` en `get_holdings()` blijven dezelfde responses,
  scopefilters en metadata leveren.
- [ ] Portfolio- en holdings-tests plus OpenAPI-generatie slagen.
- [ ] Geen nieuwe import van facade-DTO's in querylogica.
