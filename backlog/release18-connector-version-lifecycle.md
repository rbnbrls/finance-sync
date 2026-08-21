---
title: "Beheer connectorversies en deprecation lifecycle"
status: todo
priority: 15
---

## Context

Provider-contractfixtures worden ververst, maar connectorversies, compatibiliteit
en uitfasering zijn nog niet als lifecycle beheerd.

## Dependencies

Release 17 provider-contract-refresh.

## Acceptance criteria

- [ ] Definieer connectorversie, ondersteunde capability-set en minimale
  fixtureversie.
- [ ] Rapporteer incompatibele of verouderde connectors veilig in health en
  sync diagnostics.
- [ ] Ondersteun een deprecation warning en een expliciete removaldatum.
- [ ] Test registry, feature flags, bestaande verbindingen en rollback naar
  de vorige connectorversie.
- [ ] Documenteer providercontract, releasebeleid en operatoractie.
