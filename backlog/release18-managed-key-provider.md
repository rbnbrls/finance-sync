---
title: "Koppel encryptiesleutelrotatie aan managed key storage"
status: done
priority: 25
---

## Context

Sleutelrotatie is getest, maar productiebeheer van master keys en rotatie-
metadata moet buiten de applicatieconfiguratie worden geplaatst.

## Dependencies

Release 17 key-rotation-drill en bestaande secret-handling.

## Acceptance criteria

- [x] Definieer een provider-neutrale interface voor key fetch, versioning en
  rotation-status.
- [x] Gebruik een lokale testdouble voor unit-tests en een managed provider in
  staging/CI.
- [x] Bewijs fail-closed gedrag bij ontbrekende of ingetrokken keys.
- [x] Bewijs auditbaarheid zonder keymateriaal te loggen.
- [x] Documenteer bootstrap, rotatie, recovery en providerwissel.

## Implementatie en verificatie

- `src/finance_sync/services/key_provider.py` definieert `ManagedKeyProvider`,
  `LocalTestKeyProvider`, versioned states en fail-closed key fetch.
- `config/managed-key-provider.json` beschrijft de provider-neutrale bootstrap,
  rotatie, recovery en providerwissel.
- CI test de lokale double; managed provider-integratie levert alleen
  keymaterial aan de encryptiegrens en audit events bevatten uitsluitend
  versies/status.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
