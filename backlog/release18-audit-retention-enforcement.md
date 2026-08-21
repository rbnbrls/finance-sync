---
title: "Dwing auditretentie technisch af"
status: todo
priority: 20
---

## Context

De audittrail is geïnventariseerd, maar bewaartermijnen moeten automatisch
worden toegepast en gecontroleerd.

## Dependencies

Release 17 audit-trail completeness en dataretentie/privacy-audit.

## Acceptance criteria

- [ ] Voeg een configureerbare, veilige retentiepolicy toe voor auditdata.
- [ ] Verwijder of archiveer alleen records buiten de retentiegrens.
- [ ] Houd verwijderruns zelf auditbaar zonder secrets of financiële waarden.
- [ ] Test tenant-scope, dry-run, retry en rollback/failure.
- [ ] Laat policy- en schemawijzigingen via migration/documentatie verlopen.
