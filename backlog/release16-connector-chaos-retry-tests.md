---
title: "Test connector-retries en rate-limit-chaos"
status: done
priority: 20
---

## Context

Connectors hebben retry- en rate-limitgedrag, maar provider timeouts,
429-responses en gedeeltelijke responses moeten systematisch worden getest.

## Dependencies

Release 15 sync-failure recovery drill en bestaande connector-contracttests.

## Acceptance criteria

- [x] Injecteer timeouts, 429's, malformed responses en tijdelijke Redis-/DB-
  fouten in connector-contracttests.
- [x] Bewijs bounded retries, backoff en correcte permanente failure-status.
- [x] Bewijs dat een retry geen dubbele domain rows of outbox-events maakt.
- [x] Bewijs dat credentials en providerpayloads niet in errors/logs lekken.
- [x] Voer de scenario's uit tegen minstens één bank- en één brokerfixture.

## Implementatie en verificatie

- `config/connector-chaos-scenarios.json` definieert synthetische bank- en
  brokerfixtures voor timeout, 429, malformed response en tijdelijke Redis/DB-
  fouten.
- `tests/test_release16_connector_chaos.py` bewijst bounded retries,
  exponentiële backoff, permanente failures, idempotente event-creatie en
  redaction van providerfouten.
- De nieuwe `connector-chaos` CI-job voert de matrix geïsoleerd uit zonder
  echte providerverbindingen.
- Verificatie: 5 tests, Ruff en Pyright geslaagd.
