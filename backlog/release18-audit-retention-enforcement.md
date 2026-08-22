---
title: "Dwing auditretentie technisch af"
status: done
priority: 20
---

## Context

De audittrail is geïnventariseerd, maar bewaartermijnen moeten automatisch
worden toegepast en gecontroleerd.

## Dependencies

Release 17 audit-trail completeness en dataretentie/privacy-audit.

## Acceptance criteria

- [x] Voeg een configureerbare, veilige retentiepolicy toe voor auditdata.
- [x] Verwijder of archiveer alleen records buiten de retentiegrens.
- [x] Houd verwijderruns zelf auditbaar zonder secrets of financiële waarden.
- [x] Test tenant-scope, dry-run, retry en rollback/failure.
- [x] Laat policy- en schemawijzigingen via migration/documentatie verlopen.

## Implementatie en verificatie

- `config/audit-retention-policy.json` definieert 3650 dagen retentie,
  batchgrootte, tenant-scope, archive-before-delete en dry-run als default.
- `enforce_audit_retention.py` selecteert uitsluitend verlopen records van de
  aangewezen tenant, maakt een auditbaar runrapport en rolt gedeeltelijke
  deletes terug bij storage-fouten; een retry is veilig.
- Er is geen schemawijziging nodig; policy en operatorprocedure zijn
  version-controlled en CI draait de tenant-scoped dry-run.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
