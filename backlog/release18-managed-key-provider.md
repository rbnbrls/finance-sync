---
title: "Koppel encryptiesleutelrotatie aan managed key storage"
status: todo
priority: 25
---

## Context

Sleutelrotatie is getest, maar productiebeheer van master keys en rotatie-
metadata moet buiten de applicatieconfiguratie worden geplaatst.

## Dependencies

Release 17 key-rotation-drill en bestaande secret-handling.

## Acceptance criteria

- [ ] Definieer een provider-neutrale interface voor key fetch, versioning en
  rotation-status.
- [ ] Gebruik een lokale testdouble voor unit-tests en een managed provider in
  staging/CI.
- [ ] Bewijs fail-closed gedrag bij ontbrekende of ingetrokken keys.
- [ ] Bewijs auditbaarheid zonder keymateriaal te loggen.
- [ ] Documenteer bootstrap, rotatie, recovery en providerwissel.
