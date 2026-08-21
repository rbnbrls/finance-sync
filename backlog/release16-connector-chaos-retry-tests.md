---
title: "Test connector-retries en rate-limit-chaos"
status: todo
priority: 20
---

## Context

Connectors hebben retry- en rate-limitgedrag, maar provider timeouts,
429-responses en gedeeltelijke responses moeten systematisch worden getest.

## Dependencies

Release 15 sync-failure recovery drill en bestaande connector-contracttests.

## Acceptance criteria

- [ ] Injecteer timeouts, 429's, malformed responses en tijdelijke Redis-/DB-
  fouten in connector-contracttests.
- [ ] Bewijs bounded retries, backoff en correcte permanente failure-status.
- [ ] Bewijs dat een retry geen dubbele domain rows of outbox-events maakt.
- [ ] Bewijs dat credentials en providerpayloads niet in errors/logs lekken.
- [ ] Voer de scenario's uit tegen minstens één bank- en één brokerfixture.
