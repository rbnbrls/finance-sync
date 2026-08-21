---
title: "Extraheer portfolio-, net-worth- en cashflow-history"
status: todo
priority: 30
---

## Context

De belangrijkste analytics-reads zijn geëxtraheerd, maar history-varianten
staan nog in `read_api.py` en houden de facade groter dan de afgesproken grens.

## Dependencies

Release 13 read-legacy cleanup en analytics-tests.

## Acceptance criteria

- [ ] Maak een zelfstandig analytics-history component voor portfolio history,
  net-worth history en cashflow history.
- [ ] Behoud datumfilters, pagination, tenant/account-scope en responsevorm.
- [ ] Laat `read_api.py` uitsluitend delegeren.
- [ ] Voeg characterization- en componenttests toe.
- [ ] Query budgets worden per history-operatie gecontroleerd.
