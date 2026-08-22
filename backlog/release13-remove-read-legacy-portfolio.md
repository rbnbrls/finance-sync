---
title: "Verwijder legacy portfolio- en holdings-SQL uit ReadService"
status: done
priority: 40
---

## Context

Portfolio en holdings delegeren al naar `PortfolioReadService`, maar de oude
implementaties staan nog als onbereikbare code in `read_api.py`.

## Dependencies

De bestaande portfolio-characterization tests en
`release12-read-facade-legacy-cleanup.md`.

## Acceptance criteria

- [x] Verwijder alleen de onbereikbare portfolio-/holdingsblokken.
- [x] `get_portfolio()` en `get_holdings()` blijven dezelfde responses,
  scopefilters en metadata leveren.
- [x] Portfolio- en holdings-tests plus OpenAPI-generatie slagen.
- [x] Geen nieuwe import van facade-DTO's in querylogica.

## Implementatie en verificatie

- De portfolio- en holdingsimplementaties blijven uitsluitend in
  `PortfolioReadService`; de compatibility-facade bevat geen SQL.
- Response-DTO's worden rechtstreeks uit `read.schemas` geïmporteerd, zodat
  querylogica niet afhankelijk is van facade-imports.
- Characterizationtests bewaken delegatie, DTO-locatie en het ontbreken van
  legacy-SQL.

Verificatie:

```text
uv run pytest tests/test_release13_portfolio_cleanup.py \
tests/test_read_facade_contract.py tests/test_read_api.py \
tests/test_top_level_read_endpoints.py -q
122 passed

uv run ruff check src/finance_sync/services/read/portfolio.py \
tests/test_release13_portfolio_cleanup.py
Geslaagd
```
