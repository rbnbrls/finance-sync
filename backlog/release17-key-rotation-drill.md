---
title: "Test rotatie van encryptiesleutels zonder datatoegang te verliezen"
status: todo
priority: 25
---

## Context

Backups, credentials en gevoelige configuratie moeten ook na sleutelrotatie
leesbaar blijven voor geautoriseerde services en onleesbaar voor oude of
ongeldige sleutels.

## Dependencies

Release 16 backup/restore en dataretentie-audit.

## Acceptance criteria

- [ ] Definieer current-, previous- en retired-key states.
- [ ] Roteer een synthetische dataset zonder verlies of plaintext-export.
- [ ] Bewijs dat oude sleutels alleen tijdens de gecontroleerde overgang
  bruikbaar zijn.
- [ ] Test restart, worker-job en restore tijdens/na rotatie.
- [ ] Documenteer operatorcommando's, auditsporen en rollbackgrenzen.
