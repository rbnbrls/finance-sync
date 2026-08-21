---
title: "Certificeer connectoren tegen versie- en capabilitycontracten"
status: todo
priority: 15
---

## Context

Connectorversies en deprecation lifecycle zijn gedefinieerd, maar er is nog
geen formele certificeringsstatus per connector en capability.

## Dependencies

Release 18 connector-version lifecycle en Release 17 provider fixtures.

## Acceptance criteria

- [ ] Maak per connector een machineleesbare certificeringsmatrix voor
  accounts, transactions, holdings, securities en FX.
- [ ] Certificering vereist contract-, retry-, idempotentie- en securitytests.
- [ ] Een ontbrekende of verlopen certificering blokkeert nieuwe release-
  promotion voor die connector.
- [ ] Rapporteer versie, fixturedatum, testcommit en certificeringsdatum.
- [ ] Gebruik uitsluitend mocks/fixtures zonder echte providercredentials.
