---
title: "Ververs provider-contractfixtures en compatibiliteitsmatrix"
status: done
priority: 15
---

## Context

Connector-chaostests beschermen retries, maar providerpayloads en capability-
contracten moeten periodiek worden vergeleken met actuele fixtures.

## Dependencies

Release 16 connector-chaos/retrytests en de bestaande connector-contractsuite.

## Acceptance criteria

- [x] Leg per ondersteunde connector versie, capability-set en fixturedatum
  vast.
- [x] Test account-, transaction-, holding-, security- en FX-contracten waar
  de connector die capability aanbiedt.
- [x] Detecteer ontbrekende velden, typewijzigingen en onverwachte enumwaarden
  met duidelijke failures.
- [x] Gebruik geen echte credentials of financiële persoonsgegevens.
- [x] Documenteer de procedure om een fixture veilig te verversen.

## Implementatie en verificatie

- `config/provider-contract-matrix.json` bevat versie, capability-set en
  fixturedatum voor bunq, Trading212, DEGIRO Pension en YNAB.
- `provider_contract_refresh.py` detecteert capability-mismatches, ontbrekende
  velden en lege enumdefinities en maakt een veilig compatibiliteitsrapport.
- CI valideert en archiveert `provider-contracts-${{ github.sha }}`; de matrix
  gebruikt uitsluitend fixture-identifiers.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
