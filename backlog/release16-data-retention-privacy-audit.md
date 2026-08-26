---
title: "Voer dataretentie- en privacy-audit uit"
status: done
priority: 20
---

## Context

De applicatie verwerkt financiële data, credentials en outboxpayloads. Na de
releasehardening moet aantoonbaar zijn welke data hoelang wordt bewaard.

## Dependencies

Release 15 operational observability en security-exception lifecycle.

## Acceptance criteria

- [x] Inventariseer credentials, auditdata, outboxpayloads, logs en financiële
  facts per opslaglocatie.
- [x] Leg bewaartermijn, verwijderbeleid en wettelijke/operationele rationale
  per categorie vast.
- [x] Bewijs dat tenant-scoping en redaction behouden blijven.
- [x] Voeg tests toe voor verwijdering/anonymisering waar van toepassing.
- [x] Documenteer een veilige operatorprocedure zonder echte financiële data.

## Implementatie en verificatie

- `config/data-retention-policy.json` inventariseert credentials, auditdata,
  outboxpayloads, logs, financiële facts en providerpayloads met locatie,
  termijn, delete/anonymise-beleid en rationale.
- `check_data_retention_policy.py` faalt bij ontbrekende categorieën, ontbrekende
  deletion rules, te lange reviewcadans of ontbrekende tenant-scope/redaction.
- `tests/test_release16_data_retention.py` test beleid, tenant-scope en
  redaction; CI valideert en archiveert het beleid naast de SBOM.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
