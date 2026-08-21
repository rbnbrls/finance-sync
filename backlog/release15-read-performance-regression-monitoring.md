---
title: "Bewaak read-performance tegen een opgeslagen baseline"
status: todo
priority: 25
---

## Context

Release 14 levert contract- en smoke-evidence; structurele performance-
regressies moeten daarna ook tussen releases zichtbaar blijven.

## Dependencies

Release 14 OpenAPI/benchmark- en smoke-evidence.

## Acceptance criteria

- [ ] Bewaar de laatste geslaagde query- en latencybaseline per read-operatie.
- [ ] Vergelijk iedere releasecandidate met de baseline.
- [ ] Laat query-budgetoverschrijding altijd falen.
- [ ] Laat een configureerbare latency-afwijking een waarschuwing en optioneel
  een failure opleveren.
- [ ] Rapporteer datasetprofiel, databaseversie en hardwaremetadata.
