---
title: "Maak retentie- en verwijderruns controleerbaar"
status: done
priority: 20
---

## Context

Technische auditretentie verwijdert verlopen records, maar operators en
auditors hebben een samenvatting nodig van wat, waarom en wanneer is verwerkt.

## Dependencies

Release 18 audit-retention enforcement.

## Acceptance criteria

- [ ] Produceer per run aantallen per datacategorie en resultaatstatus.
- [ ] Gebruik irreversibele/geanonimiseerde identifiers in het rapport.
- [ ] Bewijs dry-run versus execute-mode en retrygedrag.
- [ ] Laat mislukte gedeeltelijke runs zichtbaar blijven zonder data-exposure.
- [ ] Bewaar rapporten volgens een afzonderlijk beperkte retentiepolicy.
