---
title: "Ververs provider-contractfixtures en compatibiliteitsmatrix"
status: todo
priority: 15
---

## Context

Connector-chaostests beschermen retries, maar providerpayloads en capability-
contracten moeten periodiek worden vergeleken met actuele fixtures.

## Dependencies

Release 16 connector-chaos/retrytests en de bestaande connector-contractsuite.

## Acceptance criteria

- [ ] Leg per ondersteunde connector versie, capability-set en fixturedatum
  vast.
- [ ] Test account-, transaction-, holding-, security- en FX-contracten waar
  de connector die capability aanbiedt.
- [ ] Detecteer ontbrekende velden, typewijzigingen en onverwachte enumwaarden
  met duidelijke failures.
- [ ] Gebruik geen echte credentials of financiële persoonsgegevens.
- [ ] Documenteer de procedure om een fixture veilig te verversen.
