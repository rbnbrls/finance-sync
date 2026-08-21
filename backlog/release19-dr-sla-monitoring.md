---
title: "Monitor disaster-recovery RPO en RTO"
status: todo
priority: 25
---

## Context

Release 18 automatiseert herstelstappen, maar er is nog geen periodieke
monitoring die aantoont dat de afgesproken RPO/RTO daadwerkelijk wordt gehaald.

## Dependencies

Release 18 automated DR runbook.

## Acceptance criteria

- [ ] Voer periodiek een geïsoleerde restore-check uit met synthetische data.
- [ ] Meet restoreduur, laatste bruikbare backup, replay-lag en herstelstatus.
- [ ] Maak alerts bij overschrijding van RPO/RTO.
- [ ] Publiceer status zonder tenantdata, credentials of financiële waarden.
- [ ] Link iedere failure aan een runbook en eigenaar.
