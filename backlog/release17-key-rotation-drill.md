---
title: "Test rotatie van encryptiesleutels zonder datatoegang te verliezen"
status: done
priority: 25
---

## Context

Backups, credentials en gevoelige configuratie moeten ook na sleutelrotatie
leesbaar blijven voor geautoriseerde services en onleesbaar voor oude of
ongeldige sleutels.

## Dependencies

Release 16 backup/restore en dataretentie-audit.

## Acceptance criteria

- [x] Definieer current-, previous- en retired-key states.
- [x] Roteer een synthetische dataset zonder verlies of plaintext-export.
- [x] Bewijs dat oude sleutels alleen tijdens de gecontroleerde overgang
  bruikbaar zijn.
- [x] Test restart, worker-job en restore tijdens/na rotatie.
- [x] Documenteer operatorcommando's, auditsporen en rollbackgrenzen.

## Implementatie en verificatie

- `config/key-rotation-drill.json` definieert current/previous/retired states,
  audit-event en rollbackgrens.
- `scripts/key_rotation_drill.py` voert AES-256-GCM rotatie uit op een
  synthetische fixture, valideert restart/restore en bewijst dat een retired
  key geen ciphertext meer kan openen. Alleen hashes en lengtes worden
  gepubliceerd.
- CI draait de drill en archiveert `key-rotation-${{ github.sha }}`.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
