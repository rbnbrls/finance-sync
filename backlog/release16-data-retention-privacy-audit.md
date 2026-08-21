---
title: "Voer dataretentie- en privacy-audit uit"
status: todo
priority: 20
---

## Context

De applicatie verwerkt financiële data, credentials en outboxpayloads. Na de
releasehardening moet aantoonbaar zijn welke data hoelang wordt bewaard.

## Dependencies

Release 15 operational observability en security-exception lifecycle.

## Acceptance criteria

- [ ] Inventariseer credentials, auditdata, outboxpayloads, logs en financiële
  facts per opslaglocatie.
- [ ] Leg bewaartermijn, verwijderbeleid en wettelijke/operationele rationale
  per categorie vast.
- [ ] Bewijs dat tenant-scoping en redaction behouden blijven.
- [ ] Voeg tests toe voor verwijdering/anonymisering waar van toepassing.
- [ ] Documenteer een veilige operatorprocedure zonder echte financiële data.
