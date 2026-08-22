---
title: "Beheer connectorversies en deprecation lifecycle"
status: done
priority: 15
---

## Context

Provider-contractfixtures worden ververst, maar connectorversies, compatibiliteit
en uitfasering zijn nog niet als lifecycle beheerd.

## Dependencies

Release 17 provider-contract-refresh.

## Acceptance criteria

- [x] Definieer connectorversie, ondersteunde capability-set en minimale
  fixtureversie.
- [x] Rapporteer incompatibele of verouderde connectors veilig in health en
  sync diagnostics.
- [x] Ondersteun een deprecation warning en een expliciete removaldatum.
- [x] Test registry, feature flags, bestaande verbindingen en rollback naar
  de vorige connectorversie.
- [x] Documenteer providercontract, releasebeleid en operatoractie.

## Implementatie en verificatie

- `config/connector-lifecycle.json` definieert versie, capabilities, minimale
  fixture, feature flag, deprecation/removal date en vorige versie voor bunq,
  Trading212, DEGIRO Pension en YNAB.
- `connector_lifecycle.py` rapporteert healthy, disabled, deprecated of
  incompatible diagnostics zonder credentials en met rollbackversie.
- CI valideert en archiveert `connector-lifecycle-${{ github.sha }}`.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
