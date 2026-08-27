---
title: "Bewaak read-performance tegen een opgeslagen baseline"
status: done
priority: 25
---

## Context

Release 14 levert contract- en smoke-evidence; structurele performance-
regressies moeten daarna ook tussen releases zichtbaar blijven.

## Dependencies

Release 14 OpenAPI/benchmark- en smoke-evidence.

## Acceptance criteria

- [x] Bewaar de laatste geslaagde query- en latencybaseline per read-operatie.
- [x] Vergelijk iedere releasecandidate met de baseline.
- [x] Laat query-budgetoverschrijding altijd falen.
- [x] Laat een configureerbare latency-afwijking een waarschuwing en optioneel
  een failure opleveren.
- [x] Rapporteer datasetprofiel, databaseversie en hardwaremetadata.

## Implementatie en verificatie

- `config/read-performance-baseline.json` bewaart per read-operatie de
  query- en latencybaseline inclusief dataset-, PostgreSQL- en hardwareprofiel.
- `check_read_performance.py` vergelijkt CI-benchmark-artifacts; query-budgetten
  falen altijd en latencyregressies zijn waarschuwingen tenzij
  `PERF_FAIL_LATENCY=true` is ingesteld.
- CI uploadt zowel de benchmark als `read-performance-comparison.json`.
- Verificatie: `tests/test_release15_performance_monitoring.py`, Ruff, Pyright
  en `git diff --check` geslaagd.
